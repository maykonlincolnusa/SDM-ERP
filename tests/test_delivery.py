"""Entregadores, turnos e o custo da entrega.

A pergunta que o modulo responde nao e "quantas entregas o Joao fez", e sim
**a entrega se paga**. Os testes cobrem as duas metades disso: a conta do
acerto individual, que precisa fechar ate o centavo porque vira dinheiro na
mao de alguem, e o cruzamento com os pedidos, que precisa acusar divergencia
em vez de escondê-la.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    CourierCreate,
    CourierUpdate,
    DeliveryEntryCreate,
    DeliveryEntryUpdate,
    OrderCreate,
    OrderLine,
    OrderUpdate,
)
from app.services import DeliveryService, ERPService
from app.services.erp import FUSO_LOJA


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    return ERPService(store), DeliveryService(store)


def _hoje() -> str:
    return datetime.now(FUSO_LOJA).date().isoformat()


def _entregador(delivery, **kwargs):
    base = {"name": "João", "fee_per_delivery": 3.0}
    base.update(kwargs)
    return delivery.create_courier(CourierCreate(**base))


# ----------------------------------------------------------------------
# Balcão é a maioria
# ----------------------------------------------------------------------


def test_a_counter_sale_is_not_a_delivery(loja):
    """A maioria dos pedidos e balcao e nao pode entrar no acerto do entregador."""

    erp, delivery = loja
    balcao = erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))

    assert balcao["is_delivery"] is False
    assert balcao["delivery_fee"] == 0
    assert delivery.performance(30)["reconciliation"]["delivery_orders"] == 0


def test_a_free_delivery_still_counts_as_a_delivery(loja):
    """Frete gratis existe. Deduzir "e entrega" de frete maior que zero deixaria
    esses pedidos de fora do acerto — e alguem entregou assim mesmo."""

    erp, delivery = loja
    pedido = erp.create_order(OrderCreate(
        items=[OrderLine(product_id="acai-300", quantity=1)],
        is_delivery=True, delivery_fee=0,
    ))

    assert pedido["is_delivery"] is True
    assert delivery.performance(30)["reconciliation"]["delivery_orders"] == 1


def test_freight_enters_the_total_but_not_the_product_revenue(loja):
    """Frete e dinheiro que entra, mas nao e faturamento de produto.

    Somar os dois faria o delivery parecer crescimento de venda quando foi so
    mais gente pedindo em casa — e o frete ainda tem de pagar o entregador.
    """

    erp, _ = loja
    pedido = erp.create_order(OrderCreate(
        items=[OrderLine(product_id="acai-300", quantity=1)],
        is_delivery=True, delivery_fee=6.0,
    ))

    assert pedido["total"] == 22.0, "16 do produto mais 6 de frete"
    metricas = erp.dashboard(7)["metrics"]
    assert metricas["delivery_fee"] == 6.0
    assert metricas["product_revenue"] == 16.0
    assert metricas["delivery_orders"] == 1


def test_dashboard_separates_delivery_from_counter(loja):
    erp, _ = loja
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True, delivery_fee=5))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))

    metricas = erp.dashboard(7)["metrics"]

    assert metricas["delivery_orders"] == 1
    assert metricas["delivery_share"] == pytest.approx(33.3, abs=0.1)


# ----------------------------------------------------------------------
# A conta do acerto
# ----------------------------------------------------------------------


def test_pay_components_add_up_instead_of_excluding_each_other(loja):
    """Diaria mais valor por entrega mais comissao: e assim que a loja combina.

    Forcar um modelo unico faria o resto ser lancado "no bonus", que e onde o
    controle se perde.
    """

    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=2.5, commission_percent=10, daily_rate=40)

    turno = delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=12, freight_total=60.0,
    ))

    # 12 × 2,50 = 30 | 10% de 60 = 6 | diária 40 → 76
    assert turno["payout"] == 76.0
    assert turno["calculated_payout"] == 76.0
    assert [parte["value"] for parte in turno["breakdown"]] == [30.0, 6.0, 40.0]


def test_a_day_off_does_not_pay_the_daily_rate(loja):
    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=0, daily_rate=50)

    turno = delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=0, worked=False,
    ))

    assert turno["payout"] == 0.0


def test_bonus_and_deduction_move_the_payout(loja):
    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=3)

    turno = delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=10, bonus=15, deduction=5,
    ))

    assert turno["payout"] == 40.0  # 30 + 15 - 5


def test_overriding_the_payout_requires_a_reason(loja):
    """Pagar diferente do combinado pode; pagar sem explicar, nao.

    E o unico ponto do modulo em que o numero deixa de sair da regra. Sem o
    motivo ao lado, o acerto para de ser auditavel exatamente onde mais precisa.
    """

    _, delivery = loja
    joao = _entregador(delivery)

    with pytest.raises(ValueError, match="motivo"):
        DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=5, payout_override=99)

    turno = delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=5, payout_override=99,
        note="acerto de dívida anterior",
    ))
    assert turno["payout"] == 99.0
    assert turno["calculated_payout"] == 15.0
    assert turno["overridden"] is True


def test_changing_the_pay_deal_does_not_rewrite_the_past(loja):
    """Turno ja lancado guarda o que foi combinado no dia? Nao — ele recalcula.

    A escolha e deliberada: o combinado vive no cadastro, e corrigir a diaria
    que estava errada precisa corrigir os turnos daquele acordo. O que protege
    o historico e a trilha de auditoria, que registra a mudanca.
    """

    erp, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=2.0)
    turno = delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=10))
    assert turno["payout"] == 20.0

    delivery.update_courier(joao["id"], CourierUpdate(fee_per_delivery=3.0))

    assert delivery.entries(30)[0]["payout"] == 30.0
    trilha = erp.audit("courier", joao["id"])
    assert trilha[0]["changes"]["fee_per_delivery"] == {"de": 2.0, "para": 3.0}


def test_a_shift_cannot_be_logged_in_the_future(loja):
    _, delivery = loja
    joao = _entregador(delivery)
    amanha = (datetime.now(FUSO_LOJA).date() + timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match="futuro"):
        delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=amanha, deliveries=1))


# ----------------------------------------------------------------------
# Edição — tudo pode ser corrigido
# ----------------------------------------------------------------------


def test_every_field_of_a_shift_can_be_corrected(loja):
    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=3)
    turno = delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=8, freight_total=40))

    corrigido = delivery.update_entry(turno["id"], DeliveryEntryUpdate(deliveries=11, freight_total=55, bonus=10))

    assert (corrigido["deliveries"], corrigido["freight_total"], corrigido["bonus"]) == (11, 55, 10)
    assert corrigido["payout"] == 43.0  # 11 × 3 + 10


def test_an_override_can_be_removed_and_the_rule_comes_back(loja):
    """Tirar a excecao devolve o numero da regra, sem precisar refazer o turno."""

    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=3)
    turno = delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=10, payout_override=50, note="adiantamento",
    ))
    assert turno["payout"] == 50.0

    voltou = delivery.update_entry(turno["id"], DeliveryEntryUpdate(payout_override=None))

    assert voltou["payout"] == 30.0
    assert voltou["overridden"] is False


def test_a_shift_can_be_deleted(loja):
    _, delivery = loja
    joao = _entregador(delivery)
    turno = delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=4))

    delivery.delete_entry(turno["id"])

    assert delivery.entries(30) == []


def test_a_courier_with_history_is_deactivated_not_deleted(loja):
    """Apagar deixaria turnos pagos sem dono e o custo da entrega sem historico."""

    _, delivery = loja
    joao = _entregador(delivery)
    delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=3))

    resultado = delivery.delete_courier(joao["id"])

    assert resultado["status"] == "inativado"
    assert delivery.couriers()[0]["active"] is False


def test_an_unused_courier_is_deleted(loja):
    _, delivery = loja
    joao = _entregador(delivery)

    assert delivery.delete_courier(joao["id"])["status"] == "excluido"
    assert delivery.couriers() == []


# ----------------------------------------------------------------------
# Acompanhamento
# ----------------------------------------------------------------------


def test_performance_answers_whether_delivery_pays_for_itself(loja):
    """A conta que decide: o frete cobre o entregador, ou a margem banca?"""

    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=5.0)
    delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=10, freight_total=30.0,
    ))

    totais = delivery.performance(30)["totals"]

    assert totais["deliveries"] == 10
    assert totais["freight"] == 30.0
    assert totais["payout"] == 50.0
    assert totais["result"] == -20.0, "o frete não cobriu a entrega"
    assert totais["cost_per_delivery"] == 5.0
    assert totais["coverage"] == 60.0
    leituras = delivery.performance(30)["readings"]
    assert any("não cobre" in leitura["title"] for leitura in leituras)


def test_performance_recognises_delivery_that_pays_off(loja):
    _, delivery = loja
    joao = _entregador(delivery, fee_per_delivery=3.0)
    delivery.create_entry(DeliveryEntryCreate(
        courier_id=joao["id"], date=_hoje(), deliveries=10, freight_total=60.0,
    ))

    resultado = delivery.performance(30)

    assert resultado["totals"]["result"] == 30.0
    assert any("se paga" in leitura["title"] for leitura in resultado["readings"])


def test_monthly_salary_is_prorated_into_the_period_cost(loja):
    """Salario fixo nao aparece em turno nenhum, mas e custo da entrega.

    Deixar de fora faria a entrega parecer mais barata do que e.
    """

    _, delivery = loja
    mensalista = _entregador(delivery, name="Carlos", fee_per_delivery=0, monthly_salary=1500)
    delivery.create_entry(DeliveryEntryCreate(courier_id=mensalista["id"], date=_hoje(), deliveries=20))

    linha = next(row for row in delivery.performance(30)["couriers"] if row["courier_id"] == mensalista["id"])

    assert linha["salary"] == 1500.0
    assert linha["payout"] == 1500.0
    assert linha["cost_per_delivery"] == 75.0


def test_reconciliation_flags_delivery_orders_without_a_courier(loja):
    """Divergencia quase nunca e problema de pessoa: e pedido sem entregador.

    Mas precisa ficar visivel, senao o acerto do fim do mes vira palavra contra
    palavra.
    """

    erp, delivery = loja
    joao = _entregador(delivery)
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True, delivery_fee=5))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True, delivery_fee=5, courier_id=joao["id"]))
    delivery.create_entry(DeliveryEntryCreate(courier_id=joao["id"], date=_hoje(), deliveries=2, freight_total=10))

    resultado = delivery.performance(30)

    conferencia = resultado["reconciliation"]
    assert conferencia["delivery_orders"] == 2
    assert conferencia["orders_without_courier"] == 1
    assert any("sem entregador" in leitura["title"] for leitura in resultado["readings"])


def test_the_shift_form_can_be_prefilled_from_the_orders(loja):
    """Digitar de memoria no fim do expediente e onde o numero se perde."""

    erp, delivery = loja
    joao = _entregador(delivery)
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True, delivery_fee=7, courier_id=joao["id"]))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True, delivery_fee=8, courier_id=joao["id"]))
    erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))

    sugestao = delivery.suggest(joao["id"], _hoje())

    assert sugestao["deliveries"] == 2, "a venda de balcão não pode entrar"
    assert sugestao["freight_total"] == 15.0


def test_an_order_cannot_point_to_a_courier_that_does_not_exist(loja):
    erp, _ = loja
    pedido = erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], is_delivery=True))

    with pytest.raises(ValueError, match="Entregador não encontrado"):
        erp.update_order(pedido["id"], OrderUpdate(courier_id="courier-fantasma"))
