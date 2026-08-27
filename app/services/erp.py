import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.repositories.store import Store, StoreTransaction
from app.schemas import (
    ChannelCreate,
    ChannelUpdate,
    ExpenseCreate,
    ExpenseUpdate,
    InventoryAdjustment,
    InventoryCount,
    InventoryItemCreate,
    InventoryItemUpdate,
    MovementUpdate,
    OrderCreate,
    OrderUpdate,
    ProductCreate,
    ProductUpdate,
)

# Marca os movimentos gerados pela venda. Todo o resto e lancamento manual:
# perda, quebra, degustacao ou recebimento.
SALE_REASON = "Venda registrada"

# Movimento que desfaz outro. Nao conta como perda nem como venda no consumo,
# senao editar um pedido apareceria na tela como se a loja tivesse jogado
# insumo fora e comprado de novo no mesmo dia.
REVERSAL_PREFIX = "Estorno"

# O Brasil nao usa horario de verao desde 2019, entao o fuso da loja e um
# deslocamento fixo — mesma escolha do modulo de previsao, e pelo mesmo motivo:
# nao depender de tzdata no Windows e ter o mesmo resultado no container.
FUSO_LOJA = timezone(timedelta(hours=-3))

CANCELLED = "Cancelado"

# Canais que a loja ja usa hoje. Viram documentos editaveis no primeiro acesso;
# dai em diante quem manda e o cadastro, nao esta lista.
DEFAULT_CHANNELS = (
    ("Balcao", 0.0, 0.0, "Venda no ponto, sem intermediário."),
    ("Delivery", 0.0, 0.0, "Entrega própria. Cadastre aqui o custo do entregador."),
    ("iFood", 27.0, 0.0, "Comissão do marketplace. Confirme o percentual do seu contrato."),
    ("WhatsApp", 0.0, 0.0, "Pedido direto pelo WhatsApp."),
)


def needs_restock(item: dict[str, Any]) -> bool:
    """Only alert when a minimum was actually configured.

    Items imported from the pricing workbook arrive with minimum zero, which
    means "not defined yet" and not "out of stock". Treating those as critical
    would bury the real alerts under one line per ingredient.
    """

    return item["minimum_quantity"] > 0 and item["quantity"] <= item["minimum_quantity"]


def is_delivery_order(order: dict[str, Any]) -> bool:
    """Pedido que saiu para entrega.

    A maioria das vendas e balcao. O sinalizador e explicito porque entrega com
    frete gratis existe: deduzir "e entrega" de um frete maior que zero deixaria
    essas de fora do acerto do entregador. O frete positivo entra como reforco
    para os pedidos importados antes do campo existir.
    """

    return bool(order.get("is_delivery")) or (order.get("delivery_fee") or 0) > 0


def is_active_order(order: dict[str, Any]) -> bool:
    """Pedido cancelado continua no historico, mas nao e faturamento.

    Ele fica visivel na tela de Pedidos para a loja saber que existiu e por que
    caiu; somar no painel faria o cancelamento nao ter efeito nenhum.
    """

    return order.get("status") != CANCELLED


class ERPService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def products(self) -> list[dict[str, Any]]:
        inventory = {item["id"]: item for item in self.store.list("inventory")}
        products = []
        for product in self.store.list("products"):
            cost = self._recipe_cost(product.get("recipe", []), inventory)
            products.append({
                **product,
                "line": product.get("line", "Classico"),
                "claims": product.get("claims", []),
                "nutrition": product.get("nutrition", {}),
                "cost": round(cost, 2),
                "margin": self._margin(product["price"], cost),
            })
        return sorted(products, key=lambda item: item["name"])

    def inventory(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("inventory"), key=lambda item: (not needs_restock(item), item["name"]))

    def orders(self, days: int | None = None) -> list[dict[str, Any]]:
        values = self.store.list("orders")
        if days:
            values = [order for order in values if self._timestamp(order["created_at"]) >= self._window_start(days)]
        return sorted(values, key=lambda item: self._timestamp(item["created_at"]), reverse=True)

    def expenses(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("expenses"), key=lambda item: (item["status"] == "Paga", item["due_date"]))

    def channels(self) -> list[dict[str, Any]]:
        """Cadastro de canais, semeado com o que a loja ja usa no primeiro acesso."""

        registered = self.store.list("channels")
        if not registered:
            for name, percent, fixed, note in DEFAULT_CHANNELS:
                channel = {
                    "id": self._identifier(name, "channel"),
                    "name": name,
                    "fee_percent": percent,
                    "fee_fixed": fixed,
                    "active": True,
                    "note": note,
                }
                self.store.put("channels", channel["id"], channel)
                registered.append(channel)
        return sorted(registered, key=lambda item: (not item.get("active", True), item["name"]))

    def audit(self, entity: str | None = None, entity_id: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
        """Quem mexeu em que. Sem isso, editar lancamento vira buraco no controle."""

        entries = self.store.list("audit_log")
        if entity:
            entries = [row for row in entries if row.get("entity") == entity]
        if entity_id:
            entries = [row for row in entries if row.get("entity_id") == entity_id]
        entries.sort(key=lambda row: row.get("at", ""), reverse=True)
        return entries[: max(1, min(limit, 300))]

    # ------------------------------------------------------------------
    # Painel
    # ------------------------------------------------------------------

    def dashboard(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(days, 90))
        # Dias fechados no fuso da loja, nao uma janela de horas em UTC. Duas
        # coisas dependiam disso: a venda das 21h caia na barra do dia seguinte,
        # e o grafico mostrava sete barras enquanto a primeira contava so o
        # pedaco do dia depois do horario em que o gestor abriu a tela.
        today = datetime.now(FUSO_LOJA).date()
        start = self._window_start(days)
        previous_start = self._window_start(days * 2)
        all_orders = [order for order in self.orders() if is_active_order(order)]
        current_orders = [order for order in all_orders if self._timestamp(order["created_at"]) >= start]
        previous_orders = [order for order in all_orders if previous_start <= self._timestamp(order["created_at"]) < start]
        revenue = sum(order["total"] for order in current_orders)
        # Pedido importado de fora traz o faturamento, mas nao os itens, entao o
        # custo e desconhecido — diferente de zero, que significaria "de graca" e
        # inflaria a margem. Margem e lucro contam so o que tem custo medido.
        com_custo = [order for order in current_orders if order.get("cost") is not None]
        revenue_com_custo = sum(order["total"] for order in com_custo)
        cost = sum(order["cost"] for order in com_custo)
        channel_fee = sum(order.get("channel_fee") or 0 for order in current_orders)
        delivery_fee = sum(order.get("delivery_fee") or 0 for order in current_orders)
        entregas = [order for order in current_orders if is_delivery_order(order)]
        previous_revenue = sum(order["total"] for order in previous_orders)
        average_ticket = revenue / len(current_orders) if current_orders else 0
        inventory = self.inventory()
        low_stock = [item for item in inventory if needs_restock(item)]
        by_day: dict[str, float] = defaultdict(float)
        labels: list[str] = []
        for offset in range(days - 1, -1, -1):
            label = (today - timedelta(days=offset)).isoformat()
            labels.append(label)
            by_day[label] = 0
        for order in current_orders:
            by_day[self._local_day(order["created_at"]).isoformat()] += order["total"]
        product_sales: Counter[str] = Counter()
        channels: Counter[str] = Counter()
        lines: Counter[str] = Counter()
        product_line = {product["id"]: product.get("line", "Classico") for product in self.store.list("products")}
        for order in current_orders:
            channels[order["channel"]] += order["total"]
            for item in order["items"]:
                product_sales[item["name"]] += item["quantity"]
                lines[product_line.get(item["product_id"], "Classico")] += item["line_total"]
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
                # O que os canais ficaram. Faturamento bruto engana quando um
                # terco das vendas passa por marketplace.
                "channel_fee": round(channel_fee, 2),
                "net_revenue": round(revenue - channel_fee, 2),
                # Frete e receita, mas nao e faturamento de produto: ele existe
                # para pagar a entrega. Separar os dois evita comemorar
                # crescimento que na verdade foi so mais gente pedindo em casa.
                "delivery_fee": round(delivery_fee, 2),
                "product_revenue": round(revenue - delivery_fee, 2),
                "delivery_orders": len(entregas),
                "delivery_share": round(len(entregas) / len(current_orders) * 100, 1) if current_orders else 0.0,
            },
            "sales_series": [{"date": label, "value": round(by_day[label], 2)} for label in labels],
            "top_products": [{"name": name, "quantity": quantity} for name, quantity in product_sales.most_common(4)],
            "channels": [{"name": name, "value": round(value, 2)} for name, value in channels.most_common()],
            "lines": [{"name": name, "value": round(value, 2)} for name, value in lines.most_common()],
            "low_stock": low_stock,
            "recent_orders": current_orders[:6],
            "insights": self._insights(current_orders, inventory, product_sales, channels),
        }

    def sales_day(self, day: str | None = None) -> dict[str, Any]:
        """Fechamento de um dia, no fuso da loja.

        O painel responde "como foi o periodo"; o caixa do fim do expediente
        precisa de outra coisa: quanto entrou hoje, por onde entrou, o que
        sobrou depois da taxa do canal e o que vence amanha.
        """

        reference = date.fromisoformat(day) if day else datetime.now(FUSO_LOJA).date()
        orders = [
            order for order in self.store.list("orders")
            if is_active_order(order) and self._local_day(order["created_at"]) == reference
        ]
        cancelled = [
            order for order in self.store.list("orders")
            if not is_active_order(order) and self._local_day(order["created_at"]) == reference
        ]
        revenue = sum(order["total"] for order in orders)
        com_custo = [order for order in orders if order.get("cost") is not None]
        cost = sum(order["cost"] for order in com_custo)
        revenue_com_custo = sum(order["total"] for order in com_custo)
        fee = sum(order.get("channel_fee") or 0 for order in orders)
        discount = sum(order.get("discount") or 0 for order in orders)
        frete = sum(order.get("delivery_fee") or 0 for order in orders)

        by_channel: Counter[str] = Counter()
        by_payment: Counter[str] = Counter()
        units: Counter[str] = Counter()
        product_value: Counter[str] = Counter()
        for order in orders:
            by_channel[order["channel"]] += order["total"]
            by_payment[order.get("payment_method") or "Não informado"] += order["total"]
            for item in order.get("items", []):
                units[item["name"]] += item["quantity"]
                product_value[item["name"]] += item["line_total"]

        # Perda lancada no mesmo dia: pertence ao resultado do dia tanto quanto a venda.
        stock = {item["id"]: item for item in self.store.list("inventory")}
        loss_cost = 0.0
        for movement in self.store.list("inventory_movements"):
            if self._local_day(movement["created_at"]) != reference:
                continue
            if movement["quantity_delta"] >= 0 or movement["reason"] == SALE_REASON:
                continue
            if movement["reason"].startswith(REVERSAL_PREFIX):
                continue
            item = stock.get(movement["inventory_id"])
            if item:
                loss_cost += abs(movement["quantity_delta"]) * item["unit_cost"]

        due = [expense for expense in self.store.list("expenses") if expense["due_date"] == reference.isoformat()]

        # Comparacao com o mesmo dia da semana, que e a referencia que a loja
        # usa de verdade: sabado so faz sentido contra sabado.
        same_weekday: list[float] = []
        history: dict[date, float] = defaultdict(float)
        for order in self.store.list("orders"):
            if not is_active_order(order):
                continue
            history[self._local_day(order["created_at"])] += order["total"]
        for other_day, value in history.items():
            if other_day != reference and other_day < reference and other_day.weekday() == reference.weekday():
                same_weekday.append(value)
        reference_average = sum(same_weekday) / len(same_weekday) if same_weekday else 0.0

        return {
            "date": reference.isoformat(),
            "weekday": ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")[reference.weekday()],
            "totals": {
                "revenue": round(revenue, 2),
                "orders": len(orders),
                "average_ticket": round(revenue / len(orders), 2) if orders else 0.0,
                "cost": round(cost, 2),
                "gross_profit": round(revenue_com_custo - cost, 2),
                "gross_margin": self._margin(revenue_com_custo, cost),
                "margin_coverage": round(revenue_com_custo / revenue * 100, 1) if revenue else 100.0,
                "channel_fee": round(fee, 2),
                "net_revenue": round(revenue - fee, 2),
                "discount": round(discount, 2),
                "delivery_fee": round(frete, 2),
                "loss_cost": round(loss_cost, 2),
                "cancelled": len(cancelled),
                "cancelled_value": round(sum(order["total"] for order in cancelled), 2),
            },
            "comparison": {
                "same_weekday_average": round(reference_average, 2),
                "samples": len(same_weekday),
                "change": self._change(revenue, reference_average) if same_weekday else None,
            },
            "by_channel": [{"name": name, "value": round(value, 2)} for name, value in by_channel.most_common()],
            "by_payment": [{"name": name, "value": round(value, 2)} for name, value in by_payment.most_common()],
            "by_product": [
                {"name": name, "quantity": quantity, "value": round(product_value[name], 2)}
                for name, quantity in units.most_common()
            ],
            "expenses_due": sorted(due, key=lambda row: row["amount"], reverse=True),
            "orders": sorted(orders, key=lambda row: row["created_at"], reverse=True),
        }

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------

    def create_order(self, payload: OrderCreate) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        occurred = self._occurrence(payload.occurred_at, now)
        order_id = str(uuid4())
        channel = self._channel_by_name(payload.channel)

        def create(transaction: StoreTransaction) -> dict[str, Any]:
            line_items, usage, total, total_cost = self._price_items(transaction, payload.items)
            if payload.discount > total:
                raise ValueError("O desconto não pode ser maior que o valor da venda.")

            number = self._next_number(transaction, now)
            for inventory_id, quantity in usage.items():
                self._move_stock(transaction, inventory_id, -quantity, SALE_REASON, occurred, order_id)

            # O frete entra no total porque e dinheiro que a loja recebe, mas
            # fica gravado a parte: ele existe para pagar a entrega, e somar os
            # dois esconderia se a entrega se paga.
            net = round(total - payload.discount + payload.delivery_fee, 2)
            order = {
                "id": order_id,
                "number": number,
                "created_at": occurred.isoformat(),
                "registered_at": now.isoformat(),
                "channel": payload.channel,
                "payment_method": payload.payment_method,
                "customer_name": payload.customer_name,
                "discount": payload.discount,
                "is_delivery": payload.is_delivery or payload.delivery_fee > 0,
                "delivery_fee": payload.delivery_fee,
                "courier_id": payload.courier_id,
                "total": net,
                "cost": round(total_cost, 2),
                "channel_fee": self._channel_fee(channel, net),
                "status": "Concluido",
                "items": line_items,
                # Guarda o que realmente saiu do estoque. Editar o pedido depois
                # precisa devolver exatamente isso — nao o que a ficha tecnica
                # diria hoje, que pode ja ter sido alterada.
                "stock_usage": [{"inventory_id": key, "quantity": round(value, 4)} for key, value in usage.items()],
            }
            transaction.put("orders", order["id"], order)
            self._audit(transaction, "order", order_id, "criado", f"Pedido #{number} registrado — {self._money(net)}")
            return order

        return self.store.run_transaction(create)

    def update_order(self, order_id: str, payload: OrderUpdate) -> dict[str, Any]:
        """Corrige um pedido ja lancado, refazendo a baixa de estoque.

        Errar o produto ou a quantidade e comum no balcao. Sem edicao a unica
        saida era cancelar e relancar, o que quebra a numeracao e polui o
        historico que a previsao le.
        """

        now = datetime.now(timezone.utc)

        def update(transaction: StoreTransaction) -> dict[str, Any]:
            order = transaction.get("orders", order_id)
            if not order:
                raise ValueError("Pedido não encontrado.")
            if not is_active_order(order):
                raise ValueError("Pedido cancelado não pode ser editado.")
            before = self._order_snapshot(order)

            if payload.items is not None:
                self._release_stock(transaction, order, f"{REVERSAL_PREFIX} de edição do pedido #{order['number']}", now)
                line_items, usage, total, total_cost = self._price_items(transaction, payload.items)
                discount = payload.discount if payload.discount is not None else order.get("discount", 0)
                if discount > total:
                    raise ValueError("O desconto não pode ser maior que o valor da venda.")
                for inventory_id, quantity in usage.items():
                    self._move_stock(transaction, inventory_id, -quantity, SALE_REASON, now, order_id)
                frete = payload.delivery_fee if payload.delivery_fee is not None else order.get("delivery_fee", 0)
                order["items"] = line_items
                order["stock_usage"] = [{"inventory_id": key, "quantity": round(value, 4)} for key, value in usage.items()]
                order["cost"] = round(total_cost, 2)
                order["total"] = round(total - discount + frete, 2)
                order["discount"] = discount
                order["delivery_fee"] = frete
            else:
                if payload.discount is not None or payload.delivery_fee is not None:
                    gross = self._gross_total(order)
                    desconto = payload.discount if payload.discount is not None else order.get("discount", 0)
                    frete = payload.delivery_fee if payload.delivery_fee is not None else order.get("delivery_fee", 0)
                    if gross and desconto > gross:
                        raise ValueError("O desconto não pode ser maior que o valor da venda.")
                    order["discount"] = desconto
                    order["delivery_fee"] = frete
                    if frete > 0:
                        order["is_delivery"] = True
                    if gross:
                        order["total"] = round(gross - desconto + frete, 2)
                if payload.total is not None:
                    # So o pedido sem itens aceita total digitado. Com ficha
                    # tecnica, o total e consequencia dos itens: deixar os dois
                    # editaveis criaria pedido cujo total nao bate com o que
                    # saiu do estoque.
                    if order.get("items"):
                        raise ValueError("Este pedido tem itens: corrija os itens para o total ser recalculado.")
                    order["total"] = round(payload.total, 2)

            if payload.channel is not None:
                self._channel_by_name(payload.channel)
                order["channel"] = payload.channel
            if payload.payment_method is not None:
                order["payment_method"] = payload.payment_method
            if payload.is_delivery is not None:
                order["is_delivery"] = payload.is_delivery
            if payload.courier_id is not None:
                # String vazia limpa o vinculo; um id precisa existir de fato,
                # senao o cruzamento com os turnos lancados nunca fecha.
                if payload.courier_id and not transaction.get("couriers", payload.courier_id):
                    raise ValueError("Entregador não encontrado.")
                order["courier_id"] = payload.courier_id or None
            if payload.customer_name is not None:
                order["customer_name"] = payload.customer_name or None
            if payload.occurred_at is not None:
                order["created_at"] = self._occurrence(payload.occurred_at, now).isoformat()

            order["channel_fee"] = self._channel_fee(self._channel_by_name(order["channel"]), order["total"])
            order["updated_at"] = now.isoformat()
            transaction.put("orders", order_id, order)
            self._audit(
                transaction, "order", order_id, "editado",
                f"Pedido #{order['number']} corrigido",
                self._diff(before, self._order_snapshot(order)),
            )
            return order

        return self.store.run_transaction(update)

    def cancel_order(self, order_id: str, reason: str = "Cancelado pela loja") -> dict[str, Any]:
        """Cancela sem apagar: devolve o insumo e tira o valor do faturamento.

        Excluir de vez seria mais simples e muito pior — sumiria a numeracao, o
        historico e a explicacao de por que aquele dia caiu.
        """

        now = datetime.now(timezone.utc)

        def cancel(transaction: StoreTransaction) -> dict[str, Any]:
            order = transaction.get("orders", order_id)
            if not order:
                raise ValueError("Pedido não encontrado.")
            if not is_active_order(order):
                raise ValueError("Este pedido já está cancelado.")
            self._release_stock(transaction, order, f"{REVERSAL_PREFIX} de venda cancelada #{order['number']}", now)
            order["status"] = CANCELLED
            order["cancelled_at"] = now.isoformat()
            order["cancel_reason"] = reason
            order["stock_usage"] = []
            transaction.put("orders", order_id, order)
            self._audit(transaction, "order", order_id, "cancelado", f"Pedido #{order['number']} cancelado — {reason}")
            return order

        return self.store.run_transaction(cancel)

    def delete_order(self, order_id: str) -> dict[str, str]:
        """Remocao definitiva, para pedido duplicado por importacao.

        So o que ja esta cancelado pode sair: assim a devolucao do estoque
        acontece uma vez so, no cancelamento, e nunca fica pendurada.
        """

        def remove(transaction: StoreTransaction) -> dict[str, str]:
            order = transaction.get("orders", order_id)
            if not order:
                raise ValueError("Pedido não encontrado.")
            if is_active_order(order):
                raise ValueError("Cancele o pedido antes de excluir, para o estoque voltar.")
            transaction.delete("orders", order_id)
            self._audit(transaction, "order", order_id, "excluido", f"Pedido #{order['number']} removido do histórico")
            return {"id": order_id, "status": "excluido"}

        return self.store.run_transaction(remove)

    # ------------------------------------------------------------------
    # Produtos
    # ------------------------------------------------------------------

    def create_product(self, payload: ProductCreate) -> dict[str, Any]:
        self._validate_recipe(payload.recipe)
        item = payload.model_dump()
        item["id"] = self._identifier(payload.name, "product")
        if self.store.get("products", item["id"]):
            item["id"] = f"{item['id']}-{str(uuid4())[:5]}"
        self.store.put("products", item["id"], item)
        self._audit(self.store, "product", item["id"], "criado", f"Produto {payload.name} adicionado ao cardápio")
        return item

    def update_product(self, product_id: str, payload: ProductUpdate) -> dict[str, Any]:
        product = self.store.get("products", product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        changes = payload.model_dump(exclude_unset=True)
        if "recipe" in changes and changes["recipe"] is not None:
            self._validate_recipe(payload.recipe or [])
        before = {key: product.get(key) for key in changes}
        product.update({key: value for key, value in changes.items() if value is not None})
        self.store.put("products", product_id, product)
        self._audit(
            self.store, "product", product_id, "editado",
            f"Produto {product['name']} atualizado",
            self._diff(before, {key: product.get(key) for key in changes}),
        )
        return product

    def delete_product(self, product_id: str) -> dict[str, str]:
        """Produto ja vendido nunca some: vira inativo.

        Apagar quebraria os pedidos antigos que apontam para ele e, com isso, o
        historico de faturamento e a previsao.
        """

        product = self.store.get("products", product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        sold = any(
            item["product_id"] == product_id
            for order in self.store.list("orders")
            for item in order.get("items", [])
        )
        if sold:
            product["active"] = False
            self.store.put("products", product_id, product)
            self._audit(self.store, "product", product_id, "inativado", f"{product['name']} saiu do cardápio (tem vendas no histórico)")
            return {"id": product_id, "status": "inativado", "detail": "O produto já tem vendas, então saiu do cardápio mas continua no histórico."}
        self.store.delete("products", product_id)
        self._audit(self.store, "product", product_id, "excluido", f"Produto {product['name']} removido")
        return {"id": product_id, "status": "excluido", "detail": "Produto removido do cardápio."}

    # ------------------------------------------------------------------
    # Estoque
    # ------------------------------------------------------------------

    def create_inventory_item(self, payload: InventoryItemCreate) -> dict[str, Any]:
        item = payload.model_dump()
        item["id"] = self._identifier(payload.name, "stock")
        if self.store.get("inventory", item["id"]):
            item["id"] = f"{item['id']}-{str(uuid4())[:5]}"
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("inventory", item["id"], item)
        self._audit(self.store, "inventory", item["id"], "criado", f"Insumo {payload.name} cadastrado")
        return item

    def update_inventory_item(self, inventory_id: str, payload: InventoryItemUpdate) -> dict[str, Any]:
        item = self.store.get("inventory", inventory_id)
        if not item:
            raise ValueError("Item de estoque não encontrado.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: item.get(key) for key in changes}
        item.update(changes)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("inventory", inventory_id, item)
        self._audit(
            self.store, "inventory", inventory_id, "editado",
            f"Insumo {item['name']} atualizado",
            self._diff(before, changes),
        )
        return item

    def delete_inventory_item(self, inventory_id: str) -> dict[str, str]:
        item = self.store.get("inventory", inventory_id)
        if not item:
            raise ValueError("Item de estoque não encontrado.")
        used_by = [
            product["name"]
            for product in self.store.list("products")
            for line in product.get("recipe", [])
            if line["inventory_id"] == inventory_id
        ]
        if used_by:
            raise ValueError(f"Este insumo está na ficha técnica de: {', '.join(sorted(set(used_by))[:4])}. Remova-o dessas receitas primeiro.")
        self.store.delete("inventory", inventory_id)
        self._audit(self.store, "inventory", inventory_id, "excluido", f"Insumo {item['name']} removido")
        return {"id": inventory_id, "status": "excluido"}

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

    def count_inventory(self, inventory_id: str, payload: InventoryCount) -> dict[str, Any]:
        """Contagem fisica: a loja informa o que existe, o sistema calcula a diferenca.

        Pedir a variacao obriga o gestor a fazer a subtracao de cabeca em cima
        de um saldo que ele ja desconfia estar errado — que e justamente o
        motivo de estar contando.
        """

        now = datetime.now(timezone.utc)

        def count(transaction: StoreTransaction) -> dict[str, Any]:
            item = transaction.get("inventory", inventory_id)
            if not item:
                raise ValueError("Item de estoque não encontrado.")
            difference = round(payload.quantity - item["quantity"], 3)
            previous = item["quantity"]
            item["quantity"] = round(payload.quantity, 3)
            item["updated_at"] = now.isoformat()
            item["counted_at"] = now.isoformat()
            transaction.put("inventory", inventory_id, item)
            if difference:
                movement = {
                    "id": str(uuid4()),
                    "inventory_id": inventory_id,
                    "quantity_delta": difference,
                    "reason": payload.reason,
                    "created_at": now.isoformat(),
                    "kind": "contagem",
                }
                transaction.put("inventory_movements", movement["id"], movement)
            self._audit(
                transaction, "inventory", inventory_id, "contagem",
                f"{item['name']}: {previous} → {item['quantity']} {item['unit']} ({difference:+})",
            )
            return {**item, "difference": difference, "previous_quantity": previous}

        return self.store.run_transaction(count)

    def update_movement(self, movement_id: str, payload: MovementUpdate) -> dict[str, Any]:
        """Corrige um lancamento de consumo, devolvendo o saldo da diferenca.

        Lancou 5 kg de granola quando foram 500 g: a correcao precisa mexer no
        saldo tambem, senao o estoque fica mentindo com o registro certo.
        """

        now = datetime.now(timezone.utc)

        def update(transaction: StoreTransaction) -> dict[str, Any]:
            movement = transaction.get("inventory_movements", movement_id)
            if not movement:
                raise ValueError("Movimentação não encontrada.")
            self._require_manual(movement, "Edite o pedido para corrigi-la.")
            item = transaction.get("inventory", movement["inventory_id"])
            if not item:
                raise ValueError("O insumo desta movimentação não existe mais.")
            before = {"quantity_delta": movement["quantity_delta"], "reason": movement["reason"]}

            if payload.quantity_delta is not None:
                correction = payload.quantity_delta - movement["quantity_delta"]
                if item["quantity"] + correction < 0:
                    raise ValueError("A correção deixaria o estoque negativo.")
                item["quantity"] = round(item["quantity"] + correction, 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", item["id"], item)
                movement["quantity_delta"] = payload.quantity_delta
            if payload.reason is not None:
                movement["reason"] = payload.reason
            movement["updated_at"] = now.isoformat()
            transaction.put("inventory_movements", movement_id, movement)
            self._audit(
                transaction, "movement", movement_id, "editado",
                f"Movimentação de {item['name']} corrigida",
                self._diff(before, {"quantity_delta": movement["quantity_delta"], "reason": movement["reason"]}),
            )
            return {**movement, "name": item["name"], "unit": item["unit"]}

        return self.store.run_transaction(update)

    def delete_movement(self, movement_id: str) -> dict[str, str]:
        """Estorna um lancamento manual: devolve o saldo e apaga o registro."""

        now = datetime.now(timezone.utc)

        def remove(transaction: StoreTransaction) -> dict[str, str]:
            movement = transaction.get("inventory_movements", movement_id)
            if not movement:
                raise ValueError("Movimentação não encontrada.")
            self._require_manual(movement, "Cancele o pedido para desfazê-la.")
            item = transaction.get("inventory", movement["inventory_id"])
            if item:
                if item["quantity"] - movement["quantity_delta"] < 0:
                    raise ValueError("Desfazer este lançamento deixaria o estoque negativo.")
                item["quantity"] = round(item["quantity"] - movement["quantity_delta"], 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", item["id"], item)
            transaction.delete("inventory_movements", movement_id)
            self._audit(transaction, "movement", movement_id, "excluido", f"Lançamento desfeito — saldo devolvido ao estoque")
            return {"id": movement_id, "status": "excluido"}

        return self.store.run_transaction(remove)

    @staticmethod
    def _require_manual(movement: dict[str, Any], remedy: str) -> None:
        """So lancamento manual pode ser corrigido pela tela de Consumo.

        Baixa de venda e estorno de cancelamento existem porque um pedido
        existe. Mexer neles por fora descolaria o estoque do pedido que os
        gerou, e a diferenca so apareceria na proxima contagem — sem nada
        no historico explicando de onde veio.
        """

        if movement["reason"] == SALE_REASON:
            raise ValueError(f"Esta baixa veio de uma venda. {remedy}")
        if movement["reason"].startswith(REVERSAL_PREFIX):
            raise ValueError(f"Este lançamento é o estorno de um pedido. {remedy}")

    def consumption(self, days: int = 7) -> dict[str, Any]:
        """Turn the movement log into what the owner actually needs to decide.

        Movements are already written by every sale and every manual
        adjustment, but nothing reads them. Sales and losses are separated
        because they mean opposite things: one produced revenue, the other
        just evaporated.
        """

        days = max(1, min(days, 90))
        # Mesmo fuso do painel: baixa lancada a noite pertence ao dia em que a
        # loja trabalhou, nao ao seguinte.
        today = datetime.now(FUSO_LOJA).date()
        start = self._window_start(days)
        stock = {item["id"]: item for item in self.store.list("inventory")}

        by_item: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, float]] = {}
        for offset in range(days - 1, -1, -1):
            by_day[(today - timedelta(days=offset)).isoformat()] = {"sale_cost": 0.0, "loss_cost": 0.0}

        history: list[dict[str, Any]] = []
        for movement in self.store.list("inventory_movements"):
            moment = self._timestamp(movement["created_at"])
            if moment < start:
                continue
            item = stock.get(movement["inventory_id"])
            if not item:
                continue
            delta = movement["quantity_delta"]
            reason = movement["reason"]
            # Estorno e correcao, nao consumo. Somar como perda faria a edicao
            # de um pedido aparecer no relatorio como insumo jogado fora.
            reversal = reason.startswith(REVERSAL_PREFIX)
            kind = (
                "estorno" if reversal
                else "entrada" if delta > 0
                else "venda" if reason == SALE_REASON
                else "perda"
            )
            quantity = abs(delta)
            cost = round(quantity * item["unit_cost"], 2)
            day = moment.astimezone(FUSO_LOJA).date().isoformat()
            if kind in ("venda", "perda") and day in by_day:
                by_day[day]["sale_cost" if kind == "venda" else "loss_cost"] += cost

            if kind != "estorno":
                row = by_item.setdefault(
                    movement["inventory_id"],
                    {"inventory_id": item["id"], "name": item["name"], "unit": item["unit"], "sold": 0.0, "lost": 0.0, "received": 0.0, "cost": 0.0},
                )
                row["sold" if kind == "venda" else "lost" if kind == "perda" else "received"] += quantity
                if kind != "entrada":
                    row["cost"] += cost
            history.append({
                **movement,
                "name": item["name"],
                "unit": item["unit"],
                "kind": kind,
                "cost": cost,
                # A tela precisa saber o que pode oferecer para editar: baixa de
                # venda so muda pelo pedido.
                "editable": reason != SALE_REASON and not reversal,
            })

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
            "history": history[:60],
        }

    # ------------------------------------------------------------------
    # Despesas
    # ------------------------------------------------------------------

    def create_expense(self, payload: ExpenseCreate) -> dict[str, Any]:
        expense = payload.model_dump()
        expense["id"] = str(uuid4())
        expense["created_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("expenses", expense["id"], expense)
        self._audit(self.store, "expense", expense["id"], "criado", f"{payload.description} — {self._money(payload.amount)}")
        return expense

    def update_expense(self, expense_id: str, payload: ExpenseUpdate) -> dict[str, Any]:
        expense = self.store.get("expenses", expense_id)
        if not expense:
            raise ValueError("Despesa não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: expense.get(key) for key in changes}
        expense.update(changes)
        expense["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("expenses", expense_id, expense)
        self._audit(
            self.store, "expense", expense_id, "editado",
            f"{expense['description']} atualizada",
            self._diff(before, changes),
        )
        return expense

    def delete_expense(self, expense_id: str) -> dict[str, str]:
        expense = self.store.get("expenses", expense_id)
        if not expense:
            raise ValueError("Despesa não encontrada.")
        self.store.delete("expenses", expense_id)
        self._audit(self.store, "expense", expense_id, "excluido", f"{expense['description']} — {self._money(expense['amount'])} removida")
        return {"id": expense_id, "status": "excluido"}

    # ------------------------------------------------------------------
    # Canais de distribuicao
    # ------------------------------------------------------------------

    def create_channel(self, payload: ChannelCreate) -> dict[str, Any]:
        channel = payload.model_dump()
        channel["id"] = self._identifier(payload.name, "channel")
        if self.store.get("channels", channel["id"]):
            raise ValueError("Já existe um canal com esse nome.")
        self.store.put("channels", channel["id"], channel)
        self._audit(self.store, "channel", channel["id"], "criado", f"Canal {payload.name} cadastrado")
        return channel

    def update_channel(self, channel_id: str, payload: ChannelUpdate) -> dict[str, Any]:
        channel = self.store.get("channels", channel_id)
        if not channel:
            raise ValueError("Canal não encontrado.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: channel.get(key) for key in changes}
        channel.update(changes)
        self.store.put("channels", channel_id, channel)
        self._audit(
            self.store, "channel", channel_id, "editado",
            f"Canal {channel['name']} atualizado",
            self._diff(before, changes),
        )
        return channel

    def delete_channel(self, channel_id: str) -> dict[str, str]:
        """Canal com venda no historico e desativado, nunca apagado.

        Apagar deixaria pedidos apontando para um canal inexistente e a taxa
        daquelas vendas ficaria sem explicacao.
        """

        channel = self.store.get("channels", channel_id)
        if not channel:
            raise ValueError("Canal não encontrado.")
        used = any(order.get("channel") == channel["name"] for order in self.store.list("orders"))
        if used:
            channel["active"] = False
            self.store.put("channels", channel_id, channel)
            self._audit(self.store, "channel", channel_id, "inativado", f"Canal {channel['name']} desativado")
            return {"id": channel_id, "status": "inativado", "detail": "O canal tem vendas no histórico, então foi desativado em vez de excluído."}
        self.store.delete("channels", channel_id)
        self._audit(self.store, "channel", channel_id, "excluido", f"Canal {channel['name']} removido")
        return {"id": channel_id, "status": "excluido", "detail": "Canal removido."}

    def channel_performance(self, days: int = 30) -> dict[str, Any]:
        """Quanto cada canal deixa depois da taxa dele.

        Faturamento por canal ja aparecia no painel, mas faturamento nao e o
        que decide onde investir: o iFood pode liderar em receita e ficar em
        ultimo em dinheiro que fica na loja.
        """

        days = max(1, min(days, 365))
        start = self._window_start(days)
        registered = {channel["name"]: channel for channel in self.channels()}
        rows: dict[str, dict[str, Any]] = {}
        for order in self.store.list("orders"):
            if not is_active_order(order) or self._timestamp(order["created_at"]) < start:
                continue
            name = order["channel"]
            channel = registered.get(name, {"fee_percent": 0, "fee_fixed": 0, "active": True, "note": None})
            row = rows.setdefault(name, {
                "name": name,
                "fee_percent": channel.get("fee_percent", 0),
                "fee_fixed": channel.get("fee_fixed", 0),
                "active": channel.get("active", True),
                "note": channel.get("note"),
                "registered": name in registered,
                "orders": 0, "revenue": 0.0, "fee": 0.0, "cost": 0.0, "revenue_with_cost": 0.0,
            })
            row["orders"] += 1
            row["revenue"] += order["total"]
            row["fee"] += order.get("channel_fee") or self._channel_fee(channel, order["total"])
            if order.get("cost") is not None:
                row["cost"] += order["cost"]
                row["revenue_with_cost"] += order["total"]

        result = []
        for row in rows.values():
            net = row["revenue"] - row["fee"]
            contribution = net - row["cost"] if row["revenue_with_cost"] else None
            result.append({
                **row,
                "revenue": round(row["revenue"], 2),
                "fee": round(row["fee"], 2),
                "cost": round(row["cost"], 2),
                "net_revenue": round(net, 2),
                "average_ticket": round(row["revenue"] / row["orders"], 2) if row["orders"] else 0.0,
                "contribution": round(contribution, 2) if contribution is not None else None,
                # Margem sobre o que sobra depois da taxa: e esse o numero que
                # responde "vale a pena vender por aqui".
                "contribution_margin": (
                    round(contribution / row["revenue_with_cost"] * 100, 1)
                    if contribution is not None and row["revenue_with_cost"] else None
                ),
                "share": 0.0,
            })
        total_revenue = sum(row["revenue"] for row in result) or 1
        for row in result:
            row["share"] = round(row["revenue"] / total_revenue * 100, 1)
        result.sort(key=lambda row: row["revenue"], reverse=True)

        idle = [channel["name"] for channel in self.channels() if channel.get("active", True) and channel["name"] not in rows]
        return {
            "period_days": days,
            "channels": result,
            "totals": {
                "revenue": round(sum(row["revenue"] for row in result), 2),
                "fee": round(sum(row["fee"] for row in result), 2),
                "net_revenue": round(sum(row["net_revenue"] for row in result), 2),
            },
            "idle_channels": idle,
        }

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _price_items(self, transaction: StoreTransaction, lines: list[Any]) -> tuple[list[dict[str, Any]], dict[str, float], float, float]:
        """Precifica os itens e calcula o que sai do estoque, sem gravar nada."""

        products: dict[str, dict[str, Any]] = {}
        needed: Counter[str] = Counter()
        for line in lines:
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
        for line in lines:
            product = products[line.product_id]
            unit_cost = self._recipe_cost(product.get("recipe", []), stock)
            line_total = product["price"] * line.quantity
            total += line_total
            total_cost += unit_cost * line.quantity
            line_items.append({
                "product_id": product["id"],
                "name": product["name"],
                "quantity": line.quantity,
                "unit_price": product["price"],
                "unit_cost": round(unit_cost, 2),
                "line_total": round(line_total, 2),
            })
        # Receita apontando para insumo removido nao tem o que baixar, mas
        # tambem nao pode impedir a venda de ser registrada.
        usage = {key: value for key, value in needed.items() if key in stock}
        return line_items, usage, total, total_cost

    def _release_stock(self, transaction: StoreTransaction, order: dict[str, Any], reason: str, now: datetime) -> None:
        """Devolve ao estoque exatamente o que o pedido consumiu."""

        usage = order.get("stock_usage")
        if usage is None:
            # Pedido gravado antes de stock_usage existir: reconstroi pela ficha
            # tecnica atual. E aproximado, e por isso o motivo do movimento diz
            # de onde veio — a conferencia fica possivel.
            recomposed: Counter[str] = Counter()
            for item in order.get("items", []):
                product = transaction.get("products", item["product_id"])
                for line in (product or {}).get("recipe", []):
                    recomposed[line["inventory_id"]] += line["quantity"] * item["quantity"]
            usage = [{"inventory_id": key, "quantity": value} for key, value in recomposed.items()]
        for line in usage:
            self._move_stock(transaction, line["inventory_id"], line["quantity"], reason, now, order["id"])

    @staticmethod
    def _move_stock(
        transaction: StoreTransaction,
        inventory_id: str,
        delta: float,
        reason: str,
        moment: datetime,
        order_id: str | None = None,
    ) -> None:
        item = transaction.get("inventory", inventory_id)
        if not item:
            return
        item["quantity"] = round(item["quantity"] + delta, 3)
        item["updated_at"] = moment.isoformat()
        transaction.put("inventory", inventory_id, item)
        movement: dict[str, Any] = {
            "id": str(uuid4()),
            "inventory_id": inventory_id,
            "quantity_delta": round(delta, 3),
            "reason": reason,
            "created_at": moment.isoformat(),
        }
        if order_id:
            movement["order_id"] = order_id
        transaction.put("inventory_movements", movement["id"], movement)

    @staticmethod
    def _next_number(transaction: StoreTransaction, now: datetime) -> int:
        sequence = transaction.get("system", "order_sequence")
        if sequence:
            sequence_value = int(sequence.get("value", 1000))
        else:
            sequence_value = max(
                (int(order["number"]) for order in transaction.list("orders") if str(order.get("number", "")).isdigit()),
                default=1000,
            )
        number = sequence_value + 1
        transaction.put("system", "order_sequence", {"id": "order_sequence", "value": number, "updated_at": now.isoformat()})
        return number

    def _channel_by_name(self, name: str) -> dict[str, Any]:
        channel = next((row for row in self.channels() if row["name"] == name), None)
        if not channel:
            raise ValueError(f"Canal '{name}' não está cadastrado. Cadastre-o em Canais antes de usar.")
        return channel

    @staticmethod
    def _channel_fee(channel: dict[str, Any], total: float) -> float:
        return round(total * (channel.get("fee_percent", 0) / 100) + channel.get("fee_fixed", 0), 2)

    @staticmethod
    def _gross_total(order: dict[str, Any]) -> float:
        return round(sum(item["line_total"] for item in order.get("items", [])), 2)

    def _validate_recipe(self, recipe: list[Any]) -> None:
        inventory_ids = {item["id"] for item in self.store.list("inventory")}
        invalid = [line.inventory_id for line in recipe if line.inventory_id not in inventory_ids]
        if invalid:
            raise ValueError("A receita inclui um insumo que não existe no estoque.")

    def _occurrence(self, requested: datetime | None, now: datetime) -> datetime:
        if requested is None:
            return now
        moment = requested if requested.tzinfo else requested.replace(tzinfo=FUSO_LOJA)
        moment = moment.astimezone(timezone.utc)
        if moment > now:
            raise ValueError("A data do pedido não pode estar no futuro.")
        return moment

    def _audit(self, sink: Any, entity: str, entity_id: str, action: str, summary: str, changes: dict[str, Any] | None = None) -> None:
        entry = {
            "id": str(uuid4()),
            "entity": entity,
            "entity_id": entity_id,
            "action": action,
            "summary": summary,
            "changes": changes or {},
            "at": datetime.now(timezone.utc).isoformat(),
        }
        sink.put("audit_log", entry["id"], entry)

    @staticmethod
    def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """So o que mudou de fato, em formato legivel na tela de histórico."""

        return {
            key: {"de": before.get(key), "para": value}
            for key, value in after.items()
            if before.get(key) != value
        }

    @staticmethod
    def _order_snapshot(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "total": order.get("total"),
            "discount": order.get("discount"),
            "channel": order.get("channel"),
            "payment_method": order.get("payment_method"),
            "created_at": order.get("created_at"),
            "itens": ", ".join(f"{item['quantity']}x {item['name']}" for item in order.get("items", [])) or None,
        }

    @staticmethod
    def _money(value: float) -> str:
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

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

    @classmethod
    def _local_day(cls, value: Any) -> date:
        return cls._timestamp(value).astimezone(FUSO_LOJA).date()

    @staticmethod
    def _window_start(days: int) -> datetime:
        """Inicio do primeiro dos ultimos N dias, no fuso da loja.

        Janela em horas ("agora menos 24h") descarta a venda da manha quando o
        gestor abre o painel a tarde, e com days=1 tem largura zero: o pedido
        registrado um segundo antes ja fica de fora. O gestor pensa em dias
        fechados, entao a janela comeca a meia-noite do primeiro dia.
        """

        first = datetime.now(FUSO_LOJA).date() - timedelta(days=days - 1)
        return datetime.combine(first, time.min, tzinfo=FUSO_LOJA).astimezone(timezone.utc)

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
                "description": f"Os {len(inventory)} insumos vieram da precificação sem saldo. Use Contagem para lançar o que existe hoje e o estoque passa a baixar sozinho a cada venda.",
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
