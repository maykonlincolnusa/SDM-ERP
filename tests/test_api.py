"""A camada HTTP.

Até aqui os serviços tinham teste e as rotas não. A diferença importa mais do
que parece: o serviço pode estar certo e a rota errada — caminho trocado,
parâmetro que não chega, exceção de domínio virando 500 em vez de 422, ordem de
rota que faz `/inventory/withdrawals` cair no handler de `/inventory/{id}`.
Nada disso aparece num teste de serviço.

O foco aqui é o contrato: o caminho existe, o método certo responde, o erro de
domínio vira 422 com a mensagem que o balcão vai ler, e a validação recusa o
que precisa ser recusado.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Cliente com loja isolada.

    O `lifespan` monta os serviços a partir das settings, então a troca do
    caminho precisa acontecer antes de o cliente entrar no contexto — e o cache
    de `get_settings` precisa cair junto, senão a loja real seria usada.
    """

    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.setenv("DEMO_DATA_PATH", str(tmp_path / "store.json"))
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    get_settings.cache_clear()
    with TestClient(app) as cliente:
        yield cliente
    get_settings.cache_clear()


def _saldo(client, inventory_id):
    return next(item for item in client.get("/api/inventory").json() if item["id"] == inventory_id)["quantity"]


# ----------------------------------------------------------------------
# O básico: as telas conseguem carregar
# ----------------------------------------------------------------------


def test_health_e_config_respondem(client):
    assert client.get("/health").json()["status"] == "healthy"
    assert client.get("/api/config").json()["storage"] == "demo"


@pytest.mark.parametrize("caminho", [
    "/api/dashboard",
    "/api/sales/daily",
    "/api/forecast",
    "/api/planning",
    "/api/growth",
    "/api/promotions/week",
    "/api/promotions/month",
    "/api/pricing",
    "/api/products",
    "/api/products/lines",
    "/api/addons",
    "/api/inventory",
    "/api/inventory/consumption",
    "/api/inventory/crossing",
    "/api/orders",
    "/api/finance/operating",
    "/api/expenses",
    "/api/expenses/recurring",
    "/api/channels",
    "/api/channels/performance",
    "/api/couriers",
    "/api/deliveries",
    "/api/catalogs",
    "/api/market-prices",
    "/api/sport-anchors",
    "/api/audit",
])
def test_toda_rota_de_leitura_responde(client, caminho):
    """Uma tela quebra inteira quando uma dessas falha, porque loadAll é um Promise.all."""

    resposta = client.get(caminho)
    assert resposta.status_code == 200, f"{caminho} devolveu {resposta.status_code}: {resposta.text[:200]}"


def test_as_rotas_que_o_carregamento_inicial_usa_existem_todas(client):
    """Espelha o Promise.all de loadAll: se uma cair, a tela não monta."""

    for caminho in (
        "/api/config", "/api/dashboard?days=7", "/api/inventory/consumption?days=7",
        "/api/inventory/crossing?days=30", "/api/forecast?horizon=7", "/api/products",
        "/api/addons", "/api/inventory", "/api/orders", "/api/expenses",
        "/api/expenses/recurring", "/api/finance/operating?days=30", "/api/media/summary",
        "/api/media", "/api/channels", "/api/channels/performance?days=30",
        "/api/products/lines?days=30", "/api/sales/daily", "/api/growth?days=90",
        "/api/promotions/week?days=90&horizon=7", "/api/promotions/month?days=90&weeks=4",
        "/api/pricing?days=90", "/api/market-prices",
        "/api/planning?days=90&horizon=7&service_level=0.9", "/api/couriers",
        "/api/deliveries?days=30", "/api/couriers/performance?days=30", "/api/catalogs",
        "/api/audit?limit=60",
    ):
        assert client.get(caminho).status_code == 200, caminho


# ----------------------------------------------------------------------
# Erro de domínio precisa chegar legível ao balcão
# ----------------------------------------------------------------------


def test_erro_de_dominio_vira_422_com_a_mensagem_do_servico(client):
    resposta = client.post("/api/inventory/banana/adjust", json={"quantity_delta": -9999, "reason": "teste"})

    assert resposta.status_code == 422
    assert "negativo" in resposta.json()["detail"]


def test_registro_inexistente_nao_vira_500(client):
    for caminho in (
        "/api/sport-anchors/nao-existe",
        "/api/market-prices/nao-existe",
        "/api/inventory/withdrawals/nao-existe",
    ):
        assert client.delete(caminho).status_code == 422, caminho


# ----------------------------------------------------------------------
# Ordem de rota: o caminho fixo não pode cair no handler do parâmetro
# ----------------------------------------------------------------------


def test_withdrawals_nao_e_confundido_com_um_id_de_insumo(client):
    """`/inventory/withdrawals` e `/inventory/{id}` disputam o mesmo prefixo."""

    antes = _saldo(client, "acai")
    criada = client.post("/api/inventory/withdrawals", json={
        "items": [{"inventory_id": "acai", "quantity": 1}],
        "responsible": "Maria", "reason": "Produção da manhã",
    })

    assert criada.status_code == 201
    assert _saldo(client, "acai") == antes - 1
    assert client.delete(f"/api/inventory/withdrawals/{criada.json()['id']}").status_code == 200
    assert _saldo(client, "acai") == antes


def test_consumption_e_crossing_nao_caem_no_handler_de_insumo(client):
    """São caminhos fixos sob o mesmo prefixo de `/inventory/{id}`."""

    assert "totals" in client.get("/api/inventory/consumption?days=7").json()
    assert client.get("/api/inventory/crossing?days=30").status_code == 200


# ----------------------------------------------------------------------
# Ciclo completo pelas rotas novas
# ----------------------------------------------------------------------


def test_ciclo_da_ancora_esportiva(client):
    criada = client.post("/api/sport-anchors", json={
        "name": "Academia do Bairro", "kind": "Academia",
        "weekdays": [0, 2, 4], "time_of_day": "Noite", "people": 150,
    })
    assert criada.status_code == 201
    anchor_id = criada.json()["id"]

    assert any(linha["id"] == anchor_id for linha in client.get("/api/sport-anchors").json())
    assert client.patch(f"/api/sport-anchors/{anchor_id}", json={"people": 200}).json()["people"] == 200
    # A âncora tem de aparecer no plano, senão o cadastro não serve para nada.
    assert any(linha["id"] == anchor_id for linha in client.get("/api/promotions/week").json()["anchors"])
    assert client.delete(f"/api/sport-anchors/{anchor_id}").status_code == 200


def test_ciclo_da_referencia_de_mercado_muda_o_veredito(client):
    def veredito():
        produtos = client.get("/api/pricing?days=90").json()["products"]
        return next(linha for linha in produtos if linha["product_id"] == "acai-500")["verdict"]["id"]

    assert veredito() == "sem-referencia"

    criada = client.post("/api/market-prices", json={
        "competitor": "Açaí do Zé", "product_id": "acai-500",
        "description": "Copo 500 ml", "price": 27.0, "channel": "Balcao",
    })
    assert criada.status_code == 201

    assert veredito() == "espaco-para-subir"
    assert client.delete(f"/api/market-prices/{criada.json()['id']}").status_code == 200
    assert veredito() == "sem-referencia"


def test_referencia_sem_alvo_e_recusada_na_rota(client):
    resposta = client.post("/api/market-prices", json={
        "competitor": "Vizinho", "description": "Copo grande", "price": 20.0,
    })

    assert resposta.status_code == 422


def test_venda_pela_rota_baixa_o_estoque(client):
    antes = _saldo(client, "acai")

    resposta = client.post("/api/orders", json={"items": [{"product_id": "acai-500", "quantity": 2}]})

    assert resposta.status_code == 201
    assert _saldo(client, "acai") < antes


# ----------------------------------------------------------------------
# Limites de parâmetro
# ----------------------------------------------------------------------


@pytest.mark.parametrize("caminho", [
    "/api/promotions/month?weeks=99",
    "/api/promotions/week?days=1",
    "/api/pricing?days=9999",
    "/api/dashboard?days=0",
])
def test_parametro_fora_do_limite_e_recusado(client, caminho):
    assert client.get(caminho).status_code == 422


def test_importacao_recusa_arquivo_que_nao_e_xlsx(client):
    resposta = client.post(
        "/api/imports/anota-ai",
        files={"report": ("pedidos.csv", b"numero,total", "text/csv")},
    )

    assert resposta.status_code == 422
    assert "xlsx" in resposta.json()["detail"].lower()
