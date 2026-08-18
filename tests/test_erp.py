from concurrent.futures import ThreadPoolExecutor

from app.repositories.store import DemoStore
from app.schemas import InventoryAdjustment, OrderCreate, OrderLine
from app.services import ERPService
from openpyxl import Workbook
from scripts.import_pricing import import_workbook


def test_sale_decreases_recipe_stock_and_creates_order(tmp_path):
    service = ERPService(DemoStore(tmp_path / "store.json"))
    acai_before = next(item for item in service.inventory() if item["id"] == "acai")["quantity"]

    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=2)]))

    acai_after = next(item for item in service.inventory() if item["id"] == "acai")["quantity"]
    assert order["total"] == 44.0
    assert order["number"] == 1015
    assert acai_after == acai_before - 0.92
    assert service.orders()[0]["id"] == order["id"]


def test_stock_adjustment_cannot_go_negative(tmp_path):
    service = ERPService(DemoStore(tmp_path / "store.json"))

    try:
        service.adjust_inventory("banana", InventoryAdjustment(quantity_delta=-100, reason="perda"))
    except ValueError as error:
        assert "negativo" in str(error)
    else:
        raise AssertionError("Expected a negative-stock adjustment to be rejected")


def test_concurrent_sales_keep_stock_and_order_numbers_consistent(tmp_path):
    service = ERPService(DemoStore(tmp_path / "store.json"))

    def create_sale(_):
        return service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        orders = list(executor.map(create_sale, range(2)))

    assert sorted(order["number"] for order in orders) == [1015, 1016]
    acai = next(item for item in service.inventory() if item["id"] == "acai")
    assert acai["quantity"] == 31.5 - 0.92


def test_dashboard_exposes_business_metrics(tmp_path):
    service = ERPService(DemoStore(tmp_path / "store.json"))

    dashboard = service.dashboard(7)

    assert dashboard["metrics"]["revenue"] > 0
    assert dashboard["metrics"]["gross_margin"] > 0
    assert len(dashboard["sales_series"]) == 7
    assert dashboard["top_products"]


def test_sale_is_recorded_even_without_stock_balance(tmp_path):
    """Venda nunca e recusada por saldo: o pedido ja aconteceu no balcao.

    A loja lanca a contagem depois, e bloquear o registro so faria o
    faturamento sumir do sistema. O saldo negativo fica como sinal visivel
    de que o cadastro esta atrasado.
    """

    service = ERPService(DemoStore(tmp_path / "store.json"))
    for item in service.inventory():
        item["quantity"] = 0
        service.store.put("inventory", item["id"], item)

    order = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=2)]))

    assert order["total"] == 44.0
    acai = next(item for item in service.inventory() if item["id"] == "acai")
    assert acai["quantity"] < 0, "o consumo precisa ser descontado mesmo sem saldo"
    titles = {insight["title"] for insight in service.dashboard(7)["insights"]}
    assert "Saldo negativo no estoque" in titles, "o gestor precisa ser avisado do saldo negativo"


def test_inventory_without_minimum_is_not_reported_as_low_stock(tmp_path):
    """Insumos importados da precificação chegam com mínimo zero.

    Sem essa distinção, o painel acusa reposição urgente em cada ingrediente da
    ficha técnica e o alerta real fica invisível no meio do ruído.
    """

    service = ERPService(DemoStore(tmp_path / "store.json"))
    service.store.put(
        "inventory",
        "stock-importado",
        {"id": "stock-importado", "name": "Insumo importado", "unit": "kg", "quantity": 0, "minimum_quantity": 0, "unit_cost": 12.0, "category": "Importado da precificação"},
    )

    dashboard = service.dashboard(7)

    low_stock_ids = {item["id"] for item in dashboard["low_stock"]}
    assert "stock-importado" not in low_stock_ids
    titles = {insight["title"] for insight in dashboard["insights"]}
    assert "Faça a contagem inicial" not in titles, "a loja de demonstração já tem saldo lançado"


def test_pricing_importer_creates_recipe_and_suggested_price(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Precificação Açaí"
    sheet.append(["Açaí teste 500 ml"])
    sheet.append(["Base de açaí", "kg", 10, 100, 0.4, 4])
    sheet.append([None, None, None, None, None, None, None, "VALOR SUGERIDO c/ TAXAS"])
    sheet.append([None, None, None, None, None, None, None, 18])
    workbook_path = tmp_path / "pricing.xlsx"
    workbook.save(workbook_path)
    store = DemoStore(tmp_path / "store.json")

    ingredients, products = import_workbook(workbook_path, store)

    assert (ingredients, products) == (1, 1)
    product = store.get("products", "product-acai-teste-500-ml")
    assert product["price"] == 18
    assert product["recipe"] == [{"inventory_id": "stock-base-de-acai", "quantity": 0.4}]
