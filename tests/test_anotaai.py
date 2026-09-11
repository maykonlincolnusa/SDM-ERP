"""A integração com a Anota Aí: o cardápio que a loja mantém lá.

Toda venda da loja passa pela Anota Aí, e o relatório exportado de pedidos não
traz os itens vendidos. Essa ausência é a raiz do que o painel não responde:
sem saber quais copos saíram, a ficha técnica não baixa insumo, o custo não
fecha e a previsão não tem o que aprender.

O cardápio é a ponta que resolve isso na origem, e é a única parte da API cuja
resposta já está documentada — por isso ela vem primeiro. `Transport` é
injetável, então estes testes exercitam a tradução da resposta real sem tocar
na internet.
"""

import json

import pytest

from app.repositories.secrets import FileVault
from app.repositories.store import DemoStore
from app.services import ERPService
from app.services.anotaai import MENU_PATH, AnotaAiClient, AnotaAiError, AnotaAiService

CHAVE = "chave-de-integracao-da-loja"

# A forma exata que a Anota Aí devolve, conferida sondando a API em 18/08/2026.
CARDAPIO = {
    "success": True,
    "message": "ok",
    "categories": [
        {
            "title": "Açaí",
            "id": "cat-1",
            "is_additional": False,
            "itens": [
                # Casa com o cardápio do ERP pelo nome.
                {"id": "a1", "title": "Açaí 500 ml", "price": 22.0, "out": False, "external_id": None},
                # Não existe no ERP: é o item que chega em pedido sem custo.
                {"id": "a2", "title": "Açaí Ninho com Nutella 700 ml", "price": 32.0, "out": False, "external_id": None},
                # Fora do ar no cardápio deles.
                {"id": "a3", "title": "Açaí Sazonal Pitaya", "price": 28.0, "out": True, "external_id": None},
            ],
        },
        {
            "title": "Complementos",
            "id": "cat-2",
            "is_additional": True,
            "itens": [{"id": "c1", "title": "Granola", "price": 2.0, "out": False, "external_id": None}],
        },
    ],
}


def _transporte(resposta, status=200, registro=None):
    def transporte(method, url, *, headers, body, timeout):
        if registro is not None:
            registro.append({"method": method, "url": url, "headers": headers})
        return status, resposta

    return transporte


@pytest.fixture
def servico(tmp_path) -> AnotaAiService:
    store = DemoStore(tmp_path / "store.json")
    return AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte(CARDAPIO)),
    )


# ----------------------------------------------------------------------
# A conexao
# ----------------------------------------------------------------------


def test_a_chave_vai_direto_no_cabecalho_sem_bearer(tmp_path):
    """Com "Bearer" a Anota Aí devolve 401 — foi medido contra a API real."""

    registro = []
    cliente = AnotaAiClient(_transporte(CARDAPIO, registro=registro))

    cliente.catalog(CHAVE)

    assert registro[0]["headers"]["Authorization"] == CHAVE
    assert "Bearer" not in registro[0]["headers"]["Authorization"]
    assert registro[0]["url"].endswith(MENU_PATH)


def test_conectar_confirma_a_chave_na_hora(servico):
    """Chave errada guardada em silêncio vira "não traz nada" três dias depois."""

    resultado = servico.connect(CHAVE)

    assert resultado["connected"] is True
    assert resultado["categories"] == 2
    assert resultado["items"] == 4
    assert servico.status()["connected"] is True


def test_a_chave_nunca_volta_para_a_tela(servico):
    servico.connect(CHAVE)

    estado = servico.status()

    assert CHAVE not in json.dumps(estado)
    assert "integration_key" not in estado


def test_chave_recusada_explica_onde_achar_a_certa(tmp_path):
    """Colar a do endereço do navegador é o erro comum: ela é sessão de 15 min."""

    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte({"err": "Failed to authenticate token."}, status=401)),
    )

    with pytest.raises(AnotaAiError, match="Configurações → Integrações"):
        servico.connect(CHAVE)

    assert servico.status()["connected"] is False


def test_endereco_nao_reconhecido_sugere_liberacao_pelo_suporte(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte({}, status=404)),
    )

    with pytest.raises(AnotaAiError, match="liberada para a loja"):
        servico.connect(CHAVE)


def test_sem_conexao_a_previa_diz_o_que_falta(servico):
    with pytest.raises(AnotaAiError, match="ainda não está conectada"):
        servico.catalog_preview()


def test_desconectar_apaga_a_chave(servico):
    servico.connect(CHAVE)

    servico.disconnect()

    assert servico.status()["connected"] is False
    with pytest.raises(AnotaAiError, match="ainda não está conectada"):
        servico.catalog_preview()


# ----------------------------------------------------------------------
# A previa: relatar antes de criar
# ----------------------------------------------------------------------


def test_a_previa_separa_o_que_casa_do_que_falta(servico):
    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    casados = {linha["name"] for linha in previa["matched"]}
    novos = {linha["name"] for linha in previa["new"]}
    assert "Açaí 500 ml" in casados
    assert "Açaí Ninho com Nutella 700 ml" in novos
    # Complemento não é produto: criá-lo como copo vendável seria inventar item.
    assert [linha["name"] for linha in previa["addons"]] == ["Granola"]


def test_a_previa_marca_quem_casou_so_pelo_nome(servico):
    """Casar por nome funciona até alguém renomear o item lá."""

    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    acai = next(linha for linha in previa["matched"] if linha["name"] == "Açaí 500 ml")
    assert acai["linked_by"] == "nome"
    assert any("identificador" in leitura["title"] for leitura in previa["readings"])


def test_a_previa_aponta_preco_divergente(servico):
    """Quem cobra do cliente é a Anota Aí; divergir é margem calculada errada."""

    servico.connect(CHAVE)
    produto = next(p for p in servico.store.list("products") if p["name"] == "Açaí 500 ml")
    produto["price"] = 19.0
    servico.store.put("products", produto["id"], produto)

    previa = servico.catalog_preview()

    acai = next(linha for linha in previa["matched"] if linha["name"] == "Açaí 500 ml")
    assert acai["price_differs"] is True
    assert any("preço diferente" in leitura["title"] for leitura in previa["readings"])


def test_a_previa_mostra_o_que_existe_so_aqui(servico):
    """Produto que some do cardápio de lá para de vender sem ninguém notar."""

    servico.connect(CHAVE)

    previa = servico.catalog_preview()

    assert any(linha["name"] == "Açaí 300 ml" for linha in previa["only_here"])


def test_a_previa_nao_grava_nada(servico):
    servico.connect(CHAVE)
    antes = len(servico.store.list("products"))

    servico.catalog_preview()

    assert len(servico.store.list("products")) == antes


# ----------------------------------------------------------------------
# A importacao: o cadastro entra, a ficha fica
# ----------------------------------------------------------------------


def test_importar_cria_o_produto_sem_ficha_tecnica(servico):
    """Inventar receita seria inventar custo, e custo inventado se espalha."""

    servico.connect(CHAVE)

    resultado = servico.import_catalog(["a2"])

    assert len(resultado["created"]) == 1
    criado = next(p for p in servico.store.list("products") if p["name"] == "Açaí Ninho com Nutella 700 ml")
    assert criado["recipe"] == []
    assert criado["price"] == 32.0
    assert resultado["pending_recipe"] == 1


def test_o_item_fora_do_ar_entra_desativado(servico):
    servico.connect(CHAVE)

    servico.import_catalog(["a3"])

    sazonal = next(p for p in servico.store.list("products") if p["name"] == "Açaí Sazonal Pitaya")
    assert sazonal["active"] is False


def test_importar_sem_escolha_traz_todos_os_que_faltam(servico):
    servico.connect(CHAVE)

    resultado = servico.import_catalog([])

    assert len(resultado["created"]) == 2


def test_um_item_recusado_nao_derruba_os_outros(tmp_path):
    """Preço zero é item de vitrine no cardápio deles, não produto vendável."""

    cardapio = {
        "success": True,
        "categories": [{
            "title": "Açaí", "id": "c", "is_additional": False,
            "itens": [
                {"id": "z1", "title": "A partir de", "price": 0, "out": False},
                {"id": "z2", "title": "Copo Novo 900 ml", "price": 36.0, "out": False},
            ],
        }],
    }
    store = DemoStore(tmp_path / "store.json")
    servico = AnotaAiService(
        store, ERPService(store), FileVault(tmp_path / "cofre.json"),
        AnotaAiClient(_transporte(cardapio)),
    )
    servico.connect(CHAVE)

    resultado = servico.import_catalog([])

    assert [linha["name"] for linha in resultado["created"]] == ["Copo Novo 900 ml"]
    assert resultado["rejected"][0]["name"] == "A partir de"
    assert "sem preço" in resultado["rejected"][0]["reason"]


def test_importar_sem_novidade_e_recusado(servico):
    servico.connect(CHAVE)
    servico.import_catalog([])

    with pytest.raises(AnotaAiError, match="Nenhum item novo"):
        servico.import_catalog([])
