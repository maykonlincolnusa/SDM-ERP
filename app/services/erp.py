import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.repositories.store import Store, StoreTransaction
from app.schemas import ExpenseCreate, InventoryAdjustment, InventoryItemCreate, OrderCreate, ProductCreate


# Marca os movimentos gerados pela venda. Todo o resto e lancamento manual:
# perda, quebra, degustacao ou recebimento.
SALE_REASON = "Venda registrada"


def needs_restock(item: dict[str, Any]) -> bool:
    """Only alert when a minimum was actually configured.

    Items imported from the pricing workbook arrive with minimum zero, which
    means "not defined yet" and not "out of stock". Treating those as critical
    would bury the real alerts under one line per ingredient.
    """

    return item["minimum_quantity"] > 0 and item["quantity"] <= item["minimum_quantity"]


class ERPService:
    def __init__(self, store: Store):
        self.store = store

    def products(self) -> list[dict[str, Any]]:
        inventory = {item["id"]: item for item in self.store.list("inventory")}
        products = []
        for product in self.store.list("products"):
            cost = self._recipe_cost(product.get("recipe", []), inventory)
            products.append({**product, "cost": round(cost, 2), "margin": self._margin(product["price"], cost)})
        return sorted(products, key=lambda item: item["name"])

    def inventory(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("inventory"), key=lambda item: (not needs_restock(item), item["name"]))

    def orders(self, days: int | None = None) -> list[dict[str, Any]]:
        values = self.store.list("orders")
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            values = [order for order in values if self._timestamp(order["created_at"]) >= cutoff]
        return sorted(values, key=lambda item: self._timestamp(item["created_at"]), reverse=True)

    def expenses(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("expenses"), key=lambda item: (item["status"] == "Paga", item["due_date"]))

    def dashboard(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(days, 90))
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days - 1)
        previous_start = start - timedelta(days=days)
        all_orders = self.orders()
        current_orders = [order for order in all_orders if self._timestamp(order["created_at"]) >= start]
        previous_orders = [order for order in all_orders if previous_start <= self._timestamp(order["created_at"]) < start]
        revenue = sum(order["total"] for order in current_orders)
        # Pedido importado de fora traz o faturamento, mas nao os itens, entao o
        # custo e desconhecido — diferente de zero, que significaria "de graca" e
        # inflaria a margem. Margem e lucro contam so o que tem custo medido.
        com_custo = [order for order in current_orders if order.get("cost") is not None]
        revenue_com_custo = sum(order["total"] for order in com_custo)
        cost = sum(order["cost"] for order in com_custo)
        previous_revenue = sum(order["total"] for order in previous_orders)
        average_ticket = revenue / len(current_orders) if current_orders else 0
        inventory = self.inventory()
        low_stock = [item for item in inventory if needs_restock(item)]
        by_day: dict[str, float] = defaultdict(float)
        labels: list[str] = []
        for offset in range(days - 1, -1, -1):
            label = (now.date() - timedelta(days=offset)).isoformat()
            labels.append(label)
            by_day[label] = 0
        for order in current_orders:
            by_day[self._timestamp(order["created_at"]).date().isoformat()] += order["total"]
        product_sales: Counter[str] = Counter()
        channels: Counter[str] = Counter()
        for order in current_orders:
            channels[order["channel"]] += order["total"]
            for item in order["items"]:
                product_sales[item["name"]] += item["quantity"]
        expense_total = sum(expense["amount"] for expense in self.expenses() if expense["status"] == "Paga")
        return {
            "period_days": days,
            "mode": self.store.mode,
            "metrics": {
                "revenue": round(revenue, 2),
                "orders": len(current_orders),
                "average_ticket": round(average_ticket, 2),
                "gross_profit": round(revenue_com_custo - cost, 2),
                "gross_margin": self._margin(revenue_com_custo, cost),
                # Sobre quanto do faturamento a margem foi calculada. Abaixo de
                # 100% a tela precisa dizer isso, senao o numero parece valer
                # para o periodo inteiro.
                "margin_coverage": round(revenue_com_custo / revenue * 100, 1) if revenue else 100.0,
                "revenue_without_cost": round(revenue - revenue_com_custo, 2),
                "expense_total": round(expense_total, 2),
                "revenue_change": self._change(revenue, previous_revenue),
            },
            "sales_series": [{"date": label, "value": round(by_day[label], 2)} for label in labels],
            "top_products": [{"name": name, "quantity": quantity} for name, quantity in product_sales.most_common(4)],
            "channels": [{"name": name, "value": round(value, 2)} for name, value in channels.most_common()],
            "low_stock": low_stock,
            "recent_orders": current_orders[:6],
            "insights": self._insights(current_orders, inventory, product_sales, channels),
        }

    def create_order(self, payload: OrderCreate) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        order_id = str(uuid4())

        def create(transaction: StoreTransaction) -> dict[str, Any]:
            products: dict[str, dict[str, Any]] = {}
            needed: Counter[str] = Counter()
            for line in payload.items:
                product = transaction.get("products", line.product_id)
                if not product or not product.get("active", True):
                    raise ValueError("Produto indisponível para venda.")
                products[line.product_id] = product
                for recipe_line in product.get("recipe", []):
                    needed[recipe_line["inventory_id"]] += recipe_line["quantity"] * line.quantity

            # A venda nunca e recusada por falta de saldo. O pedido ja aconteceu no
            # balcao: bloquear o registro nao devolve o insumo, so faz a loja perder
            # o faturamento no sistema quando alguem esquece de lancar a contagem.
            # O saldo pode ficar negativo de proposito, como sinal de que o cadastro
            # esta atrasado, e a tela de Estoque destaca esses itens para conferencia.
            stock: dict[str, dict[str, Any]] = {}
            for inventory_id in needed:
                item = transaction.get("inventory", inventory_id)
                if item:
                    stock[inventory_id] = item

            line_items: list[dict[str, Any]] = []
            total = total_cost = 0.0
            for line in payload.items:
                product = products[line.product_id]
                unit_cost = self._recipe_cost(product.get("recipe", []), stock)
                line_total = product["price"] * line.quantity
                line_cost = unit_cost * line.quantity
                total += line_total
                total_cost += line_cost
                line_items.append(
                    {
                        "product_id": product["id"],
                        "name": product["name"],
                        "quantity": line.quantity,
                        "unit_price": product["price"],
                        "unit_cost": round(unit_cost, 2),
                        "line_total": round(line_total, 2),
                    }
                )
            if payload.discount > total:
                raise ValueError("O desconto não pode ser maior que o valor da venda.")

            sequence = transaction.get("system", "order_sequence")
            if sequence:
                sequence_value = int(sequence.get("value", 1000))
            else:
                sequence_value = max(
                    (int(order.get("number", 1000)) for order in transaction.list("orders")),
                    default=1000,
                )
            number = sequence_value + 1
            transaction.put("system", "order_sequence", {"id": "order_sequence", "value": number, "updated_at": now.isoformat()})
            for inventory_id, quantity in needed.items():
                item = stock.get(inventory_id)
                # Receita que aponta para insumo removido do cadastro nao tem o que
                # baixar, mas tambem nao pode impedir a venda de ser registrada.
                if not item:
                    continue
                item["quantity"] = round(item["quantity"] - quantity, 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", inventory_id, item)
                movement = {
                    "id": str(uuid4()),
                    "inventory_id": inventory_id,
                    "quantity_delta": -round(quantity, 3),
                    "reason": SALE_REASON,
                    "created_at": now.isoformat(),
                }
                transaction.put("inventory_movements", movement["id"], movement)
            order = {
                "id": order_id,
                "number": number,
                "created_at": now.isoformat(),
                "channel": payload.channel,
                "payment_method": payload.payment_method,
                "customer_name": payload.customer_name,
                "discount": payload.discount,
                "total": round(total - payload.discount, 2),
                "cost": round(total_cost, 2),
                "status": "Concluido",
                "items": line_items,
            }
            transaction.put("orders", order["id"], order)
            return order

        return self.store.run_transaction(create)

    def create_product(self, payload: ProductCreate) -> dict[str, Any]:
        inventory_ids = {item["id"] for item in self.inventory()}
        invalid_items = [line.inventory_id for line in payload.recipe if line.inventory_id not in inventory_ids]
        if invalid_items:
            raise ValueError("A receita inclui um insumo que não existe no estoque.")
        item = payload.model_dump()
        item["id"] = self._identifier(payload.name, "product")
        if self.store.get("products", item["id"]):
            item["id"] = f"{item['id']}-{str(uuid4())[:5]}"
        self.store.put("products", item["id"], item)
        return item

    def create_inventory_item(self, payload: InventoryItemCreate) -> dict[str, Any]:
        item = payload.model_dump()
        item["id"] = self._identifier(payload.name, "stock")
        if self.store.get("inventory", item["id"]):
            item["id"] = f"{item['id']}-{str(uuid4())[:5]}"
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("inventory", item["id"], item)
        return item

    def adjust_inventory(self, inventory_id: str, payload: InventoryAdjustment) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        movement_id = str(uuid4())

        def adjust(transaction: StoreTransaction) -> dict[str, Any]:
            item = transaction.get("inventory", inventory_id)
            if not item:
                raise ValueError("Item de estoque não encontrado.")
            if item["quantity"] + payload.quantity_delta < 0:
                raise ValueError("O ajuste deixaria o estoque negativo.")
            item["quantity"] = round(item["quantity"] + payload.quantity_delta, 3)
            item["updated_at"] = now.isoformat()
            transaction.put("inventory", inventory_id, item)
            movement = {
                "id": movement_id,
                "inventory_id": inventory_id,
                "quantity_delta": payload.quantity_delta,
                "reason": payload.reason,
                "created_at": now.isoformat(),
            }
            transaction.put("inventory_movements", movement["id"], movement)
            return item

        return self.store.run_transaction(adjust)

    def consumption(self, days: int = 7) -> dict[str, Any]:
        """Turn the movement log into what the owner actually needs to decide.

        Movements are already written by every sale and every manual
        adjustment, but nothing reads them. Sales and losses are separated
        because they mean opposite things: one produced revenue, the other
        just evaporated.
        """

        days = max(1, min(days, 90))
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days - 1)
        stock = {item["id"]: item for item in self.store.list("inventory")}

        by_item: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, float]] = {}
        for offset in range(days - 1, -1, -1):
            by_day[(now.date() - timedelta(days=offset)).isoformat()] = {"sale_cost": 0.0, "loss_cost": 0.0}

        history: list[dict[str, Any]] = []
        for movement in self.store.list("inventory_movements"):
            moment = self._timestamp(movement["created_at"])
            if moment < start:
                continue
            item = stock.get(movement["inventory_id"])
            if not item:
                continue
            delta = movement["quantity_delta"]
            kind = "entrada" if delta > 0 else "venda" if movement["reason"] == SALE_REASON else "perda"
            quantity = abs(delta)
            cost = round(quantity * item["unit_cost"], 2)
            day = moment.date().isoformat()
            if kind != "entrada" and day in by_day:
                by_day[day]["sale_cost" if kind == "venda" else "loss_cost"] += cost

            row = by_item.setdefault(
                movement["inventory_id"],
                {"inventory_id": item["id"], "name": item["name"], "unit": item["unit"], "sold": 0.0, "lost": 0.0, "received": 0.0, "cost": 0.0},
            )
            row["sold" if kind == "venda" else "lost" if kind == "perda" else "received"] += quantity
            if kind != "entrada":
                row["cost"] += cost
            history.append({**movement, "name": item["name"], "unit": item["unit"], "kind": kind, "cost": cost})

        items = []
        for row in by_item.values():
            item = stock[row["inventory_id"]]
            used = row["sold"] + row["lost"]
            daily_average = used / days
            items.append({
                **row,
                "sold": round(row["sold"], 3),
                "lost": round(row["lost"], 3),
                "received": round(row["received"], 3),
                "used": round(used, 3),
                "cost": round(row["cost"], 2),
                "quantity": item["quantity"],
                "daily_average": round(daily_average, 3),
                # Sem consumo no periodo nao da para projetar duracao sem inventar numero.
                "days_left": round(item["quantity"] / daily_average, 1) if daily_average > 0 else None,
            })
        items.sort(key=lambda row: row["cost"], reverse=True)

        sale_cost = round(sum(day["sale_cost"] for day in by_day.values()), 2)
        loss_cost = round(sum(day["loss_cost"] for day in by_day.values()), 2)
        total_cost = round(sale_cost + loss_cost, 2)
        history.sort(key=lambda row: row["created_at"], reverse=True)
        return {
            "period_days": days,
            "totals": {
                "sale_cost": sale_cost,
                "loss_cost": loss_cost,
                "total_cost": total_cost,
                "loss_share": round(loss_cost / total_cost * 100, 1) if total_cost else 0.0,
                "daily_cost": round(total_cost / days, 2),
            },
            "series": [{"date": day, **{key: round(value, 2) for key, value in values.items()}} for day, values in by_day.items()],
            "items": items,
            "running_out": [row for row in items if row["days_left"] is not None and row["days_left"] <= 7],
            "history": history[:40],
        }

    def create_expense(self, payload: ExpenseCreate) -> dict[str, Any]:
        expense = payload.model_dump()
        expense["id"] = str(uuid4())
        expense["created_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("expenses", expense["id"], expense)
        return expense

    @staticmethod
    def _recipe_cost(recipe: list[dict[str, Any]], inventory: dict[str, dict[str, Any]]) -> float:
        return sum(inventory.get(line["inventory_id"], {}).get("unit_cost", 0) * line["quantity"] for line in recipe)

    @staticmethod
    def _margin(revenue: float, cost: float) -> float:
        return round(((revenue - cost) / revenue) * 100, 1) if revenue else 0

    @staticmethod
    def _change(current: float, previous: float) -> float:
        if not previous:
            return 100.0 if current else 0.0
        return round(((current - previous) / previous) * 100, 1)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    @staticmethod
    def _identifier(value: str, prefix: str) -> str:
        normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
        return f"{prefix}-{slug or 'item'}"

    def _insights(self, orders: list[dict[str, Any]], inventory: list[dict[str, Any]], products: Counter[str], channels: Counter[str]) -> list[dict[str, str]]:
        insights: list[dict[str, str]] = []
        critical = [item for item in inventory if needs_restock(item)]
        if critical:
            names = ", ".join(item["name"] for item in critical[:2])
            insights.append({"type": "attention", "title": "Reposição prioritária", "description": f"{names} atingiu o estoque mínimo. Programe a compra antes do próximo pico."})
        negative = [item for item in inventory if item["quantity"] < 0]
        if negative:
            names = ", ".join(item["name"] for item in negative[:2])
            insights.append({
                "type": "attention",
                "title": "Saldo negativo no estoque",
                "description": f"{names} saiu mais do que o cadastro tinha. A venda foi registrada assim mesmo; refaça a contagem desses itens para o custo voltar a bater.",
            })
        insights.extend(self._setup_insights(orders, inventory))
        if products:
            product, volume = products.most_common(1)[0]
            insights.append({"type": "opportunity", "title": "Produto com maior giro", "description": f"{product} vendeu {volume} unidades no período. Mantenha esse item visível no balcão."})
        delivery_value = channels.get("Delivery", 0) + channels.get("iFood", 0) + channels.get("WhatsApp", 0)
        revenue = sum(order["total"] for order in orders)
        if revenue and delivery_value / revenue >= 0.3:
            insights.append({"type": "info", "title": "Canal digital relevante", "description": f"{round(delivery_value / revenue * 100)}% da receita veio de canais fora do balcão. Acompanhe taxas desses pedidos."})
        if not insights:
            insights.append({"type": "info", "title": "Dados em formação", "description": "Registre as vendas do dia para liberar recomendações mais precisas."})
        return insights[:3]

    @staticmethod
    def _setup_insights(orders: list[dict[str, Any]], inventory: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Guidance for a store that was just set up, in the order it should act.

        A catalog imported from the pricing workbook has cost and margin but no
        balances, so the generic "registre suas vendas" message leaves the owner
        without a next step.
        """

        pending: list[dict[str, str]] = []
        if inventory and not any(item["quantity"] for item in inventory):
            pending.append({
                "type": "attention",
                "title": "Faça a contagem inicial",
                "description": f"Os {len(inventory)} insumos vieram da precificação sem saldo. Use Ajustar saldo para lançar o que existe hoje e o estoque passa a baixar sozinho a cada venda.",
            })
        elif inventory and not any(item["minimum_quantity"] for item in inventory):
            pending.append({
                "type": "info",
                "title": "Defina o estoque mínimo",
                "description": "Nenhum insumo tem mínimo configurado, então o aviso de reposição fica desligado. Defina o mínimo dos itens que não podem faltar.",
            })
        if not orders:
            pending.append({
                "type": "info",
                "title": "Registre a primeira venda",
                "description": "Faturamento, ticket médio e margem começam a ser calculados assim que a primeira venda entrar em Nova venda.",
            })
        return pending
