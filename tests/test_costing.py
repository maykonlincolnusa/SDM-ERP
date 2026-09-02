"""Dossie de custo, simulacao e linha de marca.

A conta antiga — preco menos ficha tecnica — nao erra, mas responde pouco. Os
testes aqui fixam as tres perguntas que ela nao respondia: quanto e embalagem,
quanto sobra em cada canal e a partir de que preco o produto paga a estrutura.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import ExpenseCreate, OrderCreate, OrderLine, ProductSimulation, ProductUpdate, RecipeLine
from app.services import CostingService, ERPService
from app.services.erp import FUSO_LOJA


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    return ERPService(store), CostingService(store)


def test_dossier_separates_packaging_from_ingredients(loja):
    """Copo e insumo, mas nao e ingrediente.

    Sao alavancas diferentes: ingrediente escala com o tamanho do copo,
    embalagem escala com o numero de copos. Misturar os dois esconde qual
    fornecedor renegociar.
    """

    _, costing = loja

    dossie = costing.dossier("acai-500")

    assert dossie["cost"]["packaging"] == 0.88, "o copo 500 ml precisa cair em embalagem"
    assert dossie["cost"]["ingredients"] == round(dossie["cost"]["direct"] - 0.88, 2)
    copo = next(row for row in dossie["recipe"] if row["name"] == "Copo 500 ml")
    assert copo["packaging"] is True


def test_dossier_shows_what_is_left_in_each_channel(loja):
    """O mesmo produto rende menos no marketplace, e o dossie precisa dizer isso."""

    _, costing = loja

    dossie = costing.dossier("acai-500")
    balcao = next(row for row in dossie["channels"] if row["name"] == "Balcao")
    ifood = next(row for row in dossie["channels"] if row["name"] == "iFood")

    assert balcao["fee"] == 0
    assert ifood["fee"] == round(22.0 * 0.27, 2)
    assert ifood["contribution"] < balcao["contribution"]
    # Preco que devolveria no iFood a mesma sobra do balcao.
    assert ifood["price_to_match"] > 22.0


def test_dossier_flags_a_channel_that_loses_money(loja):
    """Taxa alta demais faz a venda dar prejuizo, e isso nao pode ficar implicito."""

    erp, costing = loja
    canal = next(row for row in erp.channels() if row["name"] == "iFood")
    erp.store.put("channels", canal["id"], {**canal, "fee_percent": 90.0})

    dossie = costing.dossier("acai-500")

    ifood = next(row for row in dossie["channels"] if row["name"] == "iFood")
    assert ifood["profitable"] is False
    assert any(alerta["title"] == "Canal no prejuízo" for alerta in dossie["alerts"])


def test_dossier_allocates_fixed_cost_only_with_real_volume(loja):
    """Sem venda no periodo nao ha base para ratear — e ai o campo fica nulo.

    Dividir a estrutura por um denominador inventado daria um custo cheio com
    cara de preciso e sem nenhum lastro.
    """

    erp, costing = loja
    erp.create_expense(ExpenseCreate(description="Aluguel", category="Fixo", amount=1800, due_date="2026-09-05"))

    sem_venda = costing.dossier("super-bowl", days=1)
    assert sem_venda["cost"]["fixed_per_unit"] is None
    assert sem_venda["price_analysis"]["net_margin"] is None

    erp.create_order(OrderCreate(items=[OrderLine(product_id="super-bowl", quantity=10)]))
    com_venda = costing.dossier("super-bowl", days=1)
    assert com_venda["cost"]["fixed_per_unit"] > 0
    assert com_venda["price_analysis"]["net_margin"] < com_venda["price_analysis"]["gross_margin"]


def test_dossier_suggests_the_price_for_a_target_margin(loja):
    _, costing = loja

    dossie = costing.dossier("acai-500", target_margin=65)

    # O preco-alvo sai do custo em precisao cheia, nao do custo ja arredondado
    # para exibicao: arredondar duas vezes desloca o resultado em um centavo.
    custo = sum(row["cost"] for row in dossie["recipe"])
    assert dossie["target"]["price"] == round(custo / 0.35, 2)
    assert dossie["target"]["difference"] == round(dossie["target"]["price"] - 22.0, 2)


def test_dossier_warns_when_an_ingredient_has_no_cost(loja):
    """Custo zero nao e barato: e desconhecido, e infla a margem."""

    erp, costing = loja
    granola = erp.store.get("inventory", "granola")
    erp.store.put("inventory", "granola", {**granola, "unit_cost": 0})

    dossie = costing.dossier("acai-500")

    assert any("custo zero" in alerta["title"] for alerta in dossie["alerts"])


def test_simulation_refuses_a_price_below_cost(loja):
    """A conta do produto novo vem antes do cardapio impresso."""

    _, costing = loja

    resultado = costing.simulate(ProductSimulation(
        name="Açaí proteico 500 ml",
        price=5.0,
        recipe=[RecipeLine(inventory_id="acai", quantity=0.46), RecipeLine(inventory_id="copo-500", quantity=1)],
    ))

    assert resultado["verdict"] == "inviavel"
    assert resultado["contribution"] < 0


def test_simulation_accepts_an_ingredient_the_store_does_not_have_yet(loja):
    """Produto novo quase sempre traz insumo que a loja nunca comprou.

    Exigir o cadastro antes de saber se o produto se paga inverte a ordem da
    decisao: primeiro a conta, depois a compra.
    """

    _, costing = loja

    resultado = costing.simulate(ProductSimulation(
        name="Açaí com whey",
        price=32.0,
        recipe=[RecipeLine(inventory_id="acai", quantity=0.46), RecipeLine(inventory_id="copo-500", quantity=1)],
        extra_cost=6.5,
        extra_cost_label="Dose de whey",
        target_margin=60,
        expected_daily_units=8,
    ))

    assert resultado["cost"]["estimated_extra"] == 6.5
    assert any(row.get("estimated") for row in resultado["recipe"])
    assert resultado["target"]["price"] == round(resultado["cost"]["direct"] / 0.4, 2)
    assert resultado["projection"]["monthly_units"] == 240.0
    assert resultado["verdict"] in ("saudavel", "forte", "apertado", "atencao")


def test_simulation_needs_some_cost_to_run(loja):
    with pytest.raises(ValueError):
        ProductSimulation(price=20.0)


def test_line_performance_keeps_a_new_line_visible(loja):
    """A linha Sport comeca pequena; somada ao cardapio ela desaparece.

    Medir separado e o que permite decidir se ela cresce ou sai.
    """

    erp, costing = loja
    erp.update_product("super-bowl", ProductUpdate(line="Sport"))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="super-bowl", quantity=2)]))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=5)]))

    resultado = costing.line_performance(30)
    sport = next(row for row in resultado["lines"] if row["name"] == "Sport")

    assert sport["units"] >= 2
    assert sport["revenue"] > 0
    assert sport["margin"] > 0
    assert sport["share"] < 100, "a linha nova nao pode ocupar o resultado inteiro"
    assert sum(row["share"] for row in resultado["lines"]) == pytest.approx(100, abs=0.5)


def test_breakeven_reads_in_units_the_store_understands(loja):
    erp, costing = loja
    erp.create_expense(ExpenseCreate(description="Aluguel", category="Fixo", amount=1800, due_date="2026-09-05"))

    dossie = costing.dossier("acai-500")

    contribuicao = dossie["price_analysis"]["contribution"]
    estrutura = dossie["fixed"]["monthly"]
    assert estrutura >= 1800, "o aluguel lancado precisa entrar na estrutura"
    assert dossie["breakeven"]["units_per_month"] == round(estrutura / contribuicao, 1)
    assert dossie["breakeven"]["units_per_day"] == round(estrutura / contribuicao / 30, 1)


def test_structure_does_not_grow_with_each_month_recorded(loja):
    """Seis meses de aluguel sao seis meses, nao um aluguel seis vezes maior.

    Era o defeito mais caro do dossie: somar tudo que ja foi lancado fazia a
    estrutura crescer a cada mes de uso, e o ponto de equilibrio subia junto.
    O erro nao aparecia no primeiro mes — so quando a loja ja dependia do numero.
    """

    erp, costing = loja
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    # Seis lancamentos avulsos, um por mes, como quem digita a conta quando ela
    # chega. A categoria Fixo ja basta para entrar no rateio; marcar como
    # recorrente abriria uma serie, que e o caminho testado em
    # tests/test_expense_recurrence.py.
    for mes in range(3, 9):
        erp.create_expense(ExpenseCreate(
            description="Aluguel", category="Fixo", amount=1800,
            due_date=f"2026-0{mes}-05", status="Paga",
        ))

    fixo = costing.dossier("acai-500")["fixed"]

    assert fixo["monthly"] == 1800, "a estrutura mensal e a media dos meses, nao a soma deles"
    assert fixo["months"] == 6
    assert fixo["partial"] is False
    assert "03/2026" in fixo["expense_basis"] and "08/2026" in fixo["expense_basis"]


def test_structure_averages_the_closed_months(loja):
    """A luz de um mes quente nao pode virar a conta de luz de todo mes."""

    erp, costing = loja
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    erp.create_expense(ExpenseCreate(description="Energia", category="Fixo", amount=200, due_date="2026-07-10"))
    erp.create_expense(ExpenseCreate(description="Energia", category="Fixo", amount=400, due_date="2026-08-10"))

    fixo = costing.dossier("acai-500")["fixed"]

    assert fixo["monthly"] == 300
    assert fixo["expenses"] == 2


def test_open_month_is_used_only_when_no_month_has_closed(loja):
    """Primeiro mes de uso ainda merece um numero — desde que marcado como parcial."""

    erp, costing = loja
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    hoje = datetime.now(FUSO_LOJA).date()
    erp.create_expense(ExpenseCreate(
        description="Aluguel", category="Fixo", amount=1800,
        due_date=hoje.replace(day=1).isoformat(),
    ))

    fixo = costing.dossier("acai-500")["fixed"]

    assert (fixo["monthly"], fixo["months"], fixo["partial"]) == (1800, 0, True)
    assert any("ainda aberto" in alerta["title"] for alerta in costing.dossier("acai-500")["alerts"])


def test_closed_month_wins_over_the_open_one(loja):
    """Aberto o mes novo, a estrutura para de ser a soma dos dois."""

    erp, costing = loja
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    hoje = datetime.now(FUSO_LOJA).date()
    fechado = (hoje.replace(day=1) - timedelta(days=1)).replace(day=5)
    erp.create_expense(ExpenseCreate(description="Aluguel", category="Fixo", amount=1800, due_date=fechado.isoformat()))
    erp.create_expense(ExpenseCreate(description="Aluguel", category="Fixo", amount=1800, due_date=hoje.replace(day=1).isoformat()))

    fixo = costing.dossier("acai-500")["fixed"]

    assert fixo["monthly"] == 1800, "o mes corrente nao se soma ao fechado"
    assert fixo["partial"] is False


def test_only_fixed_expenses_enter_the_allocation(loja):
    """Ratear insumo seria contar o mesmo custo duas vezes."""

    erp, costing = loja
    antes = costing.dossier("acai-500")["fixed"]["monthly"]
    erp.create_expense(ExpenseCreate(description="Aluguel extra", category="Fixo", amount=1800, due_date="2026-09-05"))
    erp.create_expense(ExpenseCreate(description="Compra de polpa", category="Insumos", amount=900, due_date="2026-09-05"))

    dossie = costing.dossier("acai-500")

    # Sobe exatamente o valor da despesa fixa: os 900 de insumo ficam de fora,
    # senao o custo da polpa seria contado na ficha tecnica e de novo no rateio.
    assert dossie["fixed"]["monthly"] == round(antes + 1800, 2)
