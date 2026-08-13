import json
import logging
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from app.config import Settings
from app.firebase import get_firebase_app

logger = logging.getLogger(__name__)
T = TypeVar("T")


class StoreTransaction(Protocol):
    def list(self, collection: str) -> list[dict[str, Any]]: ...

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None: ...

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None: ...


class Store(Protocol):
    mode: str

    def list(self, collection: str) -> list[dict[str, Any]]: ...

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None: ...

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None: ...

    def run_transaction(self, operation: Callable[[StoreTransaction], T]) -> T: ...


class DemoStore:
    """Small JSON-backed store for local use and a ready-to-explore first run."""

    mode = "demo"

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(_seed_data())

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        with self.path.open("r", encoding="utf-8") as source:
            return json.load(source)

    def _write(self, data: dict[str, list[dict[str, Any]]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
        temporary.replace(self.path)

    def list(self, collection: str) -> list[dict[str, Any]]:
        with self.lock:
            return deepcopy(self._read().get(collection, []))

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = next(
                (row for row in self._read().get(collection, []) if row["id"] == document_id),
                None,
            )
            return deepcopy(item)

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        with self.lock:
            data = self._read()
            records = data.setdefault(collection, [])
            for index, record in enumerate(records):
                if record["id"] == document_id:
                    records[index] = deepcopy(value)
                    break
            else:
                records.append(deepcopy(value))
            self._write(data)

    def run_transaction(self, operation: Callable[[StoreTransaction], T]) -> T:
        with self.lock:
            data = self._read()
            result = operation(_DemoTransaction(data))
            self._write(data)
            return deepcopy(result)


class _DemoTransaction:
    def __init__(self, data: dict[str, list[dict[str, Any]]]):
        self.data = data

    def list(self, collection: str) -> list[dict[str, Any]]:
        return deepcopy(self.data.get(collection, []))

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        item = next((row for row in self.data.get(collection, []) if row["id"] == document_id), None)
        return deepcopy(item)

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        records = self.data.setdefault(collection, [])
        for index, record in enumerate(records):
            if record["id"] == document_id:
                records[index] = deepcopy(value)
                return
        records.append(deepcopy(value))


class FirestoreStore:
    """Firestore document adapter. Collections match the local demo collections."""

    mode = "firebase"

    def __init__(self, settings: Settings):
        try:
            from firebase_admin import firestore
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise RuntimeError("firebase-admin is required for Firebase mode") from error

        self.firestore = firestore
        self.app = get_firebase_app(settings)
        self.db = firestore.client(app=self.app)

    def list(self, collection: str) -> list[dict[str, Any]]:
        return [{"id": snap.id, **snap.to_dict()} for snap in self.db.collection(collection).stream()]

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        snap = self.db.collection(collection).document(document_id).get()
        return {"id": snap.id, **snap.to_dict()} if snap.exists else None

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        payload = {key: value for key, value in value.items() if key != "id"}
        self.db.collection(collection).document(document_id).set(payload)

    def run_transaction(self, operation: Callable[[StoreTransaction], T]) -> T:
        @self.firestore.transactional
        def execute(transaction):
            return operation(_FirestoreTransaction(self.db, transaction))

        return execute(self.db.transaction())


class _FirestoreTransaction:
    def __init__(self, database, transaction):
        self.database = database
        self.transaction = transaction

    def list(self, collection: str) -> list[dict[str, Any]]:
        snapshots = self.database.collection(collection).stream(transaction=self.transaction)
        return [{"id": snapshot.id, **snapshot.to_dict()} for snapshot in snapshots]

    def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        reference = self.database.collection(collection).document(document_id)
        snapshot = next(self.transaction.get(reference), None)
        return {"id": snapshot.id, **snapshot.to_dict()} if snapshot and snapshot.exists else None

    def put(self, collection: str, document_id: str, value: dict[str, Any]) -> None:
        payload = {key: value for key, value in value.items() if key != "id"}
        self.transaction.set(self.database.collection(collection).document(document_id), payload)


def build_store(settings: Settings) -> Store:
    if settings.data_mode == "demo":
        return DemoStore(settings.demo_data_path)
    if settings.data_mode == "firebase":
        return FirestoreStore(settings)
    if settings.firebase_configured:
        try:
            return FirestoreStore(settings)
        except Exception as error:
            logger.warning("Firebase indisponivel; usando dados demonstrativos: %s", error)
    elif settings.firebase_requested:
        logger.warning("Configuracao Firebase incompleta; usando dados demonstrativos.")
    return DemoStore(settings.demo_data_path)


def _seed_data() -> dict[str, list[dict[str, Any]]]:
    now = datetime.now(timezone.utc)
    today = date.today()
    inventory = [
        _inventory("acai", "Base de açaí", "kg", 31.5, 8, 22.5, "Polpas"),
        _inventory("guarana", "Xarope de guaraná", "L", 7.2, 2, 13.8, "Complementos"),
        _inventory("banana", "Banana", "kg", 8.6, 4, 6.9, "Frutas"),
        _inventory("morango", "Morango", "kg", 1.4, 2, 22.0, "Frutas"),
        _inventory("granola", "Granola", "kg", 3.8, 1.5, 27.0, "Complementos"),
        _inventory("leite-po", "Leite em pó", "kg", 1.8, 0.8, 30.0, "Complementos"),
        _inventory("copo-300", "Copo 300 ml", "un", 94, 80, 0.72, "Embalagens"),
        _inventory("copo-500", "Copo 500 ml", "un", 126, 80, 0.88, "Embalagens"),
        _inventory("copo-700", "Copo 700 ml", "un", 61, 60, 1.05, "Embalagens"),
    ]
    products = [
        _product("acai-300", "Açaí 300 ml", "Açaí", 16.0, [("acai", 0.28), ("guarana", 0.02), ("banana", 0.06), ("granola", 0.035), ("copo-300", 1)]),
        _product("acai-500", "Açaí 500 ml", "Açaí", 22.0, [("acai", 0.46), ("guarana", 0.03), ("banana", 0.1), ("granola", 0.055), ("copo-500", 1)]),
        _product("acai-700", "Açaí 700 ml", "Açaí", 28.0, [("acai", 0.63), ("guarana", 0.04), ("banana", 0.13), ("granola", 0.075), ("copo-700", 1)]),
        _product("super-bowl", "Super Bowl", "Bowl", 31.0, [("acai", 0.58), ("guarana", 0.04), ("banana", 0.14), ("morango", 0.08), ("granola", 0.08), ("leite-po", 0.025), ("copo-700", 1)]),
    ]
    products[1]["image"] = "/static/assets/acai-copo.png"
    orders: list[dict[str, Any]] = []
    order_specs = [
        (0, "acai-500", 3, "Balcao", "Pix"), (0, "acai-300", 2, "Balcao", "Debito"), (1, "super-bowl", 2, "iFood", "Credito"), (1, "acai-700", 1, "WhatsApp", "Pix"), (2, "acai-500", 4, "Balcao", "Pix"), (2, "acai-300", 3, "Delivery", "Pix"), (3, "acai-700", 2, "iFood", "Credito"), (3, "acai-500", 2, "Balcao", "Dinheiro"), (4, "super-bowl", 1, "Balcao", "Pix"), (4, "acai-300", 5, "Balcao", "Debito"), (5, "acai-500", 5, "iFood", "Credito"), (5, "acai-700", 2, "Balcao", "Pix"), (6, "acai-500", 4, "Balcao", "Pix"), (6, "super-bowl", 2, "Delivery", "Pix"),
    ]
    product_map = {product["id"]: product for product in products}
    inventory_map = {item["id"]: item for item in inventory}
    for index, (days_ago, product_id, quantity, channel, payment) in enumerate(order_specs, start=1):
        product = product_map[product_id]
        unit_cost = round(sum(inventory_map[line["inventory_id"]]["unit_cost"] * line["quantity"] for line in product["recipe"]), 2)
        created = datetime.combine(today - timedelta(days=days_ago), datetime.min.time(), tzinfo=timezone.utc).replace(hour=11 + index % 8)
        orders.append({"id": f"seed-{index}", "number": 1000 + index, "created_at": created.isoformat(), "channel": channel, "payment_method": payment, "customer_name": None, "discount": 0, "total": round(product["price"] * quantity, 2), "cost": round(unit_cost * quantity, 2), "status": "Concluido", "items": [{"product_id": product_id, "name": product["name"], "quantity": quantity, "unit_price": product["price"], "unit_cost": unit_cost, "line_total": round(product["price"] * quantity, 2)}]})
    return {"products": products, "inventory": inventory, "orders": orders, "expenses": [{"id": "expense-1", "description": "Aluguel", "category": "Fixo", "amount": 1800.0, "due_date": today.replace(day=min(5, today.day)).isoformat(), "status": "Paga", "created_at": now.isoformat()}, {"id": "expense-2", "description": "Compra de frutas", "category": "Insumos", "amount": 486.5, "due_date": (today + timedelta(days=2)).isoformat(), "status": "Pendente", "created_at": now.isoformat()}, {"id": "expense-3", "description": "Energia elétrica", "category": "Fixo", "amount": 278.4, "due_date": (today + timedelta(days=5)).isoformat(), "status": "Pendente", "created_at": now.isoformat()}], "inventory_movements": []}


def _inventory(item_id: str, name: str, unit: str, quantity: float, minimum: float, unit_cost: float, category: str) -> dict[str, Any]:
    return {"id": item_id, "name": name, "unit": unit, "quantity": quantity, "minimum_quantity": minimum, "unit_cost": unit_cost, "category": category, "updated_at": datetime.now(timezone.utc).isoformat()}


def _product(item_id: str, name: str, category: str, price: float, recipe: list[tuple[str, float]]) -> dict[str, Any]:
    return {"id": item_id, "name": name, "category": category, "price": price, "recipe": [{"inventory_id": stock_id, "quantity": quantity} for stock_id, quantity in recipe], "active": True}
