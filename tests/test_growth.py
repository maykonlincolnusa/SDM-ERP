"""Sugestoes de crescimento.

O risco deste modulo nao e errar a conta: e soar convincente sem lastro. Cada
teste aqui fixa uma das duas metades disso — a sugestao aparece quando os
dados a sustentam, e nao aparece (ou vem rebaixada) quando nao sustentam.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.store import DemoStore
from app.services import GrowthService
from app.services.erp import FUSO_LOJA

PRECOS = {"acai-300": 16.0, "acai-500": 22.0, "acai-700": 28.0, "super-bowl": 31.0}
CUSTOS = {"acai-300": 8.6, "acai-500": 13.82, "acai-700": 19.09, "super-bowl": 20.6}
NOMES = {"acai-300": "Açaí 300 ml", "acai-500": "Açaí 500 ml", "acai-700": "Açaí 700 ml", "super-bowl": "Super Bowl"}


@pytest.fixture
def store(tmp_path):
    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    return loja


def _pedido(loja, numero, dias_atras, itens, canal="Balcao", cliente=None, hora=14):
    dia = datetime.now(FUSO_LOJA).date() - timedelta(days=dias_atras)
    momento = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=hora)
    linhas = [{
        "product_id": pid,
        "name": NOMES[pid],
        "quantity": quantidade,
        "unit_price": PRECOS[pid],
        "unit_cost": CUSTOS[pid],
        "line_total": round(PRECOS[pid] * quantidade, 2),
    } for pid, quantidade in itens]
    total = round(sum(linha["line_total"] for linha in linhas), 2)
    loja.put("orders", f"p-{numero}", {
        "id": f"p-{numero}", "number": numero, "created_at": momento.isoformat(),
        "channel": canal, "payment_method": "Pix", "customer_name": cliente,
        "discount": 0, "total": total,
        "cost": round(sum(CUSTOS[pid] * q for pid, q in itens), 2),
        "channel_fee": round(total * 0.27, 2) if canal == "iFood" else 0,
        "status": "Concluido", "items": linhas,
        "stock_usage": [],
    })


def _loja_movimentada(loja, dias=42):
    """Historico com um par que sai junto de proposito e uma terca vazia."""

    numero = 1000
    for atras in range(dias, 0, -1):
        dia = datetime.now(FUSO_LOJA).date() - timedelta(days=atras)
        # Terca e o dia fraco plantado: um pedido pequeno contra quatro nos outros dias.
        pedidos_do_dia = 1 if dia.weekday() == 1 else 4
        for indice in range(pedidos_do_dia):
            numero += 1
            # O par 500 + granola-equivalente (super-bowl) sai junto em metade dos pedidos.
            itens = [("acai-500", 1), ("super-bowl", 1)] if indice % 2 == 0 else [("acai-300", 1)]
            # O pedido de maior valor vai pelo marketplace: e assim que a
            # comissao vira um numero que importa, e nao um detalhe.
            _pedido(loja, numero, atras, itens, canal="iFood" if indice == 0 else "Balcao")
    return loja


# ----------------------------------------------------------------------


def test_refuses_to_invent_a_combo_without_enough_history(store):
    """Com pouca venda, a resposta honesta e dizer que ainda nao da.

    Sugerir combo com cinco pedidos seria adivinhacao com cara de analise.
    """

    for numero in range(5):
        _pedido(store, 1000 + numero, numero, [("acai-500", 1), ("super-bowl", 1)])

    resultado = GrowthService(store).opportunities(90)

    combos = [s for s in resultado["suggestions"] if s["type"] == "combo"]
    assert len(combos) == 1
    assert "Ainda não dá" in combos[0]["title"]
    assert combos[0]["confidence"] == "baixa"
    assert combos[0]["impact_monthly"] is None


def test_finds_the_pair_that_already_sells_together(store):
    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    combo = next(s for s in resultado["suggestions"] if s["type"] == "combo" and s["id"].startswith("combo-"))
    assert "Açaí 500 ml" in combo["title"] and "Super Bowl" in combo["title"]
    # O preco do combo tem de ficar entre o custo somado e o preco cheio.
    preco_cheio = PRECOS["acai-500"] + PRECOS["super-bowl"]
    valores = {m["label"]: m["value"] for m in combo["metrics"]}
    assert valores["Preço separado"] == "R$ 53,00"
    assert float(valores["Preço do combo"].replace("R$ ", "").replace(".", "").replace(",", ".")) < preco_cheio
    assert combo["confidence"] == "alta"


def test_every_quantified_suggestion_states_its_premise(store):
    """Impacto sem premissa escrita e chute com cara de projecao."""

    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    quantificadas = [s for s in resultado["suggestions"] if s["impact_monthly"] is not None]
    assert quantificadas, "o histórico plantado precisa gerar pelo menos uma sugestão com número"
    for sugestao in quantificadas:
        assert sugestao["impact_note"], f"{sugestao['id']} tem impacto sem premissa"
        assert sugestao["impact_monthly"] > 0


def test_every_suggestion_carries_evidence_and_an_action(store):
    _loja_movimentada(store)

    for sugestao in GrowthService(store).opportunities(90)["suggestions"]:
        assert sugestao["evidence"], f"{sugestao['id']} sem evidência"
        assert sugestao["action"], f"{sugestao['id']} sem ação"
        assert sugestao["confidence"] in ("alta", "media", "baixa")
        assert sugestao["confidence_note"], f"{sugestao['id']} sem justificar a confiança"


def test_spots_the_empty_weekday(store):
    """Dia vazio e aluguel pago sem contrapartida: e a promocao mais barata."""

    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    dia_fraco = next(s for s in resultado["suggestions"] if s["id"] == "marketing-dia-fraco")
    assert "Terça" in dia_fraco["title"]
    assert dia_fraco["impact_monthly"] > 0


def test_quantifies_what_the_marketplace_took(store):
    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    canal = next(s for s in resultado["suggestions"] if s["id"].startswith("canal-taxa"))
    assert "iFood" in canal["title"]
    assert canal["impact_monthly"] > 0
    # O ganho da migração é a comissão economizada menos o desconto dado.
    assert "30%" in canal["impact_note"] and "5%" in canal["impact_note"]


def test_flags_capital_parked_in_the_freezer(store):
    """Insumo que nenhuma receita puxa e dinheiro imobilizado, nao estoque."""

    store.put("inventory", "stock-parado", {
        "id": "stock-parado", "name": "Pasta de amendoim", "unit": "kg",
        "quantity": 4, "minimum_quantity": 0, "unit_cost": 38.0, "category": "Complementos",
    })
    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    parado = next(s for s in resultado["suggestions"] if s["id"] == "produto-insumo-stock-parado")
    assert "Pasta de amendoim" in parado["title"]
    assert "R$ 152,00" in parado["impact_note"]
    assert parado["confidence"] == "alta"


def test_reads_the_missing_size_from_the_existing_ladder(store):
    """500 ml some do cardapio: a escada 300/700 denuncia o vao."""

    store.delete("products", "acai-500")
    _loja_movimentada(store)

    resultado = GrowthService(store).opportunities(90)

    lacuna = next((s for s in resultado["suggestions"] if s["id"].startswith("produto-tamanho")), None)
    assert lacuna is not None
    assert "500 ml" in lacuna["title"]
    # Preço e custo saem da proporção da receita, não de palpite.
    assert lacuna["impact_monthly"] is None
    assert "simulador" in lacuna["impact_note"].lower()


def test_says_what_is_blocking_better_suggestions(store):
    """Dizer que a cesta enxerga so parte dos pedidos vale tanto quanto sugerir."""

    _loja_movimentada(store)
    # Lote importado da Anota Aí: faturamento conhecido, itens desconhecidos.
    for indice in range(40):
        store.put("orders", f"importado-{indice}", {
            "id": f"importado-{indice}", "number": 9000 + indice,
            "created_at": (datetime.now(timezone.utc) - timedelta(days=indice % 20)).isoformat(),
            "channel": "Delivery", "payment_method": "Pix", "customer_name": None,
            "discount": 0, "total": 45.0, "cost": None, "status": "Concluido", "items": [],
        })

    resultado = GrowthService(store).opportunities(90)

    titulos = [b["title"] for b in resultado["blockers"]]
    assert any("itens detalhados" in titulo for titulo in titulos)
    assert any("Base de clientes" in titulo for titulo in titulos)
    assert resultado["context"]["detail_coverage"] < 100


def test_suggests_a_club_only_when_recurrence_exists(store):
    """Clube sem base recorrente e produto sem cliente."""

    _loja_movimentada(store)
    sem_base = GrowthService(store).opportunities(90)
    assert not any(s["id"] == "servico-clube" for s in sem_base["suggestions"])

    for numero in range(9000, 9012):
        _pedido(store, numero, numero % 30, [("acai-500", 1)], cliente=f"Cliente {numero % 4}")

    com_base = GrowthService(store).opportunities(90)
    clube = next(s for s in com_base["suggestions"] if s["id"] == "servico-clube")
    assert clube["impact_monthly"] > 0
    assert "30%" in clube["impact_note"]


def test_ranks_by_impact_and_keeps_unquantified_last(store):
    _loja_movimentada(store)

    sugestoes = GrowthService(store).opportunities(90)["suggestions"]

    valores = [s["impact_monthly"] for s in sugestoes]
    com_numero = [v for v in valores if v is not None]
    assert com_numero == sorted(com_numero, reverse=True)
    # Quem não tem número não some — parte do que importa não é quantificável.
    assert valores.index(None) == len(com_numero) if None in valores else True
    assert [s["rank"] for s in sugestoes] == list(range(1, len(sugestoes) + 1))
