"""O retrato de leitura que dura uma requisição.

Medido em produção antes desta mudança: `/api/promotions/month` com 16,5 s de
mediana e 27,8 s no pior caso. A causa não era uma consulta lenta — era a mesma
consulta repetida. Uma requisição dessa rota varria `orders` seis vezes, porque
cada serviço encadeado (promoção, previsão, preço, custo, ERP) lê o histórico
inteiro por conta própria. Contra mil pedidos no Firestore, cada varredura é uma
viagem de rede que traz tudo.

O que estes testes protegem não é a velocidade — é a promessa que a torna
segura: **ligar o cache não muda resultado nenhum**. Uma leitura cacheada tem
de devolver exatamente o que o banco devolveria, inclusive objetos novos, e
qualquer gravação tem de derrubar o retrato antes da próxima leitura.
"""

import os
from pathlib import Path

import pytest

from app.repositories import readcache
from app.repositories.store import DemoStore


@pytest.fixture
def store(tmp_path) -> DemoStore:
    return DemoStore(tmp_path / "store.json")


@pytest.fixture(autouse=True)
def escopo_limpo():
    """Nenhum teste herda o retrato do anterior."""

    readcache.fechar_escopo()
    yield
    readcache.fechar_escopo()


# ----------------------------------------------------------------------
# O ganho
# ----------------------------------------------------------------------


def test_a_mesma_colecao_e_lida_uma_vez_so_na_mesma_requisicao(store, monkeypatch):
    leituras = []
    original = DemoStore._read
    monkeypatch.setattr(DemoStore, "_read", lambda self: (leituras.append(1), original(self))[1])

    readcache.abrir_escopo()
    for _ in range(6):
        store.list("orders")

    assert len(leituras) == 1


def test_fora_de_requisicao_o_cache_nao_liga_sozinho(store, monkeypatch):
    """Script, teste e tarefa de fundo continuam falando direto com o banco.

    Ligar sozinho seria pior do que não existir: um processo longo passaria a
    servir dado velho sem nada no código dizendo que ele faz isso.
    """

    leituras = []
    original = DemoStore._read
    monkeypatch.setattr(DemoStore, "_read", lambda self: (leituras.append(1), original(self))[1])

    for _ in range(3):
        store.list("orders")

    assert len(leituras) == 3


# ----------------------------------------------------------------------
# A promessa: ligar o cache não muda resultado
# ----------------------------------------------------------------------


def test_a_leitura_cacheada_devolve_objetos_novos(store):
    """Servir o mesmo dicionário duas vezes mudaria o comportamento em silêncio.

    O ERP lê uma coleção, mexe no que leu e grava. Se a segunda leitura
    devolvesse o mesmo objeto, ela enxergaria a alteração da primeira antes de
    ela existir no banco.
    """

    readcache.abrir_escopo()
    primeira = store.list("inventory")
    primeira[0]["quantity"] = 99999

    segunda = store.list("inventory")

    assert segunda[0]["quantity"] != 99999
    assert segunda[0] is not primeira[0]


def test_gravar_derruba_o_retrato_da_colecao(store):
    readcache.abrir_escopo()
    antes = len(store.list("couriers"))

    store.put("couriers", "novo", {"id": "novo", "name": "Maria", "active": True})

    assert len(store.list("couriers")) == antes + 1


def test_apagar_derruba_o_retrato_da_colecao(store):
    readcache.abrir_escopo()
    store.put("couriers", "novo", {"id": "novo", "name": "Maria", "active": True})
    antes = len(store.list("couriers"))

    store.delete("couriers", "novo")

    assert len(store.list("couriers")) == antes - 1


def test_a_transacao_derruba_o_retrato_inteiro(store):
    """Ela mexe em várias coleções e não diz em quais."""

    readcache.abrir_escopo()
    estoque_antes = next(item for item in store.list("inventory") if item["id"] == "acai")["quantity"]
    store.list("inventory_movements")

    def baixar(transaction):
        item = transaction.get("inventory", "acai")
        item["quantity"] = item["quantity"] - 5
        transaction.put("inventory", "acai", item)
        transaction.put("inventory_movements", "m1", {"id": "m1", "inventory_id": "acai", "quantity_delta": -5})
        return item

    store.run_transaction(baixar)

    assert next(item for item in store.list("inventory") if item["id"] == "acai")["quantity"] == estoque_antes - 5
    assert any(linha["id"] == "m1" for linha in store.list("inventory_movements"))


def test_uma_requisicao_nao_herda_o_retrato_da_anterior(store):
    readcache.abrir_escopo()
    store.list("couriers")
    readcache.fechar_escopo()

    # Outra requisição grava; a próxima tem de enxergar.
    store.put("couriers", "novo", {"id": "novo", "name": "Maria", "active": True})
    readcache.abrir_escopo()

    assert any(linha["id"] == "novo" for linha in store.list("couriers"))


# ----------------------------------------------------------------------
# Ligação com a requisição HTTP
# ----------------------------------------------------------------------


def test_o_escopo_abre_e_fecha_com_a_requisicao(tmp_path, monkeypatch):
    """Retrato que sobrevive à requisição serviria dado velho na seguinte."""

    from fastapi.testclient import TestClient

    for chave, valor in {
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp_path / "s.json"), "AUTH_REQUIRED": "false",
        "DATA_RAW_PATH": str(tmp_path / "r"), "MEDIA_INBOX_PATH": str(tmp_path / "i"),
        "MEDIA_LIBRARY_PATH": str(tmp_path / "l"), "MEDIA_SYNC_LEGACY_FILES": "false",
    }.items():
        monkeypatch.setenv(chave, valor)

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert client.get("/api/inventory").status_code == 200
            # Terminada a requisição, não sobra retrato nenhum no processo.
            assert readcache.escopo_atual() is None
    finally:
        get_settings.cache_clear()


def test_o_que_foi_gravado_pela_rota_aparece_na_leitura_seguinte(tmp_path, monkeypatch):
    """A gravação e a releitura acontecem dentro da mesma requisição."""

    from fastapi.testclient import TestClient

    for chave, valor in {
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp_path / "s.json"), "AUTH_REQUIRED": "false",
        "DATA_RAW_PATH": str(tmp_path / "r"), "MEDIA_INBOX_PATH": str(tmp_path / "i"),
        "MEDIA_LIBRARY_PATH": str(tmp_path / "l"), "MEDIA_SYNC_LEGACY_FILES": "false",
    }.items():
        monkeypatch.setenv(chave, valor)

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            antes = next(i for i in client.get("/api/inventory").json() if i["id"] == "acai")["quantity"]
            resposta = client.post("/api/inventory/withdrawals", json={
                "items": [{"inventory_id": "acai", "quantity": 1}],
                "responsible": "Maria",
            })
            assert resposta.status_code == 201, resposta.text
            depois = next(i for i in client.get("/api/inventory").json() if i["id"] == "acai")["quantity"]
            assert depois == antes - 1
    finally:
        get_settings.cache_clear()
