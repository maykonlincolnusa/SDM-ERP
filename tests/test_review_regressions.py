from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.repositories.store import DemoStore, FirestoreStore, _seed_data
from app.schemas import (
    CourierCreate, DeliveryEntryCreate, DeliveryEntryUpdate, ExpenseCreate,
    ExpenseSeriesUpdate, InventoryAdjustment, InventoryItemUpdate,
    OrderCreate, OrderLine, OrderUpdate, ProductCreate, ProductionRecipeCreate,
)
from app.services import ERPService, DeliveryService
from app.services.erp import FUSO_LOJA


class Snapshot:
    def __init__(self, key, value):
        self.id = key
        self.exists = value is not None
        self.value = value

    def to_dict(self):
        return deepcopy(self.value)


class StrictTransaction:
    def __init__(self, database):
        self.database = database
        self.writes = []

    def get(self, reference):
        assert not self.writes, "Firestore forbids reads after writes"
        name, key = reference
        yield Snapshot(key, self.database.data.get(name, {}).get(key))

    def set(self, reference, value):
        self.writes.append((reference, deepcopy(value)))

    def delete(self, reference):
        self.writes.append((reference, None))


class StrictDatabase:
    def __init__(self):
        self.data = {name: {row['id']: row for row in rows} for name, rows in _seed_data().items()}

    def collection(self, name):
        def stream(transaction):
            assert not transaction.writes, "Firestore forbids reads after writes"
            return iter(Snapshot(key, value) for key, value in self.data.get(name, {}).items())
        return SimpleNamespace(document=lambda key: (name, key), stream=stream)

    def transaction(self):
        return StrictTransaction(self)


def strict_store():
    database = StrictDatabase()
    def transactional(operation):
        def execute(transaction):
            result = operation(transaction)
            for (name, key), value in transaction.writes:
                if value is None:
                    database.data.setdefault(name, {}).pop(key, None)
                else:
                    database.data.setdefault(name, {})[key] = value
            return result
        return execute
    store = FirestoreStore.__new__(FirestoreStore)
    store.db = database
    store.firestore = SimpleNamespace(transactional=transactional)
    # Ordinary reads are independent of the transaction under test.
    store.put = lambda name, key, value: database.data.setdefault(name, {}).__setitem__(key, deepcopy(value))
    store.list = lambda name: [{**deepcopy(v), 'id': k} for k, v in database.data.get(name, {}).items()]
    return store


def test_firestore_sale_edit_cancel_preserves_stock():
    store = strict_store()
    erp = ERPService(store)
    initial = {row['id']: row['quantity'] for row in store.list('inventory')}
    order = erp.create_order(OrderCreate(items=[OrderLine(product_id='acai-500', quantity=2)]))
    erp.update_order(order['id'], OrderUpdate(items=[OrderLine(product_id='acai-500', quantity=1)]))
    erp.cancel_order(order['id'])
    assert {row['id']: row['quantity'] for row in store.list('inventory')} == initial


def test_firestore_overlay_and_rollback():
    store = strict_store()
    def change(tx):
        tx.put('test', 'one', {'id': 'one', 'value': 1})
        tx.put('test', 'two', {'id': 'two', 'value': 2})
        copy = tx.get('test', 'one')
        copy['value'] = 99
        assert tx.get('test', 'one')['value'] == 1
        tx.delete('test', 'two')
        assert tx.get('test', 'two') is None
        assert tx.list('test') == [{'id': 'one', 'value': 1}]
    store.run_transaction(change)
    assert store.list('test') == [{'id': 'one', 'value': 1}]
    def fail(tx):
        tx.delete('test', 'one')
        tx.put('test', 'three', {'value': 3})
        raise ValueError('abort')
    with pytest.raises(ValueError, match='abort'):
        store.run_transaction(fail)
    assert store.list('test') == [{'id': 'one', 'value': 1}]


@pytest.fixture
def erp(tmp_path):
    return ERPService(DemoStore(tmp_path / 'store.json'))


def test_inventory_optional_fields_can_be_cleared(erp):
    erp.update_inventory_item('acai', InventoryItemUpdate(pack_unit='barra', pack_size=2.8))
    item = erp.update_inventory_item('acai', InventoryItemUpdate(pack_unit=None, pack_size=None, shelf_life_days=None))
    assert item['pack_unit'] is item['pack_size'] is item['shelf_life_days'] is None


def test_inventory_rejects_incomplete_pack_without_writing(erp):
    before = erp.store.get('inventory', 'acai')
    with pytest.raises(ValueError, match='unidade de manuseio'):
        erp.update_inventory_item('acai', InventoryItemUpdate(pack_unit='barra'))
    assert erp.store.get('inventory', 'acai') == before


def test_order_rejects_missing_courier_without_stock_change(erp):
    before = erp.inventory()
    with pytest.raises(ValueError, match='Entregador'):
        erp.create_order(OrderCreate(items=[OrderLine(product_id='acai-500', quantity=1)], courier_id='missing'))
    assert erp.inventory() == before


def test_delivery_edit_cannot_move_shift_into_future(erp):
    delivery = DeliveryService(erp.store)
    courier = delivery.create_courier(CourierCreate(name='Maria'))
    today = datetime.now(FUSO_LOJA).date()
    entry = delivery.create_entry(DeliveryEntryCreate(courier_id=courier['id'], date=today.isoformat()))
    with pytest.raises(ValueError, match='futuro'):
        delivery.update_entry(entry['id'], DeliveryEntryUpdate(date=(today + timedelta(days=1)).isoformat()))
    assert erp.store.get('delivery_entries', entry['id'])['date'] == today.isoformat()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_non_finite_stock_values_are_rejected(value):
    with pytest.raises(ValidationError):
        InventoryAdjustment(quantity_delta=value, reason='Ajuste')


def test_whitespace_name_is_rejected():
    with pytest.raises(ValidationError):
        ProductCreate(name='   ', category='Acai', price=10)


def test_recurring_expense_end_can_be_cleared(erp):
    today = datetime.now(FUSO_LOJA).date()
    expense = erp.create_expense(ExpenseCreate(description='Aluguel', category='Fixo', amount=100,
        due_date=today.isoformat(), recurring=True, repeat_until=today.strftime('%Y-%m')))
    rule = erp.update_expense_series(expense['series_id'], ExpenseSeriesUpdate(repeat_until=None))
    assert rule['ends_on'] is None


def test_manufacturing_ingredient_cannot_be_deleted(erp):
    from app.schemas import InventoryItemCreate, InventoryBulkCount, InventoryCountLine
    stock = erp.create_inventory_item(InventoryItemCreate(name='Barra', unit='kg', quantity=2,
        minimum_quantity=0, unit_cost=10))
    erp.create_production_recipe(ProductionRecipeCreate(name='Base pronta',
        inputs=[{'inventory_id': stock['id'], 'quantity': 1}], output_inventory_id='acai', output_quantity=2))
    with pytest.raises(ValueError, match='fabricação'):
        erp.delete_inventory_item(stock['id'])
    with pytest.raises(ValueError, match='fabricação'):
        erp.bulk_count(InventoryBulkCount(items=[InventoryCountLine(inventory_id=stock['id'], remove=True)]))
    assert erp.store.get('inventory', stock['id']) is not None


def test_imported_order_discount_and_freight_recompute_total(erp):
    erp.store.put('orders', 'imported', {'id': 'imported', 'number': 42, 'items': [],
        'total': 25, 'discount': 2, 'delivery_fee': 5, 'channel': 'Balcao',
        'status': 'Concluido', 'created_at': datetime.now(FUSO_LOJA).isoformat(), 'cost': None})
    updated = erp.update_order('imported', OrderUpdate(discount=3, delivery_fee=7))
    assert updated['total'] == 26


@pytest.mark.parametrize('model,fields', [
    (ExpenseCreate, {'description': 'Luz', 'category': 'Fixo', 'amount': 10, 'due_date': '2026-02-30'}),
    (ExpenseSeriesUpdate, {'repeat_until': '2026-99'}),
    (DeliveryEntryUpdate, {'date': 'not-a-date'}),
])
def test_invalid_calendar_values_are_rejected(model, fields):
    with pytest.raises(ValidationError):
        model(**fields)


@pytest.mark.parametrize('changes', [
    {'output_inventory_id': 'banana'},
    {'inputs': [{'inventory_id': 'banana', 'quantity': 1}, {'inventory_id': 'banana', 'quantity': 2}]},
])
def test_manufacturing_recipe_edit_validates_merged_recipe(erp, changes):
    from app.schemas import ProductionRecipeUpdate
    recipe = erp.create_production_recipe(ProductionRecipeCreate(name='Base pronta',
        inputs=[{'inventory_id': 'banana', 'quantity': 1}], output_inventory_id='acai', output_quantity=2))
    with pytest.raises(ValueError):
        erp.update_production_recipe(recipe['id'], ProductionRecipeUpdate(**changes))
    assert erp.store.get('production_recipes', recipe['id']) == recipe
