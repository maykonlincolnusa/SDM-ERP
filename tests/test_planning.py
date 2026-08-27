"""Planejamento de producao e compra.

O valor deste modulo nao esta em acertar a demanda de amanha — ninguem acerta.
Esta em nao inventar precisao que os dados nao sustentam. Por isso a maior parte
dos testes aqui prova o que o plano *nao* faz: nao extrapola alem do observado,
nao deixa uma amostra pequena virar regra, nao supoe normalidade e nao conta dia
de loja fechada como demanda zero.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import OrderCreate, OrderLine
from app.services import ERPService, PlanningService
from app.services.erp import FUSO_LOJA


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    for movimento in store.list("inventory_movements"):
        store.delete("inventory_movements", movimento["id"])
    return ERPService(store), PlanningService(store)


def _vende(erp, dias_atras: int, copos: int, produto: str = "acai-500"):
    dia = datetime.now(FUSO_LOJA).date() - timedelta(days=dias_atras)
    quando = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
    erp.create_order(OrderCreate(items=[OrderLine(product_id=produto, quantity=copos)], occurred_at=quando))


def _historico(erp, dias: int = 60, copos: int = 4):
    for atras in range(dias, 0, -1):
        _vende(erp, atras, copos)


# ----------------------------------------------------------------------
# O nucleo: quantil empirico
# ----------------------------------------------------------------------


def test_quantile_never_extrapolates_beyond_what_was_observed(loja):
    """O plano nunca sugere um dia maior do que qualquer dia ja visto.

    E a diferenca entre quantil empirico e distribuicao ajustada: uma normal
    com cauda infinita sempre devolve um numero acima do maximo observado, e
    esse numero nao tem lastro nenhum.
    """

    _, planning = loja
    amostra = [1.0, 2.0, 3.0, 4.0, 100.0]

    assert planning._quantil(amostra, 0.99) <= max(amostra)
    assert planning._quantil(amostra, 1.0) == max(amostra)
    assert planning._quantil(amostra, 0.0) == min(amostra)
    assert planning._quantil(amostra, 0.5) == 3.0


def test_quantile_interpolates_between_observations(loja):
    _, planning = loja

    # Entre a 2a e a 3a observacao de quatro: posicao 0.5*(4-1) = 1.5.
    assert planning._quantil([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0


def test_quantile_of_a_single_observation_is_that_observation(loja):
    """Uma observacao nao vira distribuicao. O plano devolve o que viu."""

    _, planning = loja

    assert planning._quantil([7.0], 0.9) == 7.0
    assert planning._quantil([], 0.9) == 0.0


# ----------------------------------------------------------------------
# Encolhimento: o que impede a amostra pequena de virar regra
# ----------------------------------------------------------------------


def test_a_weekday_seen_once_barely_moves_the_plan(loja):
    """Um sabado atipico nao pode virar regra permanente.

    Sem encolhimento, uma unica observacao 3x acima da media fixaria o fator em
    3,0 e o plano mandaria preparar o triplo em todo sabado seguinte.
    """

    _, planning = loja
    hoje = datetime.now(FUSO_LOJA).date()
    # Serie plana de 1,0, com um unico sabado de 3,0.
    serie = []
    for atras in range(21, 0, -1):
        dia = hoje - timedelta(days=atras)
        serie.append((dia, 3.0 if (dia.weekday() == 5 and atras <= 7) else 1.0))

    fator, amostras = planning._fator_do_dia(serie, 5)

    assert amostras == 3, "tres sabados na janela de 21 dias"
    bruto = (3.0 + 1.0 + 1.0) / 3 / (sum(v for _, v in serie) / len(serie))
    assert fator < bruto, "o fator bruto precisa ser puxado de volta para 1"
    assert 1.0 < fator < 1.5, f"encolhimento deixou o fator solto demais: {fator}"


def test_shrinkage_relaxes_as_the_sample_grows(loja):
    """Com historico grande, o fator pode se afastar de 1 com seguranca."""

    _, planning = loja
    hoje = datetime.now(FUSO_LOJA).date()

    def fator_com(semanas: int) -> float:
        serie = []
        for atras in range(semanas * 7, 0, -1):
            dia = hoje - timedelta(days=atras)
            serie.append((dia, 2.0 if dia.weekday() == 5 else 1.0))
        return planning._fator_do_dia(serie, 5)[0]

    curto, longo = fator_com(3), fator_com(30)

    assert curto < longo, "amostra maior precisa deixar o fator chegar mais perto do bruto"
    assert longo < 2.0, "nem com historico longo o fator ultrapassa o bruto"


def test_a_weekday_never_seen_gets_a_neutral_factor(loja):
    """Sem observacao daquele dia, o plano nao chuta: usa a media geral."""

    _, planning = loja
    hoje = datetime.now(FUSO_LOJA).date()
    serie = [(hoje - timedelta(days=atras), 1.0) for atras in range(21, 0, -1) if (hoje - timedelta(days=atras)).weekday() != 5]

    fator, amostras = planning._fator_do_dia(serie, 5)

    assert (fator, amostras) == (1.0, 0)


# ----------------------------------------------------------------------
# Prazo de entrega: convolucao empirica, sem normalidade
# ----------------------------------------------------------------------


def test_lead_time_demand_uses_observed_windows_not_a_normal_curve(loja):
    _, planning = loja

    janelas = planning._somas_moveis([1.0, 2.0, 3.0, 4.0], 2)

    assert janelas == [3.0, 5.0, 7.0]


def test_lead_time_longer_than_the_history_returns_nothing(loja):
    """Sem janela observada, o plano diz que nao tem — nao extrapola."""

    _, planning = loja

    assert planning._somas_moveis([1.0, 2.0], 5) == []
    assert planning._somas_moveis([1.0, 2.0], 1) == [1.0, 2.0]


# ----------------------------------------------------------------------
# Dia fechado nao e demanda zero
# ----------------------------------------------------------------------


def test_closed_days_are_left_out_of_the_sample(loja):
    """Domingo fechado nao e "vendeu zero".

    Contar o dia fechado rebaixaria todo quantil e o plano sugeriria menos do
    que a loja precisa nos dias em que ela abre.
    """

    erp, planning = loja
    hoje = datetime.now(FUSO_LOJA).date()
    # A janela de 30 dias comeca em hoje-29; vender em hoje-30 cairia fora dela.
    janela = [hoje - timedelta(days=atras) for atras in range(29, 0, -1)]
    abertos = [dia for dia in janela if dia.weekday() != 6]  # loja fecha domingo
    for dia in abertos:
        _vende(erp, (hoje - dia).days, 4)

    plano = planning.plan(days=30)

    assert plano["open_days"] == len(abertos)
    assert len(abertos) < len(janela), "o teste so prova algo se houver domingo fechado na janela"
    acai = next(row for row in plano["production"] if row["inventory_id"] == "acai")
    # Todos os dias abertos venderam o mesmo, entao a mediana e exatamente isso.
    assert acai["median"] == pytest.approx(0.46 * 4, abs=0.01)


# ----------------------------------------------------------------------
# Newsvendor: a razao critica decide, nao o gosto
# ----------------------------------------------------------------------


def test_cheap_ingredient_that_unlocks_a_big_margin_gets_a_high_service_level(loja):
    """Banana e barata e destrava a margem do copo inteiro: vale ter sobrando.

    Acai e caro perto do que rende, entao a razao critica dele e menor. E o
    modelo dizendo, com numero, o que a intuicao diria com sorte.
    """

    erp, planning = loja
    _historico(erp, dias=40)

    plano = planning.plan(days=60)
    acai = next(row for row in plano["production"] if row["inventory_id"] == "acai")
    banana = next(row for row in plano["production"] if row["inventory_id"] == "banana")

    assert banana["critical_ratio"] > acai["critical_ratio"]
    assert banana["critical_ratio"] > 80


def test_shelf_life_dilutes_the_cost_of_leftovers(loja):
    """O que sobra hoje e dura tres dias nao custa o insumo inteiro.

    Cobrar o custo cheio faria o plano preparar de menos todo dia, com medo de
    uma perda que so acontece se o insumo vencer.
    """

    erp, planning = loja
    _historico(erp, dias=40)
    curto = next(r for r in planning.plan(days=60)["production"] if r["inventory_id"] == "acai")

    item = erp.store.get("inventory", "acai")
    erp.store.put("inventory", "acai", {**item, "shelf_life_days": 1})
    longo = next(r for r in planning.plan(days=60)["production"] if r["inventory_id"] == "acai")

    assert curto["critical_ratio"] > longo["critical_ratio"], (
        "validade maior dilui o custo de sobra e permite preparar mais"
    )


def test_only_perishable_items_enter_the_production_plan(loja):
    """Copo descartavel nao tem o que decidir de manha."""

    erp, planning = loja
    _historico(erp, dias=40)

    plano = planning.plan(days=60)

    ids = {row["inventory_id"] for row in plano["production"]}
    assert "acai" in ids and "banana" in ids
    assert "copo-500" not in ids, "embalagem não perece; ela vive de ponto de pedido"
    assert any(row["inventory_id"] == "copo-500" for row in plano["purchase"])


# ----------------------------------------------------------------------
# Ponto de pedido
# ----------------------------------------------------------------------


def test_reorder_point_covers_the_supplier_lead_time(loja):
    erp, planning = loja
    _historico(erp, dias=40)

    compra = next(row for row in planning.plan(days=60)["purchase"] if row["inventory_id"] == "acai")

    assert compra["lead_time_days"] == 3
    # Consumo constante de 0,46*4 por dia durante 3 dias de prazo.
    assert compra["reorder_point"] == pytest.approx(0.46 * 4 * 3, abs=0.05)
    assert compra["suggested_minimum"] == compra["reorder_point"]


def test_a_steady_history_needs_almost_no_safety_stock(loja):
    """Estoque de seguranca e o preco da incerteza. Sem incerteza, ele some."""

    erp, planning = loja
    _historico(erp, dias=40, copos=4)

    compra = next(row for row in planning.plan(days=60)["purchase"] if row["inventory_id"] == "acai")

    assert compra["safety_stock"] == pytest.approx(0, abs=0.01)


def test_an_erratic_history_raises_the_safety_stock(loja):
    erp, planning = loja
    for atras in range(40, 0, -1):
        _vende(erp, atras, 1 if atras % 2 else 9)

    compra = next(row for row in planning.plan(days=60)["purchase"] if row["inventory_id"] == "acai")

    assert compra["safety_stock"] > 0, "demanda que oscila precisa de folga"


# ----------------------------------------------------------------------
# Amostra insuficiente e transparencia
# ----------------------------------------------------------------------


def test_an_item_without_enough_history_is_left_out(loja):
    """Menos que o minimo de observacoes nao vira plano — vira pendencia."""

    erp, planning = loja
    _historico(erp, dias=3)

    plano = planning.plan(days=30)

    assert plano["production"] == []
    assert any("dia(s) de consumo" in b["title"] for b in plano["blockers"])


def test_the_plan_explains_why_it_fits_nothing(loja):
    erp, planning = loja
    _historico(erp, dias=40)

    metodo = planning.plan(days=60)["method"]

    assert "Nenhuma distribuição é ajustada" in metodo["why_no_fit"]
    assert metodo["shrinkage_k"] > 0
    assert "loja fechada" in metodo["summary"]


def test_abc_concentrates_cost_and_xyz_reads_variability(loja):
    erp, planning = loja
    _historico(erp, dias=40)

    curva = planning.plan(days=60)["abc"]

    assert curva[0]["abc"] == "A", "o insumo de maior custo abre a curva"
    assert curva[0]["cumulative"] <= 100
    assert all(row["xyz"] == "X" for row in curva), "consumo constante é previsível"
    assert curva[0]["routine"]
