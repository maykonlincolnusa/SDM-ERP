"""Como precificar.

Preço tem três âncoras: custo diz quanto preciso cobrar, margem-alvo diz quanto
quero cobrar, e mercado diz quanto consigo cobrar. Só as duas primeiras estão
dentro da loja. Os testes aqui protegem a terceira de ser inventada: sem
referência cadastrada o módulo tem de admitir que não sabe, e não empurrar o
custo como se fosse leitura de mercado.
"""

from datetime import date, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import ChannelCreate, MarketPriceCreate, OrderCreate, OrderLine
from app.services import ERPService, PricingService


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    return ERPService(store), PricingService(store)


def _produto(orientacao, nome):
    return next(linha for linha in orientacao["products"] if linha["name"] == nome)


def _referencia(erp, preco, competitor="Açaí do Zé", dias=0, product_id="acai-500"):
    erp.create_market_price(MarketPriceCreate(
        competitor=competitor, product_id=product_id, description="Copo 500 ml",
        price=preco, channel="Balcao",
        observed_on=(date.today() - timedelta(days=dias)).isoformat(),
    ))


# ----------------------------------------------------------------------
# Sem mercado, o módulo admite que não sabe
# ----------------------------------------------------------------------


def test_sem_referencia_o_veredito_e_pedir_referencia(loja):
    """Custo não vira leitura de mercado só porque o mercado está faltando."""

    _, pricing = loja

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["market"]["samples"] == 0
    assert linha["verdict"]["id"] == "sem-referencia"
    assert "quanto dá para cobrar" in linha["verdict"]["reason"]
    assert linha["verdict"]["suggested_price"] is None


def test_sem_nenhuma_referencia_o_bloqueio_e_explicito(loja):
    _, pricing = loja

    bloqueios = pricing.guidance(90)["blockers"]

    assert any("Nenhum produto tem referência" in b["title"] for b in bloqueios)


# ----------------------------------------------------------------------
# O piso de custo vem antes de qualquer conversa de posicionamento
# ----------------------------------------------------------------------


def test_preco_abaixo_do_custo_ignora_o_mercado(loja):
    """Vender abaixo do custo não é decisão de posicionamento."""

    erp, pricing = loja
    produto = erp.store.get("products", "acai-500")
    produto["price"] = 5.0
    erp.store.put("products", "acai-500", produto)
    _referencia(erp, 26.0)

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["verdict"]["id"] == "abaixo-do-custo"
    assert linha["verdict"]["suggested_price"] > linha["cost"]["with_shrinkage"]


def test_canal_com_taxa_que_come_a_margem_aparece_antes_do_mercado(loja):
    erp, pricing = loja
    erp.create_channel(ChannelCreate(name="Marketplace caro", fee_percent=45.0))
    _referencia(erp, 26.0)

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")
    caro = next(c for c in linha["channels"] if c["name"] == "Marketplace caro")

    assert caro["contribution"] < linha["contribution"]
    assert linha["verdict"]["id"] in ("canal-no-prejuizo", "espaco-para-subir")


# ----------------------------------------------------------------------
# Com mercado declarado
# ----------------------------------------------------------------------


def test_preco_abaixo_da_mediana_vira_espaco_para_subir(loja):
    erp, pricing = loja
    for preco, quem in ((26.0, "Açaí do Zé"), (25.0, "Point do Açaí"), (27.0, "Sabor Tropical")):
        _referencia(erp, preco, competitor=quem)

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["market"]["samples"] == 3
    assert linha["market"]["median"] == 26.0
    assert linha["market"]["position"] == "abaixo"
    assert linha["verdict"]["id"] == "espaco-para-subir"
    assert linha["verdict"]["suggested_price"] == 26.0


def test_preco_acima_da_mediana_pede_diferenciacao_e_nao_desconto(loja):
    erp, pricing = loja
    for preco, quem in ((15.0, "Açaí do Zé"), (16.0, "Point do Açaí")):
        _referencia(erp, preco, competitor=quem)

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["market"]["position"] == "acima"
    assert linha["verdict"]["id"] == "acima-do-mercado"
    # Acima da rua não é erro: é posição que precisa ser sustentada.
    assert "sustenta" in linha["verdict"]["reason"]
    assert linha["verdict"]["suggested_price"] is None


def test_preco_dentro_da_faixa_manda_nao_mexer(loja):
    erp, pricing = loja
    for preco, quem in ((22.0, "Açaí do Zé"), (23.0, "Point do Açaí")):
        _referencia(erp, preco, competitor=quem)

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["market"]["position"] == "na faixa"
    assert linha["verdict"]["id"] == "na-faixa"
    assert "se falta volume, o problema é outro" in linha["verdict"]["reason"]


def test_referencia_do_produto_tem_prioridade_sobre_a_de_categoria(loja):
    """Comparar com item equivalente vale mais que comparar com a categoria."""

    erp, pricing = loja
    _referencia(erp, 26.0)
    erp.create_market_price(MarketPriceCreate(
        competitor="Rede grande", category="Açaí", description="Média da categoria",
        price=9.0, channel="Balcao",
    ))

    linha = _produto(pricing.guidance(90), "Açaí 500 ml")

    assert linha["market"]["match"] == "produto"
    assert linha["market"]["samples"] == 1
    assert linha["market"]["median"] == 26.0


def test_referencia_velha_entra_na_conta_mas_avisa(loja):
    erp, pricing = loja
    _referencia(erp, 26.0, dias=200)
    _referencia(erp, 27.0, competitor="Point do Açaí", dias=210)

    orientacao = pricing.guidance(90)
    linha = _produto(orientacao, "Açaí 500 ml")

    assert linha["market"]["samples"] == 2
    assert linha["market"]["stale_share"] == 100.0
    assert "confirme na rua" in linha["verdict"]["reason"]
    assert any("mais de 120 dias" in b["title"] for b in orientacao["blockers"])


def test_referencia_precisa_apontar_para_produto_ou_categoria(loja):
    with pytest.raises(ValueError, match="produto do cardápio ou a uma categoria"):
        MarketPriceCreate(competitor="Vizinho", description="Copo grande", price=20.0)


def test_referencia_para_produto_inexistente_e_recusada(loja):
    erp, _ = loja

    with pytest.raises(ValueError, match="não encontrado"):
        erp.create_market_price(MarketPriceCreate(
            competitor="Vizinho", product_id="produto-que-nao-existe",
            description="Copo", price=20.0,
        ))


# ----------------------------------------------------------------------
# O mercado da própria loja
# ----------------------------------------------------------------------


def test_o_ticket_por_canal_e_medido_e_entra_na_leitura(loja):
    """Não é o mercado da rua, mas é medido — e diz até onde este cliente vai."""

    erp, pricing = loja
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="Balcao"))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=2)], channel="Delivery"))

    mercado = pricing.guidance(90)["store_market"]

    assert mercado["orders"] == 2
    assert mercado["average_ticket"] > 0
    nomes = {canal["name"] for canal in mercado["channels"]}
    assert {"Balcao", "Delivery"} <= nomes
    # Com itens no histórico a faixa de preço pode ser medida.
    assert mercado["detail_coverage"] == 100.0
    assert mercado["price_bands"]


def test_sem_itens_a_faixa_de_preco_nao_e_inventada(loja):
    erp, pricing = loja
    erp.store.put("orders", "importado-1", {
        "id": "importado-1", "number": "1", "created_at": "2026-09-01T15:00:00+00:00",
        "channel": "Balcao", "source": "anota-ai", "total": 40.0, "cost": None,
        "items": [], "status": "Concluido", "is_delivery": False, "delivery_fee": 0.0,
    })

    orientacao = pricing.guidance(90)

    assert orientacao["store_market"]["price_bands"] == []
    assert any("faixa de preço" in b["title"] for b in orientacao["blockers"])
