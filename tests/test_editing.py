"""Edicao e estorno dos lancamentos.

O risco de abrir edicao num ERP nao e a tela: e o saldo. Toda correcao aqui
precisa mexer no estoque junto, senao o registro fica certo e a prateleira
fica errada — que e pior do que nao ter edicao nenhuma.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    ChannelUpdate,
    ExpenseCreate,
    ExpenseUpdate,
    InventoryAdjustment,
    InventoryCount,
    InventoryItemUpdate,
    MovementUpdate,
    OrderCreate,
    OrderLine,
    OrderUpdate,
    ProductUpdate,
)
from app.services import ERPService


@pytest.fixture
def service(tmp_path) -> ERPService:
    return ERPService(DemoStore(tmp_path / "store.json"))


def _saldo(service: ERPService, inventory_id: str) -> float:
    return next(item for item in service.inventory() if item["id"] == inventory_id)["quantity"]


# ----------------------------------------------------------------------
# Pedidos
# ----------------------------------------------------------------------


def test_editing_an_order_moves_the_stock_to_match(service):
    """Trocar a quantidade do pedido tem de devolver o insumo da versao antiga.

    Sem isso, corrigir 3 copos para 1 mantem a baixa dos 3 e o estoque vai
    afundando a cada correcao.
    """

    antes = _saldo(service, "acai")
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=3)]))
    assert _saldo(service, "acai") == round(antes - 0.46 * 3, 3)

    corrigido = service.update_order(order["id"], OrderUpdate(items=[OrderLine(product_id="acai-500", quantity=1)]))

    assert corrigido["total"] == 22.0
    assert _saldo(service, "acai") == round(antes - 0.46, 3)


def test_cancelling_an_order_returns_stock_and_leaves_revenue(service):
    antes = _saldo(service, "acai")
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=2)]))
    faturamento_com_pedido = service.dashboard(7)["metrics"]["revenue"]

    cancelado = service.cancel_order(order["id"], "cliente desistiu")

    assert cancelado["status"] == "Cancelado"
    assert _saldo(service, "acai") == antes, "o insumo precisa voltar para a prateleira"
    depois = service.dashboard(7)["metrics"]["revenue"]
    assert round(faturamento_com_pedido - depois, 2) == 44.0
    # O pedido continua na lista, para a loja saber que existiu e por que caiu.
    assert any(row["id"] == order["id"] for row in service.orders())


def test_cancelled_order_cannot_be_edited_or_cancelled_twice(service):
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))
    service.cancel_order(order["id"])

    with pytest.raises(ValueError, match="cancelado"):
        service.update_order(order["id"], OrderUpdate(items=[OrderLine(product_id="acai-300", quantity=2)]))
    with pytest.raises(ValueError, match="já está cancelado"):
        service.cancel_order(order["id"])


def test_order_can_be_registered_for_a_past_day(service):
    """Cadastro do movimento de ontem, que e como a loja realmente lanca."""

    ontem = datetime.now(timezone.utc) - timedelta(days=1)
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=2)], occurred_at=ontem))

    assert order["created_at"][:10] == ontem.date().isoformat()
    # registered_at guarda quando foi digitado: a diferenca entre os dois e o
    # que denuncia lancamento feito muito depois do fato.
    assert order["registered_at"][:10] == datetime.now(timezone.utc).date().isoformat()


def test_order_in_the_future_is_refused(service):
    with pytest.raises(ValueError, match="futuro"):
        service.create_order(OrderCreate(
            items=[OrderLine(product_id="acai-300", quantity=1)],
            occurred_at=datetime.now(timezone.utc) + timedelta(days=2),
        ))


def test_order_records_the_channel_fee(service):
    """iFood cobra comissao; o pedido precisa carregar quanto ficou pelo caminho."""

    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="iFood"))

    assert order["channel_fee"] == round(22.0 * 0.27, 2)
    assert service.dashboard(7)["metrics"]["net_revenue"] < service.dashboard(7)["metrics"]["revenue"]


def test_unregistered_channel_is_refused(service):
    with pytest.raises(ValueError, match="não está cadastrado"):
        service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="Rappi"))


def test_order_can_only_be_deleted_after_being_cancelled(service):
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))

    with pytest.raises(ValueError, match="Cancele o pedido antes"):
        service.delete_order(order["id"])

    service.cancel_order(order["id"])
    service.delete_order(order["id"])
    assert not any(row["id"] == order["id"] for row in service.orders())


# ----------------------------------------------------------------------
# Estoque e consumo
# ----------------------------------------------------------------------


def test_physical_count_records_the_difference(service):
    """A loja informa o que contou; o sistema calcula a diferenca."""

    resultado = service.count_inventory("granola", InventoryCount(quantity=3.0))

    assert resultado["previous_quantity"] == 3.8
    assert resultado["difference"] == -0.8
    assert _saldo(service, "granola") == 3.0
    perdas = [row for row in service.consumption(7)["history"] if row["inventory_id"] == "granola"]
    assert perdas and perdas[0]["quantity_delta"] == -0.8


def test_count_without_difference_writes_no_movement(service):
    antes = len(service.store.list("inventory_movements"))

    service.count_inventory("granola", InventoryCount(quantity=3.8))

    assert len(service.store.list("inventory_movements")) == antes, "contagem que confere nao e movimento"


def test_correcting_a_manual_movement_fixes_the_balance(service):
    """Lancou 5 kg quando foram 0,5: a correcao devolve a diferenca ao saldo."""

    antes = _saldo(service, "granola")
    service.adjust_inventory("granola", InventoryAdjustment(quantity_delta=-2.0, reason="perda"))
    movimento = next(row for row in service.store.list("inventory_movements") if row["reason"] == "perda")

    service.update_movement(movimento["id"], MovementUpdate(quantity_delta=-0.5, reason="perda corrigida"))

    assert _saldo(service, "granola") == round(antes - 0.5, 3)


def test_undoing_a_manual_movement_returns_the_balance(service):
    antes = _saldo(service, "banana")
    service.adjust_inventory("banana", InventoryAdjustment(quantity_delta=-1.5, reason="degustação"))
    movimento = next(row for row in service.store.list("inventory_movements") if row["reason"] == "degustação")

    service.delete_movement(movimento["id"])

    assert _saldo(service, "banana") == antes


def test_sale_movements_are_not_editable_by_hand(service):
    """Baixa de venda so muda pelo pedido, senao pedido e estoque se separam."""

    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))
    venda = next(row for row in service.store.list("inventory_movements") if row["reason"] == "Venda registrada")

    with pytest.raises(ValueError, match="Edite o pedido"):
        service.update_movement(venda["id"], MovementUpdate(quantity_delta=-0.1))
    with pytest.raises(ValueError, match="Cancele o pedido"):
        service.delete_movement(venda["id"])


def test_reversal_movements_are_not_editable_by_hand(service):
    """Estorno existe porque um pedido foi cancelado.

    Editar o estorno por fora descolaria o estoque do pedido que o gerou, e a
    diferenca so apareceria na proxima contagem — sem nada explicando de onde
    veio.
    """

    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))
    service.cancel_order(order["id"])
    estorno = next(row for row in service.store.list("inventory_movements") if row["reason"].startswith("Estorno"))

    with pytest.raises(ValueError, match="estorno"):
        service.update_movement(estorno["id"], MovementUpdate(quantity_delta=-0.1))
    with pytest.raises(ValueError, match="estorno"):
        service.delete_movement(estorno["id"])


def test_editing_an_order_is_not_reported_as_loss(service):
    """Estorno de edicao nao pode aparecer como insumo jogado fora."""

    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=4)]))
    service.update_order(order["id"], OrderUpdate(items=[OrderLine(product_id="acai-500", quantity=1)]))

    assert service.consumption(7)["totals"]["loss_cost"] == 0.0


def test_inventory_item_can_be_edited_but_not_its_balance(service):
    atualizado = service.update_inventory_item("granola", InventoryItemUpdate(unit_cost=31.0, minimum_quantity=2.5))

    assert atualizado["unit_cost"] == 31.0
    assert atualizado["minimum_quantity"] == 2.5
    assert atualizado["quantity"] == 3.8, "saldo so muda por ajuste ou contagem"
    # O custo do produto acompanha o custo do insumo, sem tocar no cardapio.
    produto = next(item for item in service.products() if item["id"] == "acai-500")
    assert produto["cost"] == round(0.46 * 22.5 + 0.03 * 13.8 + 0.1 * 6.9 + 0.055 * 31.0 + 1 * 0.88, 2)


def test_inventory_item_in_a_recipe_cannot_be_deleted(service):
    with pytest.raises(ValueError, match="ficha técnica"):
        service.delete_inventory_item("granola")


def test_unused_inventory_item_is_deleted(service):
    service.store.put("inventory", "stock-avulso", {
        "id": "stock-avulso", "name": "Insumo avulso", "unit": "kg",
        "quantity": 0, "minimum_quantity": 0, "unit_cost": 1.0, "category": "Insumos",
    })

    service.delete_inventory_item("stock-avulso")

    assert service.store.get("inventory", "stock-avulso") is None


# ----------------------------------------------------------------------
# Despesas, produtos e canais
# ----------------------------------------------------------------------


def test_expense_is_edited_and_deleted(service):
    despesa = service.create_expense(ExpenseCreate(description="Gelo", category="Insumos", amount=80, due_date="2026-09-01"))

    editada = service.update_expense(despesa["id"], ExpenseUpdate(amount=95.5, status="Paga"))
    assert (editada["amount"], editada["status"]) == (95.5, "Paga")

    service.delete_expense(despesa["id"])
    assert not any(row["id"] == despesa["id"] for row in service.expenses())


def test_product_with_sales_is_deactivated_instead_of_deleted(service):
    """Apagar produto vendido quebraria o historico que a previsao le."""

    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)]))

    resultado = service.delete_product("acai-300")

    assert resultado["status"] == "inativado"
    assert service.store.get("products", "acai-300")["active"] is False


def test_product_edit_keeps_untouched_fields(service):
    editado = service.update_product("acai-300", ProductUpdate(price=17.5, line="Sport"))

    assert editado["price"] == 17.5
    assert editado["line"] == "Sport"
    assert editado["recipe"], "a ficha tecnica nao pode se perder ao mudar o preco"


def test_channel_fee_can_be_corrected(service):
    canal = next(row for row in service.channels() if row["name"] == "iFood")

    atualizado = service.update_channel(canal["id"], ChannelUpdate(fee_percent=23.0))

    assert atualizado["fee_percent"] == 23.0
    pedido = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="iFood"))
    assert pedido["channel_fee"] == round(22.0 * 0.23, 2)


def test_channel_performance_ranks_by_what_is_left(service):
    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="iFood"))
    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)], channel="Balcao"))

    resultado = service.channel_performance(30)
    ifood = next(row for row in resultado["channels"] if row["name"] == "iFood")
    balcao = next(row for row in resultado["channels"] if row["name"] == "Balcao")

    assert ifood["fee"] > 0 and balcao["fee"] == 0
    assert ifood["net_revenue"] < balcao["net_revenue"], "mesma receita, menos dinheiro no bolso"


# ----------------------------------------------------------------------
# Rastro
# ----------------------------------------------------------------------


def test_every_change_leaves_a_trail(service):
    despesa = service.create_expense(ExpenseCreate(description="Energia", category="Fixo", amount=300, due_date="2026-09-05"))
    service.update_expense(despesa["id"], ExpenseUpdate(amount=412.9))

    trilha = service.audit("expense", despesa["id"])

    assert [row["action"] for row in trilha] == ["editado", "criado"]
    assert trilha[0]["changes"]["amount"] == {"de": 300.0, "para": 412.9}


def test_daily_closing_separates_the_day_from_the_period(service):
    """O fechamento responde "como foi hoje", não "como foram os sete dias"."""

    antes = service.sales_day()["totals"]
    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=2)], channel="iFood"))

    depois = service.sales_day()["totals"]

    assert round(depois["revenue"] - antes["revenue"], 2) == 44.0
    assert depois["orders"] == antes["orders"] + 1
    # A taxa do canal e o que separa faturamento de dinheiro que fica na loja.
    assert round(depois["channel_fee"] - antes["channel_fee"], 2) == round(44.0 * 0.27, 2)
    assert depois["net_revenue"] == round(depois["revenue"] - depois["channel_fee"], 2)


def test_daily_closing_uses_the_store_day_not_utc(service):
    """Venda das 21h no Brasil pertence ao dia de hoje, nao ao de amanha.

    Em UTC ela cai no dia seguinte, e o fechamento do caixa nao bateria com o
    dinheiro na gaveta — que e exatamente o que a tela existe para conferir.
    """

    hoje = datetime.now(timezone(timedelta(hours=-3))).date()
    tarde = datetime.combine(hoje, datetime.min.time(), tzinfo=timezone(timedelta(hours=-3))).replace(hour=21)
    if tarde > datetime.now(timezone.utc):
        pytest.skip("ainda nao passou das 21h no fuso da loja")
    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-300", quantity=1)], occurred_at=tarde))

    fechamento = service.sales_day(hoje.isoformat())

    assert any(row["id"] == order["id"] for row in fechamento["orders"])
