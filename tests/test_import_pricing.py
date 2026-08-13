from copy import deepcopy

from openpyxl import Workbook

from scripts.import_pricing import import_workbook


class MemoryStore:
    mode = "test"

    def __init__(self):
        self.values = {
            "inventory": [
                {
                    "id": "stock-base",
                    "name": "Base antiga",
                    "unit": "kg",
                    "quantity": 12,
                    "minimum_quantity": 3,
                    "unit_cost": 9,
                    "category": "Insumos",
                }
            ],
            "products": [],
        }

    def get(self, collection, document_id):
        return next((deepcopy(item) for item in self.values.get(collection, []) if item["id"] == document_id), None)

    def put(self, collection, document_id, value):
        items = self.values.setdefault(collection, [])
        for index, item in enumerate(items):
            if item["id"] == document_id:
                items[index] = deepcopy(value)
                return
        items.append(deepcopy(value))


def add_pricing_sheet(workbook, title, price_label, price):
    sheet = workbook.create_sheet(title)
    sheet.append([title])
    sheet.append(["Base", "kg", 10, 100, 0.4, 4])
    sheet.append([None, None, None, None, None, None, None, price_label])
    sheet.append([None, None, None, None, None, None, None, price])


def test_import_uses_etl_pricing_contract_and_preserves_stock(tmp_path):
    workbook = Workbook()
    workbook.remove(workbook.active)
    add_pricing_sheet(workbook, "Produto com taxas", "VALOR SUGERIDO C/ TAXAS", 18)
    add_pricing_sheet(workbook, "Produto simples", "VALOR SUGERIDO SIMPLES", 12)
    path = tmp_path / "pricing.xlsx"
    workbook.save(path)
    store = MemoryStore()

    inventory_count, product_count = import_workbook(path, storage=store)

    assert inventory_count == 1
    assert product_count == 1
    inventory = store.get("inventory", "stock-base")
    assert inventory["quantity"] == 12
    assert inventory["minimum_quantity"] == 3
    assert inventory["unit_cost"] == 10.0
    product = store.get("products", "product-produto-com-taxas")
    assert product["price"] == 18.0
    assert store.get("products", "product-produto-simples") is None
