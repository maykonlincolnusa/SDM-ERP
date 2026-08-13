from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RecipeLine(BaseModel):
    inventory_id: str
    quantity: float = Field(gt=0)


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    category: str = Field(min_length=2, max_length=40)
    price: float = Field(gt=0)
    recipe: list[RecipeLine] = Field(default_factory=list)
    active: bool = True


class Product(ProductCreate):
    id: str
    image: str | None = None


class InventoryItemCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    unit: str = Field(min_length=1, max_length=12)
    quantity: float = Field(ge=0)
    minimum_quantity: float = Field(ge=0)
    unit_cost: float = Field(ge=0)
    category: str = Field(default="Insumos", max_length=40)


class InventoryItem(InventoryItemCreate):
    id: str
    updated_at: datetime | None = None


class InventoryAdjustment(BaseModel):
    quantity_delta: float
    reason: str = Field(min_length=2, max_length=120)


class OrderLine(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, le=50)


class OrderCreate(BaseModel):
    items: list[OrderLine] = Field(min_length=1)
    channel: Literal["Balcao", "Delivery", "iFood", "WhatsApp"] = "Balcao"
    payment_method: Literal["Pix", "Credito", "Debito", "Dinheiro"] = "Pix"
    customer_name: str | None = Field(default=None, max_length=80)
    discount: float = Field(default=0, ge=0)


class ExpenseCreate(BaseModel):
    description: str = Field(min_length=2, max_length=100)
    category: str = Field(min_length=2, max_length=40)
    amount: float = Field(gt=0)
    due_date: str
    status: Literal["Paga", "Pendente"] = "Pendente"


class Expense(ExpenseCreate):
    id: str
    created_at: datetime | None = None


class MediaUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    category: str | None = Field(default=None, min_length=2, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=12)
    entity_type: str | None = Field(default=None, max_length=40)
    entity_id: str | None = Field(default=None, max_length=120)
