import re
import unicodedata
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.repositories.store import Store, StoreTransaction
from app.schemas import (
    AddonCreate,
    AddonUpdate,
    ChannelCreate,
    ChannelUpdate,
    ExpenseCreate,
    ExpenseSeriesUpdate,
    ExpenseUpdate,
    FixedCostSetup,
    InventoryAdjustment,
    InventoryCount,
    InventoryItemCreate,
    InventoryItemUpdate,
    InventoryPurchase,
    InventoryWithdrawal,
    MarketPriceCreate,
    MarketPriceUpdate,
    MovementUpdate,
    OrderCreate,
    OrderUpdate,
    ProductCreate,
    ProductionRecipeCreate,
    ProductionRecipeUpdate,
    ProductionRun,
    ProductUpdate,
    SportAnchorCreate,
    SportAnchorUpdate,
)

# Marca os movimentos gerados pela venda. Todo o resto e lancamento manual:
# perda, quebra, degustacao ou recebimento.
SALE_REASON = "Venda registrada"

# Compra é diferente de uma entrada por ajuste: além de aumentar o saldo, o
# custo recebido atualiza a média usada pela ficha técnica e pela reposição.
PURCHASE_REASON = "Compra recebida"

# Retirada manual para uso/operacao. Ela nao e quebra nem vencimento: entra no
# consumo e no planejamento, mas aparece separada para nao mascarar perdas.
WITHDRAWAL_KIND = "retirada"

# Fabricacao: a barra de acai vira base pronta. E o unico movimento que tira de
# um item e poe em outro, e o custo nao some — muda de lugar. Por isso ele fica
# fora de perda, de retirada e de quebra em toda conta de resultado: contar aqui
# e de novo quando o copo for vendido cobraria o mesmo acai duas vezes.
PRODUCTION_KIND = "fabricacao"
PRODUCTION_REASON = "Fabricação"

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


def month_index(month: str) -> int:
    """Converte AAAA-MM num numero, para contar meses sem cair em aritmetica de data."""

    return int(month[:4]) * 12 + int(month[5:7]) - 1


def month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_name(month: str) -> str:
    """AAAA-MM como a loja escreve: 08/2026."""

    return f"{month[5:7]}/{month[:4]}"


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
        self.materialize_recurring_expenses()
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

    def operating_result(self, days: int = 30, delivery_cost: float = 0.0) -> dict[str, Any]:
        """DRE operacional curta, calculada somente com custos comprovados.

        Compra de estoque nao entra duas vezes: ela atualiza o valor do insumo
        e passa ao resultado quando uma venda consome a receita. Pedidos
        importados sem itens continuam no faturamento, mas deixam o lucro em
        aberto em vez de receberem um custo inventado.
        """

        days = max(1, min(days, 365))
        # O aluguel de cada mes da janela precisa existir antes de a conta ser
        # feita. Sem isso o trimestre mostraria um aluguel onde ha tres.
        self.materialize_recurring_expenses()
        start = self._window_start(days)
        first_day = start.astimezone(FUSO_LOJA).date()
        last_day = datetime.now(FUSO_LOJA).date()
        orders = [
            order for order in self.store.list("orders")
            if is_active_order(order) and self._timestamp(order["created_at"]) >= start
        ]
        channels = {channel["name"]: channel for channel in self.channels()}
        inventory = {item["id"]: item for item in self.store.list("inventory")}

        revenue = sum(order["total"] for order in orders)
        measured_orders = [order for order in orders if order.get("cost") is not None]
        unmeasured_orders = [order for order in orders if order.get("cost") is None]
        measured_revenue = sum(order["total"] for order in measured_orders)
        cost_of_goods = sum(order["cost"] for order in measured_orders)
        estimated_channel_fees = 0.0
        channel_fees = 0.0
        products: dict[str, dict[str, Any]] = {}
        for order in orders:
            stored_fee = order.get("channel_fee")
            if stored_fee is None:
                fee = self._channel_fee(channels.get(order.get("channel"), {}), order["total"])
                estimated_channel_fees += fee
            else:
                fee = stored_fee
            channel_fees += fee
            for line in order.get("items", []):
                row = products.setdefault(line["product_id"], {
                    "product_id": line["product_id"], "name": line["name"],
                    "units": 0, "revenue": 0.0, "cost": 0.0,
                })
                row["units"] += line["quantity"]
                row["revenue"] += line["line_total"]
                row["cost"] += line.get("unit_cost", 0) * line["quantity"]

        losses = 0.0
        for movement in self.store.list("inventory_movements"):
            if self._timestamp(movement["created_at"]) < start:
                continue
            if movement["quantity_delta"] >= 0 or movement["reason"] == SALE_REASON:
                continue
            if movement["reason"].startswith(REVERSAL_PREFIX):
                continue
            if movement.get("kind") in (WITHDRAWAL_KIND, PRODUCTION_KIND):
                continue
            item = inventory.get(movement["inventory_id"])
            if item:
                losses += movement.get("total_cost", abs(movement["quantity_delta"]) * item["unit_cost"])

        withdrawals = sum(
            movement.get("total_cost", abs(movement["quantity_delta"]) * inventory[movement["inventory_id"]]["unit_cost"])
            for movement in self.store.list("inventory_movements")
            if movement.get("kind") == WITHDRAWAL_KIND
            and movement["quantity_delta"] < 0
            and self._timestamp(movement["created_at"]) >= start
            and movement["inventory_id"] in inventory
        )

        recognised_expenses: list[dict[str, Any]] = []
        inventory_purchases_expense = 0.0
        courier_expense = 0.0
        paid_expenses = 0.0
        pending_expenses = 0.0
        for expense in self.store.list("expenses"):
            try:
                due_date = date.fromisoformat(str(expense["due_date"])[:10])
            except (KeyError, TypeError, ValueError):
                continue
            if not first_day <= due_date <= last_day:
                continue
            category = self._normalized_text(expense.get("category", ""))
            # Compras de polpa, fruta e embalagem ja entram no custo da receita
            # quando vendidas. Somar a despesa aqui duplicaria o mesmo insumo.
            if "insumo" in category or "compra" in category:
                inventory_purchases_expense += expense["amount"]
                continue
            # Mesma logica para a entrega: quando ha fechamento de turno no
            # periodo, o que a loja pagou ao entregador ja entrou por
            # `delivery_cost`. Sem fechamento nenhum, esta despesa e o unico
            # registro do custo e precisa ficar.
            if "entrega" in category and delivery_cost > 0:
                courier_expense += expense["amount"]
                continue
            recognised_expenses.append(expense)
            if expense.get("status") == "Paga":
                paid_expenses += expense["amount"]
            else:
                pending_expenses += expense["amount"]

        operating_expenses = sum(expense["amount"] for expense in recognised_expenses)
        purchase_cash = sum(
            movement.get("total_cost", 0)
            for movement in self.store.list("inventory_movements")
            if movement.get("kind") == "compra" and self._timestamp(movement["created_at"]) >= start
        )
        delivery_cost = max(0.0, delivery_cost)
        direct_cost = cost_of_goods + channel_fees + losses + withdrawals + delivery_cost
        upper_bound = revenue - direct_cost - operating_expenses
        cost_coverage = round(measured_revenue / revenue * 100, 1) if revenue else 100.0
        complete = not unmeasured_orders
        product_rows = []
        for row in products.values():
            contribution = row["revenue"] - row["cost"]
            product_rows.append({
                **row,
                "revenue": round(row["revenue"], 2),
                "cost": round(row["cost"], 2),
                "contribution": round(contribution, 2),
                "margin": self._margin(row["revenue"], row["cost"]),
            })
        product_rows.sort(key=lambda row: row["revenue"], reverse=True)

        blockers: list[str] = []
        if unmeasured_orders:
            blockers.append(
                f"{len(unmeasured_orders)} pedido(s), somando {self._money(sum(order['total'] for order in unmeasured_orders))}, "
                "vieram da Anota AI sem os itens vendidos. Registre os itens para fechar o custo e o lucro."
            )
        if estimated_channel_fees:
            blockers.append(
                "Parte das taxas foi estimada com a tabela atual de canais porque o pedido importado nao trouxe a taxa gravada no dia."
            )
        if inventory_purchases_expense:
            blockers.append(
                f"{self._money(inventory_purchases_expense)} em despesas de insumo ficaram fora da DRE para nao duplicar o custo vendido."
            )
        if courier_expense:
            blockers.append(
                f"{self._money(courier_expense)} em despesas de entrega ficaram fora da DRE porque os turnos lancados em Entregas ja respondem por esse custo."
            )

        return {
            "period_days": days,
            "from": first_day.isoformat(),
            "to": last_day.isoformat(),
            "complete": complete,
            "cost_coverage": cost_coverage,
            "products": product_rows,
            "blockers": blockers,
            "totals": {
                "revenue": round(revenue, 2),
                "measured_revenue": round(measured_revenue, 2),
                "revenue_without_cost": round(revenue - measured_revenue, 2),
                "orders": len(orders),
                "orders_without_cost": len(unmeasured_orders),
                "cost_of_goods": round(cost_of_goods, 2),
                "channel_fees": round(channel_fees, 2),
                "estimated_channel_fees": round(estimated_channel_fees, 2),
                "inventory_losses": round(losses, 2),
                "inventory_withdrawals": round(withdrawals, 2),
                "delivery_cost": round(delivery_cost, 2),
                "operating_expenses": round(operating_expenses, 2),
                "paid_expenses": round(paid_expenses, 2),
                "pending_expenses": round(pending_expenses, 2),
                "inventory_purchase_expenses": round(inventory_purchases_expense, 2),
                "courier_expenses_excluded": round(courier_expense, 2),
                "inventory_purchase_cash": round(purchase_cash, 2),
                "total_operating_cost": round(direct_cost + operating_expenses, 2),
                "operating_profit": round(upper_bound, 2) if complete else None,
                # Enquanto houver custo desconhecido, este e apenas o teto do
                # lucro: o custo faltante so pode reduzir o resultado.
                "profit_upper_bound": round(upper_bound, 2),
                "operating_margin": self._margin(revenue, direct_cost + operating_expenses) if complete else None,
            },
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

        # Perda lancada no mesmo dia pertence ao resultado; retirada e uma
        # saida operacional separada, para nao aparecer como quebra.
        stock = {item["id"]: item for item in self.store.list("inventory")}
        loss_cost = 0.0
        withdrawal_cost = 0.0
        for movement in self.store.list("inventory_movements"):
            if self._local_day(movement["created_at"]) != reference:
                continue
            if movement["quantity_delta"] >= 0 or movement["reason"] == SALE_REASON:
                continue
            if movement["reason"].startswith(REVERSAL_PREFIX):
                continue
            item = stock.get(movement["inventory_id"])
            if item:
                # Fabricacao fica de fora das duas: o custo da barra vira custo
                # da base e ja aparece quando o copo e vendido.
                if movement.get("kind") == PRODUCTION_KIND:
                    continue
                cost = movement.get("total_cost", abs(movement["quantity_delta"]) * item["unit_cost"])
                if movement.get("kind") == WITHDRAWAL_KIND:
                    withdrawal_cost += cost
                else:
                    loss_cost += cost

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
                "withdrawal_cost": round(withdrawal_cost, 2),
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

    def delete_product(self, product_id: str, permanent: bool = False) -> dict[str, str]:
        """Tira o produto do cardapio. Vendido, sai em duas etapas.

        O primeiro clique apenas inativa: o produto continua respondendo pelos
        pedidos que ja aconteceram. So depois, com `permanent`, ele sai do
        cadastro de vez — o pedido antigo nao quebra porque cada linha guarda
        nome, preco e custo do dia da venda, mas o dossie e a previsao daquele
        produto deixam de existir, e por isso a decisao e explicita.
        """

        product = self.store.get("products", product_id)
        if not product:
            raise ValueError("Produto não encontrado.")
        sold = any(
            item["product_id"] == product_id
            for order in self.store.list("orders")
            for item in order.get("items", [])
        )
        if sold and not permanent:
            if product.get("active", True) is False:
                raise ValueError(
                    "Este produto já está fora do cardápio e tem vendas no histórico. "
                    "Use a exclusão definitiva para removê-lo do cadastro."
                )
            product["active"] = False
            self.store.put("products", product_id, product)
            self._audit(self.store, "product", product_id, "inativado", f"{product['name']} saiu do cardápio (tem vendas no histórico)")
            return {"id": product_id, "status": "inativado", "detail": "O produto já tem vendas, então saiu do cardápio mas continua no histórico."}
        self.store.delete("products", product_id)
        detalhe = (
            "Produto removido do cadastro. Os pedidos antigos mantêm nome, preço e custo do dia da venda."
            if sold else "Produto removido do cardápio."
        )
        self._audit(
            self.store, "product", product_id, "excluido",
            f"Produto {product['name']} removido" + (" em definitivo (tinha vendas no histórico)" if sold else ""),
        )
        return {"id": product_id, "status": "excluido", "detail": detalhe}

    # ------------------------------------------------------------------
    # Complementos
    # ------------------------------------------------------------------

    def addons(self) -> list[dict[str, Any]]:
        """Cadastro de complementos, ja com o custo da porcao pelo estoque de hoje."""

        inventory = {item["id"]: item for item in self.store.list("inventory")}
        rows = []
        for addon in self.store.list("addons"):
            item = inventory.get(addon["inventory_id"])
            cost = (item or {}).get("unit_cost", 0) * addon["quantity"]
            rows.append({
                **addon,
                "inventory_name": (item or {}).get("name"),
                "unit": (item or {}).get("unit"),
                "in_stock": (item or {}).get("quantity"),
                # Porcoes que o saldo de hoje ainda aguenta. E o numero que diz
                # se vale anunciar o complemento no balcao amanha.
                # A divisao vai com folga porque 4,1 / 0,02 da 204,99... em ponto
                # flutuante, e o balcao contaria 205 doses na prateleira.
                "portions_available": int((item or {}).get("quantity", 0) / addon["quantity"] + 1e-9) if item and addon["quantity"] else None,
                "cost": round(cost, 4),
                "contribution": round(addon["price"] - cost, 2),
                "margin": self._margin(addon["price"], cost) if addon["price"] else 0.0,
            })
        return sorted(rows, key=lambda row: (not row.get("active", True), row.get("category", ""), row["name"]))

    def create_addon(self, payload: AddonCreate) -> dict[str, Any]:
        if not self.store.get("inventory", payload.inventory_id):
            raise ValueError("O insumo deste complemento não existe no estoque.")
        addon = payload.model_dump()
        addon["id"] = self._identifier(payload.name, "addon")
        if self.store.get("addons", addon["id"]):
            addon["id"] = f"{addon['id']}-{str(uuid4())[:5]}"
        self.store.put("addons", addon["id"], addon)
        self._audit(self.store, "addon", addon["id"], "criado", f"Complemento {payload.name} cadastrado")
        return addon

    def update_addon(self, addon_id: str, payload: AddonUpdate) -> dict[str, Any]:
        addon = self.store.get("addons", addon_id)
        if not addon:
            raise ValueError("Complemento não encontrado.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        if "inventory_id" in changes and not self.store.get("inventory", changes["inventory_id"]):
            raise ValueError("O insumo deste complemento não existe no estoque.")
        before = {key: addon.get(key) for key in changes}
        addon.update(changes)
        self.store.put("addons", addon_id, addon)
        self._audit(
            self.store, "addon", addon_id, "editado",
            f"Complemento {addon['name']} atualizado",
            self._diff(before, changes),
        )
        return addon

    def delete_addon(self, addon_id: str) -> dict[str, str]:
        """Complemento ja vendido e desativado: o pedido antigo precisa continuar explicavel."""

        addon = self.store.get("addons", addon_id)
        if not addon:
            raise ValueError("Complemento não encontrado.")
        used = any(
            chosen["addon_id"] == addon_id
            for order in self.store.list("orders")
            for item in order.get("items", [])
            for chosen in item.get("addons", [])
        )
        if used:
            addon["active"] = False
            self.store.put("addons", addon_id, addon)
            self._audit(self.store, "addon", addon_id, "inativado", f"Complemento {addon['name']} desativado")
            return {"id": addon_id, "status": "inativado", "detail": "O complemento já foi vendido, então saiu do balcão mas continua no histórico."}
        self.store.delete("addons", addon_id)
        self._audit(self.store, "addon", addon_id, "excluido", f"Complemento {addon['name']} removido")
        return {"id": addon_id, "status": "excluido", "detail": "Complemento removido."}

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
        # Complemento sem insumo nao teria o que baixar do estoque, e a venda
        # passaria a registrar custo zero para a calda que a loja pagou.
        offered_as = [addon["name"] for addon in self.store.list("addons") if addon["inventory_id"] == inventory_id]
        if offered_as:
            raise ValueError(f"Este insumo é a porção do complemento: {', '.join(sorted(set(offered_as))[:4])}. Remova esses complementos primeiro.")
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

    def register_inventory_withdrawal(self, payload: InventoryWithdrawal) -> dict[str, Any]:
        """Registra uma retirada operacional de varios insumos, de uma vez.

        A conferencia e a baixa ficam na mesma transacao. Portanto, se faltar
        morango para uma das linhas, nem o acai nem os outros itens sao
        descontados parcialmente. Linhas repetidas sao somadas antes da
        validacao, para que duas retiradas de 1 kg nao escapem de um saldo de
        apenas 1,5 kg.

        Faltar saldo recusa a retirada, para barrar o 50 digitado no lugar do
        0,5. Com `allow_negative` a tela confirma a excecao e o lancamento
        passa mesmo assim: o insumo ja saiu da prateleira, e o saldo negativo e
        um sinal mais util de cadastro atrasado do que um registro que nunca
        aconteceu.
        """

        now = datetime.now(timezone.utc)
        withdrawal_id = str(uuid4())
        requested: dict[str, float] = {}
        for line in payload.items:
            requested[line.inventory_id] = round(requested.get(line.inventory_id, 0.0) + line.quantity, 3)

        responsible = payload.responsible.strip()
        reason = payload.reason.strip()
        note = self._optional_text(payload.note)

        def withdraw(transaction: StoreTransaction) -> dict[str, Any]:
            stock: dict[str, dict[str, Any]] = {}
            short: set[str] = set()
            for inventory_id, quantity in requested.items():
                item = transaction.get("inventory", inventory_id)
                if not item:
                    raise ValueError("Item de estoque não encontrado.")
                if item["quantity"] < quantity:
                    if not payload.allow_negative:
                        raise ValueError(
                            f"Saldo insuficiente de {item['name']}: há {item['quantity']:g} {item['unit']} "
                            f"e a retirada pede {quantity:g} {item['unit']}. Confirme na tela para registrar "
                            "assim mesmo e deixar o saldo negativo."
                        )
                    short.add(inventory_id)
                stock[inventory_id] = item

            lines: list[dict[str, Any]] = []
            total_cost = 0.0
            for inventory_id, quantity in requested.items():
                item = stock[inventory_id]
                previous_quantity = item["quantity"]
                item["quantity"] = round(previous_quantity - quantity, 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", inventory_id, item)

                cost = round(quantity * item.get("unit_cost", 0.0), 2)
                movement = {
                    "id": str(uuid4()),
                    "inventory_id": inventory_id,
                    "quantity_delta": -quantity,
                    "reason": reason,
                    "kind": WITHDRAWAL_KIND,
                    "withdrawal_id": withdrawal_id,
                    "responsible": responsible,
                    "note": note,
                    # Sinaliza a linha que passou por cima do saldo: sem isso o
                    # negativo apareceria no estoque sem nada explicando de onde
                    # veio.
                    "below_balance": inventory_id in short,
                    # Mantem o valor da retirada mesmo se o custo medio mudar
                    # depois da proxima compra.
                    "unit_cost": item.get("unit_cost", 0.0),
                    "total_cost": cost,
                    "created_at": now.isoformat(),
                }
                transaction.put("inventory_movements", movement["id"], movement)
                lines.append({
                    "movement_id": movement["id"],
                    "inventory_id": inventory_id,
                    "name": item["name"],
                    "unit": item["unit"],
                    "quantity": quantity,
                    "previous_quantity": previous_quantity,
                    "resulting_quantity": item["quantity"],
                    "total_cost": cost,
                    "below_balance": inventory_id in short,
                })
                total_cost += cost

            self._audit(
                transaction,
                "withdrawal",
                withdrawal_id,
                "registrada",
                f"Retirada de {len(lines)} insumo(s) por {responsible}",
                {
                    "motivo": reason,
                    "observação": note,
                    "itens": [f"{line['quantity']:g} {line['unit']} de {line['name']}" for line in lines],
                    "custo": round(total_cost, 2),
                    **({"saldo estourado": [stock[key]["name"] for key in sorted(short)]} if short else {}),
                },
            )
            return {
                "id": withdrawal_id,
                "responsible": responsible,
                "reason": reason,
                "note": note,
                "created_at": now.isoformat(),
                "total_cost": round(total_cost, 2),
                "below_balance": sorted(stock[key]["name"] for key in short),
                "items": lines,
            }

        return self.store.run_transaction(withdraw)

    def undo_inventory_withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        """Devolve ao saldo todas as linhas de uma retirada registrada."""

        now = datetime.now(timezone.utc)

        def undo(transaction: StoreTransaction) -> dict[str, Any]:
            movements = [
                movement
                for movement in transaction.list("inventory_movements")
                if movement.get("kind") == WITHDRAWAL_KIND and movement.get("withdrawal_id") == withdrawal_id
            ]
            if not movements:
                raise ValueError("Retirada não encontrada ou já desfeita.")

            stock: dict[str, dict[str, Any]] = {}
            for movement in movements:
                item = transaction.get("inventory", movement["inventory_id"])
                if not item:
                    raise ValueError("Não é possível desfazer: um dos insumos da retirada foi excluído.")
                stock[item["id"]] = item

            restored: list[str] = []
            for movement in movements:
                item = stock[movement["inventory_id"]]
                item["quantity"] = round(item["quantity"] - movement["quantity_delta"], 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", item["id"], item)
                transaction.delete("inventory_movements", movement["id"])
                restored.append(f"{abs(movement['quantity_delta']):g} {item['unit']} de {item['name']}")

            self._audit(
                transaction,
                "withdrawal",
                withdrawal_id,
                "desfeita",
                f"Retirada de {len(movements)} insumo(s) desfeita",
                {"itens devolvidos": restored},
            )
            return {"id": withdrawal_id, "status": "desfeita", "items_restored": len(movements)}

        return self.store.run_transaction(undo)

    # ------------------------------------------------------------------
    # Fabricacao: a barra vira base pronta
    # ------------------------------------------------------------------

    def production_recipes(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("production_recipes"), key=lambda linha: str(linha.get("name", "")))

    def create_production_recipe(self, payload: ProductionRecipeCreate) -> dict[str, Any]:
        recipe = payload.model_dump()
        recipe["id"] = self._identifier(payload.name, "ficha")
        if self.store.get("production_recipes", recipe["id"]):
            raise ValueError("Já existe uma ficha de fabricação com esse nome.")
        self._validar_itens_da_ficha(recipe)
        self.store.put("production_recipes", recipe["id"], recipe)
        self._audit(
            self.store, "production_recipe", recipe["id"], "criada",
            f"Ficha de fabricação {payload.name} cadastrada",
        )
        return recipe

    def update_production_recipe(self, recipe_id: str, payload: ProductionRecipeUpdate) -> dict[str, Any]:
        recipe = self.store.get("production_recipes", recipe_id)
        if not recipe:
            raise ValueError("Ficha de fabricação não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: recipe.get(key) for key in changes}
        recipe.update(changes)
        self._validar_itens_da_ficha(recipe)
        self.store.put("production_recipes", recipe_id, recipe)
        self._audit(
            self.store, "production_recipe", recipe_id, "editada",
            f"Ficha de fabricação {recipe['name']} atualizada",
            self._diff(before, changes),
        )
        return recipe

    def delete_production_recipe(self, recipe_id: str) -> dict[str, str]:
        recipe = self.store.get("production_recipes", recipe_id)
        if not recipe:
            raise ValueError("Ficha de fabricação não encontrada.")
        self.store.delete("production_recipes", recipe_id)
        self._audit(
            self.store, "production_recipe", recipe_id, "excluida",
            f"Ficha de fabricação {recipe['name']} removida",
        )
        return {"id": recipe_id, "status": "excluida", "detail": "Ficha removida. As fabricações já lançadas continuam no histórico."}

    def _validar_itens_da_ficha(self, recipe: dict[str, Any]) -> None:
        """Ficha que aponta para insumo inexistente quebraria só na hora da batida."""

        for linha in recipe.get("inputs", []):
            if not self.store.get("inventory", linha["inventory_id"]):
                raise ValueError("Um dos insumos da ficha não existe no estoque.")
        if not self.store.get("inventory", recipe["output_inventory_id"]):
            raise ValueError("O item produzido não existe no estoque.")

    def register_production(self, payload: ProductionRun) -> dict[str, Any]:
        """Registra uma batida: consome as barras e da entrada na base pronta.

        Fabricacao nao e retirada. Retirada so tira; aqui o custo nao some, ele
        muda de lugar — sai das barras e entra no quilo de base. Por isso o
        movimento de fabricacao fica fora de perda, de retirada e de quebra em
        toda conta de resultado: contar como custo aqui e de novo quando o copo
        for vendido cobraria o mesmo acai duas vezes.

        E por isso tambem que o custo da base vem do que foi usado de verdade.
        Bater com barra de 12% deixa o quilo mais caro que bater com a de 8%, e
        e essa diferenca que faz a margem do copo ser real.
        """

        now = datetime.now(timezone.utc)
        production_id = str(uuid4())
        requested: dict[str, float] = {}
        for linha in payload.inputs:
            requested[linha.inventory_id] = round(requested.get(linha.inventory_id, 0.0) + linha.quantity, 3)

        responsible = payload.responsible.strip()
        note = self._optional_text(payload.note)
        output_quantity = round(payload.output_quantity, 3)

        def produce(transaction: StoreTransaction) -> dict[str, Any]:
            produced = transaction.get("inventory", payload.output_inventory_id)
            if not produced:
                raise ValueError("O item produzido não existe no estoque.")

            stock: dict[str, dict[str, Any]] = {}
            short: set[str] = set()
            for inventory_id, quantity in requested.items():
                item = transaction.get("inventory", inventory_id)
                if not item:
                    raise ValueError("Item de estoque não encontrado.")
                if item["quantity"] < quantity:
                    if not payload.allow_negative:
                        raise ValueError(
                            f"Saldo insuficiente de {item['name']}: há {item['quantity']:g} {item['unit']} "
                            f"e a fabricação pede {quantity:g} {item['unit']}. Confirme na tela para registrar "
                            "assim mesmo e deixar o saldo negativo."
                        )
                    short.add(inventory_id)
                stock[inventory_id] = item

            consumidos: list[dict[str, Any]] = []
            custo_total = 0.0
            for inventory_id, quantity in requested.items():
                item = stock[inventory_id]
                anterior = item["quantity"]
                item["quantity"] = round(anterior - quantity, 3)
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", inventory_id, item)

                custo = round(quantity * item.get("unit_cost", 0.0), 2)
                custo_total += custo
                movimento = {
                    "id": str(uuid4()),
                    "inventory_id": inventory_id,
                    "quantity_delta": -quantity,
                    "reason": PRODUCTION_REASON,
                    "kind": PRODUCTION_KIND,
                    "production_id": production_id,
                    "recipe_id": payload.recipe_id,
                    "responsible": responsible,
                    "note": note,
                    "below_balance": inventory_id in short,
                    "unit_cost": item.get("unit_cost", 0.0),
                    "total_cost": custo,
                    "created_at": now.isoformat(),
                }
                transaction.put("inventory_movements", movimento["id"], movimento)
                consumidos.append({
                    "movement_id": movimento["id"],
                    "inventory_id": inventory_id,
                    "name": item["name"],
                    "unit": item["unit"],
                    "quantity": quantity,
                    "unit_cost": item.get("unit_cost", 0.0),
                    "total_cost": custo,
                    "below_balance": inventory_id in short,
                })

            # O custo do que saiu da batida e o custo do que entrou dividido
            # pelo rendimento real. Rendimento pior encarece o quilo, e e assim
            # que a perda de processo aparece no custo em vez de sumir.
            custo_unitario = round(custo_total / output_quantity, 4) if output_quantity else 0.0
            saldo_anterior = produced["quantity"]
            custo_anterior = produced.get("unit_cost", 0.0)
            saldo_novo = round(saldo_anterior + output_quantity, 3)
            if saldo_anterior > 0:
                custo_novo = round(
                    (saldo_anterior * custo_anterior + output_quantity * custo_unitario) / saldo_novo, 4
                )
            else:
                custo_novo = custo_unitario
            produced["quantity"] = saldo_novo
            produced["unit_cost"] = custo_novo
            produced["updated_at"] = now.isoformat()
            transaction.put("inventory", produced["id"], produced)

            entrada = {
                "id": str(uuid4()),
                "inventory_id": produced["id"],
                "quantity_delta": output_quantity,
                "reason": PRODUCTION_REASON,
                "kind": PRODUCTION_KIND,
                "production_id": production_id,
                "recipe_id": payload.recipe_id,
                "responsible": responsible,
                "note": note,
                "unit_cost": custo_unitario,
                "total_cost": round(custo_total, 2),
                # Sem isto o desfazer devolveria a quantidade e deixaria o custo
                # medio contaminado pela batida que deixou de existir.
                "previous_unit_cost": custo_anterior,
                "previous_quantity": saldo_anterior,
                "created_at": now.isoformat(),
            }
            transaction.put("inventory_movements", entrada["id"], entrada)

            self._audit(
                transaction, "production", production_id, "registrada",
                f"Fabricação de {output_quantity:g} {produced['unit']} de {produced['name']} por {responsible}",
                {
                    "consumido": [f"{linha['quantity']:g} {linha['unit']} de {linha['name']}" for linha in consumidos],
                    "custo do lote": round(custo_total, 2),
                    "custo por {}".format(produced["unit"]): custo_unitario,
                    "custo médio": {"de": custo_anterior, "para": custo_novo},
                    "observação": note,
                    **({"saldo estourado": [stock[key]["name"] for key in sorted(short)]} if short else {}),
                },
            )
            return {
                "id": production_id,
                "recipe_id": payload.recipe_id,
                "responsible": responsible,
                "note": note,
                "created_at": now.isoformat(),
                "inputs": consumidos,
                "output": {
                    "movement_id": entrada["id"],
                    "inventory_id": produced["id"],
                    "name": produced["name"],
                    "unit": produced["unit"],
                    "quantity": output_quantity,
                    "unit_cost": custo_unitario,
                    "previous_unit_cost": custo_anterior,
                    "resulting_unit_cost": custo_novo,
                    "resulting_quantity": saldo_novo,
                },
                "total_cost": round(custo_total, 2),
                "below_balance": sorted(stock[key]["name"] for key in short),
            }

        return self.store.run_transaction(produce)

    def undo_production(self, production_id: str) -> dict[str, Any]:
        """Desfaz a batida inteira: devolve as barras e tira a base produzida.

        As duas pontas voltam juntas, senao o estoque ficaria com a base sem a
        barra que a originou — ou o contrario.
        """

        now = datetime.now(timezone.utc)

        def undo(transaction: StoreTransaction) -> dict[str, Any]:
            movimentos = [
                movimento for movimento in transaction.list("inventory_movements")
                if movimento.get("kind") == PRODUCTION_KIND and movimento.get("production_id") == production_id
            ]
            if not movimentos:
                raise ValueError("Fabricação não encontrada ou já desfeita.")

            for movimento in movimentos:
                item = transaction.get("inventory", movimento["inventory_id"])
                if not item:
                    raise ValueError("Não é possível desfazer: um dos itens da fabricação foi excluído.")
                item["quantity"] = round(item["quantity"] - movimento["quantity_delta"], 3)
                # A ponta que recebeu a producao teve o custo medio recalculado;
                # devolver so a quantidade deixaria o custo de uma batida que
                # nao existe mais grudado no saldo.
                if movimento["quantity_delta"] > 0 and "previous_unit_cost" in movimento:
                    item["unit_cost"] = movimento["previous_unit_cost"]
                item["updated_at"] = now.isoformat()
                transaction.put("inventory", item["id"], item)
                transaction.delete("inventory_movements", movimento["id"])

            self._audit(
                transaction, "production", production_id, "desfeita",
                f"Fabricação com {len(movimentos)} movimento(s) desfeita",
                {"detalhe": "As barras voltaram ao saldo e a base produzida saiu."},
            )
            return {"id": production_id, "status": "desfeita", "movements_reverted": len(movimentos)}

        return self.store.run_transaction(undo)

    def register_inventory_purchase(self, inventory_id: str, payload: InventoryPurchase) -> dict[str, Any]:
        """Recebe uma compra sem transformar a entrada em simples ajuste.

        O custo médio ponderado mantém a ficha técnica com o custo do que está
        na prateleira. Se o saldo anterior é zero ou negativo, não há estoque
        valorizado confiável para ponderar; nesse caso o custo da compra passa
        a ser a nova referência até a próxima entrada.
        """

        now = datetime.now(timezone.utc)
        received_at = self._occurrence(payload.received_at, now, "compra")
        movement_id = str(uuid4())
        # O validador normaliza a embalagem para a unidade de estoque. Estas
        # guardas tambem deixam a regra clara para chamadas internas futuras.
        if payload.quantity is None or payload.unit_cost is None:
            raise ValueError("Informe a quantidade e o custo da compra.")
        received_quantity = payload.quantity
        received_unit_cost = payload.unit_cost

        def receive(transaction: StoreTransaction) -> dict[str, Any]:
            item = transaction.get("inventory", inventory_id)
            if not item:
                raise ValueError("Item de estoque não encontrado.")

            previous_quantity = item["quantity"]
            previous_unit_cost = item.get("unit_cost", 0.0)
            resulting_quantity = round(previous_quantity + received_quantity, 3)
            if previous_quantity > 0:
                resulting_unit_cost = round(
                    (previous_quantity * previous_unit_cost + received_quantity * received_unit_cost)
                    / resulting_quantity,
                    4,
                )
            else:
                resulting_unit_cost = round(received_unit_cost, 4)

            quantity_bought = payload.purchase_quantity or received_quantity
            unit_bought = self._optional_text(payload.purchase_unit) or item["unit"]
            units_per_purchase = payload.units_per_purchase or 1.0
            cost_per_purchase = payload.purchase_unit_cost or received_unit_cost

            item["quantity"] = resulting_quantity
            item["unit_cost"] = resulting_unit_cost
            item["updated_at"] = now.isoformat()
            transaction.put("inventory", inventory_id, item)

            movement = {
                "id": movement_id,
                "inventory_id": inventory_id,
                "quantity_delta": received_quantity,
                "reason": PURCHASE_REASON,
                "kind": "compra",
                # Custo por unidade de estoque, usado na média ponderada.
                "unit_cost": received_unit_cost,
                # Dados originais da compra: preservam "2 caixas de 5 kg" em
                # vez de reduzir o histórico a "10 kg" sem contexto.
                "purchase_quantity": quantity_bought,
                "purchase_unit": unit_bought,
                "units_per_purchase": units_per_purchase,
                "purchase_unit_cost": cost_per_purchase,
                "total_cost": round(quantity_bought * cost_per_purchase, 2),
                "supplier": self._optional_text(payload.supplier),
                "invoice_number": self._optional_text(payload.invoice_number),
                "note": self._optional_text(payload.note),
                # created_at é o momento em que a mercadoria chegou; o registro
                # posterior continua visível em registered_at para auditoria.
                "created_at": received_at.isoformat(),
                "registered_at": now.isoformat(),
            }
            transaction.put("inventory_movements", movement_id, movement)
            self._audit(
                transaction,
                "purchase",
                movement_id,
                "recebida",
                f"Compra de {quantity_bought:g} {unit_bought} ({received_quantity:g} {item['unit']}) de {item['name']} — {self._money(movement['total_cost'])}",
                {
                    "quantidade comprada": f"{quantity_bought:g} {unit_bought}",
                    "conteúdo por unidade": f"{units_per_purchase:g} {item['unit']}",
                    "custo da unidade comprada": cost_per_purchase,
                    "custo médio": {"de": previous_unit_cost, "para": resulting_unit_cost},
                    "fornecedor": movement["supplier"],
                    "documento": movement["invoice_number"],
                },
            )
            return {
                **movement,
                "name": item["name"],
                "unit": item["unit"],
                "previous_quantity": previous_quantity,
                "resulting_quantity": resulting_quantity,
                "previous_unit_cost": previous_unit_cost,
                "resulting_unit_cost": resulting_unit_cost,
            }

        return self.store.run_transaction(receive)

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
        if movement.get("kind") == WITHDRAWAL_KIND:
            raise ValueError("Esta baixa faz parte de uma retirada. Desfaça a retirada inteira para corrigir o saldo.")
        if movement.get("kind") == PRODUCTION_KIND:
            raise ValueError("Este lançamento faz parte de uma fabricação. Desfaça a fabricação inteira, para a barra e a base voltarem juntas.")
        if movement.get("kind") == "compra":
            raise ValueError("Esta movimentação veio de uma compra. Lance uma contagem ou ajuste para corrigir o saldo.")

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
            by_day[(today - timedelta(days=offset)).isoformat()] = {
                "sale_cost": 0.0,
                "withdrawal_cost": 0.0,
                "loss_cost": 0.0,
            }

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
                else PRODUCTION_KIND if movement.get("kind") == PRODUCTION_KIND
                else "compra" if movement.get("kind") == "compra"
                else WITHDRAWAL_KIND if movement.get("kind") == WITHDRAWAL_KIND and delta < 0
                else "entrada" if delta > 0
                else "venda" if reason == SALE_REASON
                else "perda"
            )
            quantity = abs(delta)
            # A compra guarda o preço pago no dia. Usar o custo médio atual
            # apagaria esse dado assim que o próximo fornecedor mudasse o preço.
            cost = movement.get("total_cost", round(quantity * item["unit_cost"], 2))
            day = moment.astimezone(FUSO_LOJA).date().isoformat()
            if kind in ("venda", WITHDRAWAL_KIND, "perda") and day in by_day:
                key = "sale_cost" if kind == "venda" else "withdrawal_cost" if kind == WITHDRAWAL_KIND else "loss_cost"
                by_day[day][key] += cost

            if kind != "estorno":
                row = by_item.setdefault(
                    movement["inventory_id"],
                    {
                        "inventory_id": item["id"], "name": item["name"], "unit": item["unit"],
                        "sold": 0.0, "withdrawn": 0.0, "lost": 0.0, "received": 0.0, "cost": 0.0,
                    },
                )
                bucket = (
                    "sold" if kind == "venda"
                    else "withdrawn" if kind == WITHDRAWAL_KIND
                    else "lost" if kind == "perda"
                    else "received"
                )
                row[bucket] += quantity
                if kind in ("venda", WITHDRAWAL_KIND, "perda"):
                    row["cost"] += cost
            history.append({
                **movement,
                "name": item["name"],
                "unit": item["unit"],
                "kind": kind,
                "cost": cost,
                # A tela precisa saber o que pode oferecer para editar: baixa de
                # venda so muda pelo pedido.
                "editable": reason != SALE_REASON and not reversal and kind not in ("compra", WITHDRAWAL_KIND, PRODUCTION_KIND),
            })

        items = []
        for row in by_item.values():
            item = stock[row["inventory_id"]]
            used = row["sold"] + row["withdrawn"] + row["lost"]
            daily_average = used / days
            items.append({
                **row,
                "sold": round(row["sold"], 3),
                "withdrawn": round(row["withdrawn"], 3),
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
        withdrawal_cost = round(sum(day["withdrawal_cost"] for day in by_day.values()), 2)
        loss_cost = round(sum(day["loss_cost"] for day in by_day.values()), 2)
        total_cost = round(sale_cost + withdrawal_cost + loss_cost, 2)
        history.sort(key=lambda row: row["created_at"], reverse=True)
        return {
            "period_days": days,
            "totals": {
                "sale_cost": sale_cost,
                "withdrawal_cost": withdrawal_cost,
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

    def ingredient_crossing(self, days: int = 30) -> dict[str, Any]:
        """Cruza cada venda com o estoque, separando ficha tecnica de complemento.

        A tela de Consumo responde "quanto saiu". Esta conta responde "por que
        saiu": um copo de 500 ml gasta polpa porque a receita manda, e gasta
        banana e calda porque o cliente pediu. Sao decisoes de dono diferentes
        — uma se muda mexendo na receita, a outra mexendo no preco do
        complemento — e enquanto as duas ficam somadas na mesma linha nao da
        para saber qual delas esta comendo a margem.
        """

        days = max(1, min(days, 365))
        start = self._window_start(days)
        inventory = {item["id"]: item for item in self.store.list("inventory")}
        products = {product["id"]: product for product in self.store.list("products")}
        registered_addons = {addon["id"]: addon for addon in self.store.list("addons")}

        orders = [
            order for order in self.store.list("orders")
            if is_active_order(order) and self._timestamp(order["created_at"]) >= start
        ]
        revenue = sum(order["total"] for order in orders)
        detailed = [order for order in orders if order.get("items")]
        detailed_revenue = sum(order["total"] for order in detailed)

        usage: dict[str, dict[str, float]] = {}
        product_rows: dict[str, dict[str, Any]] = {}
        addon_rows: dict[str, dict[str, Any]] = {}
        units_total = 0.0

        for order in detailed:
            # Quanto cada insumo saiu por causa de complemento neste pedido. O
            # resto da baixa e ficha tecnica, por subtracao.
            from_addons: Counter[str] = Counter()
            for line in order["items"]:
                quantity = line["quantity"]
                units_total += quantity
                row = product_rows.setdefault(line["product_id"], {
                    "product_id": line["product_id"],
                    "name": line["name"],
                    "units": 0.0, "revenue": 0.0, "cost": 0.0,
                    "addons_revenue": 0.0, "addons_cost": 0.0, "addon_units": 0.0,
                })
                row["units"] += quantity
                row["revenue"] += line["line_total"]
                row["cost"] += line.get("unit_cost", 0) * quantity
                row["addons_revenue"] += line.get("addons_price", 0) * quantity
                row["addons_cost"] += line.get("addons_cost", 0) * quantity
                if line.get("addons"):
                    row["addon_units"] += quantity
                for chosen in line.get("addons", []):
                    portions = chosen["quantity"] * quantity
                    from_addons[chosen["inventory_id"]] += chosen.get("portion", 0) * portions
                    addon = addon_rows.setdefault(chosen["addon_id"], {
                        "addon_id": chosen["addon_id"],
                        "name": chosen["name"],
                        "inventory_id": chosen["inventory_id"],
                        "portions": 0.0, "revenue": 0.0, "cost": 0.0,
                        "quantity": 0.0, "units_with": 0.0,
                    })
                    addon["portions"] += portions
                    addon["quantity"] += chosen.get("portion", 0) * portions
                    addon["revenue"] += chosen.get("unit_price", 0) * portions
                    addon["cost"] += chosen.get("unit_cost", 0) * portions
                    addon["units_with"] += quantity

            consumed: Counter[str] = Counter()
            stock_usage = order.get("stock_usage")
            if stock_usage is None:
                # Pedido gravado antes de stock_usage existir: a ficha tecnica
                # atual e a unica reconstituicao possivel.
                for line in order["items"]:
                    for recipe_line in products.get(line["product_id"], {}).get("recipe", []):
                        consumed[recipe_line["inventory_id"]] += recipe_line["quantity"] * line["quantity"]
                consumed.update(from_addons)
            else:
                for entry in stock_usage:
                    consumed[entry["inventory_id"]] += entry["quantity"]

            for inventory_id, total in consumed.items():
                bucket = usage.setdefault(inventory_id, {"recipe": 0.0, "addon": 0.0})
                # A parte de complemento nunca pode passar do que saiu de fato:
                # em pedido antigo os dois numeros vem de origens diferentes.
                by_addon = min(from_addons.get(inventory_id, 0.0), total)
                bucket["addon"] += by_addon
                bucket["recipe"] += max(0.0, total - by_addon)

        items = []
        for inventory_id, split in usage.items():
            item = inventory.get(inventory_id)
            if not item:
                continue
            quantity = split["recipe"] + split["addon"]
            unit_cost = item.get("unit_cost", 0)
            daily = quantity / days
            items.append({
                "inventory_id": inventory_id,
                "name": item["name"],
                "unit": item["unit"],
                "category": item.get("category", "Insumos"),
                "recipe_quantity": round(split["recipe"], 3),
                "addon_quantity": round(split["addon"], 3),
                "quantity": round(quantity, 3),
                "addon_share": round(split["addon"] / quantity * 100, 1) if quantity else 0.0,
                "recipe_cost": round(split["recipe"] * unit_cost, 2),
                "addon_cost": round(split["addon"] * unit_cost, 2),
                "cost": round(quantity * unit_cost, 2),
                "in_stock": item["quantity"],
                "daily_average": round(daily, 3),
                "days_left": round(item["quantity"] / daily, 1) if daily > 0 else None,
            })
        items.sort(key=lambda row: row["cost"], reverse=True)

        product_list = []
        for row in product_rows.values():
            contribution = row["revenue"] - row["cost"]
            product_list.append({
                **row,
                "units": round(row["units"], 2),
                "revenue": round(row["revenue"], 2),
                "cost": round(row["cost"], 2),
                "addons_revenue": round(row["addons_revenue"], 2),
                "addons_cost": round(row["addons_cost"], 2),
                # Quantos por cento das unidades saiu com algum complemento.
                "addon_attach": round(row["addon_units"] / row["units"] * 100, 1) if row["units"] else 0.0,
                "contribution": round(contribution, 2),
                "margin": self._margin(row["revenue"], row["cost"]),
            })
        product_list.sort(key=lambda row: row["revenue"], reverse=True)

        addon_list = []
        for row in addon_rows.values():
            item = inventory.get(row["inventory_id"])
            addon_list.append({
                **row,
                "unit": (item or {}).get("unit"),
                "inventory_name": (item or {}).get("name"),
                "portions": round(row["portions"], 2),
                "quantity": round(row["quantity"], 3),
                "revenue": round(row["revenue"], 2),
                "cost": round(row["cost"], 2),
                "contribution": round(row["revenue"] - row["cost"], 2),
                "margin": self._margin(row["revenue"], row["cost"]),
                "attach": round(row["units_with"] / units_total * 100, 1) if units_total else 0.0,
                "registered": row["addon_id"] in registered_addons,
            })
        addon_list.sort(key=lambda row: row["portions"], reverse=True)

        recipe_cost = round(sum(row["recipe_cost"] for row in items), 2)
        addon_cost = round(sum(row["addon_cost"] for row in items), 2)
        addon_revenue = round(sum(row["revenue"] for row in addon_list), 2)

        blockers: list[str] = []
        undetailed = [order for order in orders if not order.get("items")]
        if undetailed:
            blockers.append(
                f"{len(undetailed)} pedido(s), somando {self._money(sum(order['total'] for order in undetailed))}, "
                "não têm os itens vendidos, então não entram no cruzamento com o estoque."
            )
        sem_receita = sorted({
            row["name"] for row in product_list
            if not products.get(row["product_id"], {}).get("recipe")
        })
        if sem_receita:
            blockers.append(
                "Sem ficha técnica: " + ", ".join(sem_receita[:4])
                + ". Cadastre a receita para a polpa, as frutas e as embalagens desses produtos entrarem na conta."
            )
        if not registered_addons:
            blockers.append(
                "Nenhum complemento cadastrado ainda. Cadastre banana, caldas e granola em Cardápio "
                "para marcá-los na venda e baixá-los do estoque."
            )

        return {
            "period_days": days,
            "orders": len(detailed),
            "units": round(units_total, 2),
            "coverage": round(detailed_revenue / revenue * 100, 1) if revenue else 100.0,
            "items": items,
            "products": product_list,
            "addons": addon_list,
            "blockers": blockers,
            "totals": {
                "revenue": round(detailed_revenue, 2),
                "revenue_without_items": round(revenue - detailed_revenue, 2),
                "recipe_cost": recipe_cost,
                "addon_cost": addon_cost,
                "ingredient_cost": round(recipe_cost + addon_cost, 2),
                "addon_revenue": addon_revenue,
                "addon_contribution": round(addon_revenue - addon_cost, 2),
                "contribution": round(detailed_revenue - recipe_cost - addon_cost, 2),
                "cost_share": round((recipe_cost + addon_cost) / detailed_revenue * 100, 1) if detailed_revenue else 0.0,
            },
            "note": (
                "Quantidades vêm do que cada pedido realmente baixou; o valor usa o custo médio atual do insumo, "
                "que é o mesmo critério da tela de Consumo."
            ),
        }

    # ------------------------------------------------------------------
    # Despesas
    # ------------------------------------------------------------------

    def create_expense(self, payload: ExpenseCreate) -> dict[str, Any]:
        """Lanca a despesa. Marcada como fixa, abre uma serie que se repete.

        O mes informado no vencimento e o primeiro da serie: datar o aluguel em
        marco faz o sistema lancar de marco ate hoje, que e como uma loja em
        operacao ha meses coloca o historico em dia de uma vez.
        """

        now = datetime.now(timezone.utc)
        expense = payload.model_dump()
        expense.pop("repeat_until", None)
        if not payload.recurring:
            expense["id"] = str(uuid4())
            expense["created_at"] = now.isoformat()
            self.store.put("expenses", expense["id"], expense)
            self._audit(self.store, "expense", expense["id"], "criado", f"{payload.description} — {self._money(payload.amount)}")
            return expense

        first = date.fromisoformat(str(payload.due_date)[:10])
        month = first.strftime("%Y-%m")
        series = {
            "id": f"serie-{uuid4()}",
            "description": payload.description,
            "category": payload.category,
            "amount": payload.amount,
            "day": first.day,
            "starts_on": month,
            "ends_on": payload.repeat_until,
            "active": True,
            # Meses que a loja apagou de proposito. Sem essa lista a geracao
            # ressuscitaria, no dia seguinte, a conta que alguem acabou de tirar.
            "skipped": [],
            "created_at": now.isoformat(),
        }
        self.store.put("expense_series", series["id"], series)

        # O primeiro mes guarda o que o gestor digitou — data e situacao. Os
        # demais nascem pendentes: o sistema sabe que a conta venceu, nao sabe
        # se ela foi paga.
        expense["id"] = f"{series['id']}-{month}"
        expense["series_id"] = series["id"]
        expense["competence"] = month
        expense["created_at"] = now.isoformat()
        self.store.put("expenses", expense["id"], expense)
        generated = self.materialize_recurring_expenses()
        self._audit(
            self.store, "expense", expense["id"], "criado",
            f"{payload.description} — {self._money(payload.amount)} por mês, a partir de {month_name(month)}",
            {"repete até": payload.repeat_until or "sem fim previsto", "meses lançados agora": generated + 1},
        )
        return {**expense, "generated": generated}

    # ------------------------------------------------------------------
    # Despesas que se repetem
    # ------------------------------------------------------------------

    def materialize_recurring_expenses(self) -> int:
        """Lanca o mes ja vencido de cada despesa que se repete.

        Sem isso `recurring` seria so um rotulo: o aluguel existiria apenas no
        mes em que alguem digitou, e a DRE de 90 dias mostraria um aluguel onde
        deveriam estar tres. A geracao para no mes corrente de proposito — o
        passado e registro e o futuro e projecao; materializar conta que ainda
        nao venceu encheria a lista de pendencias de despesa imaginaria.

        E idempotente: o identificador do lancamento e a serie mais o mes, entao
        rodar de novo nao duplica nada.
        """

        series = self.store.list("expense_series")
        if not series:
            return 0
        limit = month_index(datetime.now(FUSO_LOJA).date().strftime("%Y-%m"))
        existing = {row["id"] for row in self.store.list("expenses")}
        now = datetime.now(timezone.utc).isoformat()
        created = 0
        for rule in series:
            if not rule.get("active", True):
                continue
            last = min(limit, month_index(rule["ends_on"])) if rule.get("ends_on") else limit
            skipped = set(rule.get("skipped", []))
            for index in range(month_index(rule["starts_on"]), last + 1):
                month = month_label(index)
                expense_id = f"{rule['id']}-{month}"
                if month in skipped or expense_id in existing:
                    continue
                self.store.put("expenses", expense_id, {
                    "id": expense_id,
                    "series_id": rule["id"],
                    "competence": month,
                    "description": rule["description"],
                    "category": rule["category"],
                    "amount": rule["amount"],
                    "due_date": self._due_date(month, rule["day"]).isoformat(),
                    "status": "Pendente",
                    "recurring": True,
                    "created_at": now,
                })
                created += 1
        return created

    def setup_fixed_costs(self, payload: FixedCostSetup) -> dict[str, Any]:
        """Cria de uma vez as despesas que se repetem todo mes.

        Nao ha de onde importar isso: o aluguel e a folha nao estao em nenhum
        relatorio, estao no contrato e no boleto. O que da para fazer e reduzir
        a dez campos o trabalho de coloca-los no sistema uma unica vez — dai em
        diante a recorrencia mantem cada mes em dia sozinha.

        Linha que ja existe como despesa ativa e pulada em vez de duplicada:
        abrir a tela de novo para acrescentar a conta que faltava nao pode criar
        um segundo aluguel.
        """

        current = datetime.now(FUSO_LOJA).date().strftime("%Y-%m")
        start = payload.starts_on or current
        if month_index(start) > month_index(current):
            raise ValueError("O mês inicial não pode estar no futuro.")

        existing = {
            self._normalized_text(rule["description"]): rule
            for rule in self.store.list("expense_series")
            if rule.get("active", True)
        }
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        for line in payload.items:
            if self._normalized_text(line.description) in existing:
                skipped.append(line.description)
                continue
            created.append(self.create_expense(ExpenseCreate(
                description=line.description,
                category=line.category,
                amount=line.amount,
                due_date=self._due_date(start, line.day).isoformat(),
                status="Pendente",
                recurring=True,
            )))

        # Mes ja vencido entra como pago quando a loja diz que pagou. Sem isso o
        # cadastro retroativo encheria a lista de pendencias com meses que a
        # loja quitou ha muito tempo, e o painel diria que ela nunca gastou
        # nada com estrutura. O mes corrente fica pendente de qualquer forma:
        # a conta pode ainda estar na gaveta.
        settled = 0
        if payload.past_paid and created:
            today = datetime.now(FUSO_LOJA).date()
            series_ids = {row["series_id"] for row in created if row.get("series_id")}
            for expense in self.store.list("expenses"):
                if expense.get("series_id") not in series_ids or expense.get("status") == "Paga":
                    continue
                if date.fromisoformat(str(expense["due_date"])[:10]) >= today:
                    continue
                expense["status"] = "Paga"
                self.store.put("expenses", expense["id"], expense)
                settled += 1

        monthly = self.recurring_monthly_cost()
        return {
            "created": len(created),
            "settled": settled,
            "skipped": skipped,
            "months_released": sum(row.get("generated", 0) + 1 for row in created),
            "monthly_total": monthly["total"],
            "starts_on": start,
            "detail": (
                f"{len(created)} despesa(s) passam a ser lançadas todo mês, somando "
                f"{self._money(monthly['total'])} de custo fixo."
                + (f" Já existiam e foram mantidas: {', '.join(skipped)}." if skipped else "")
            ),
        }

    def expense_series(self) -> list[dict[str, Any]]:
        """As regras que se repetem, com o proximo vencimento de cada uma."""

        current = month_index(datetime.now(FUSO_LOJA).date().strftime("%Y-%m"))
        released: Counter[str] = Counter(
            row["series_id"] for row in self.store.list("expenses") if row.get("series_id")
        )
        rows = []
        for rule in self.store.list("expense_series"):
            last = month_index(rule["ends_on"]) if rule.get("ends_on") else None
            active = rule.get("active", True) and (last is None or last >= current)
            next_month = None
            if active:
                candidate = max(current, month_index(rule["starts_on"]))
                skipped = set(rule.get("skipped", []))
                while last is None or candidate <= last:
                    if month_label(candidate) not in skipped:
                        break
                    candidate += 1
                else:
                    candidate = None
                next_month = self._due_date(month_label(candidate), rule["day"]).isoformat() if candidate else None
            rows.append({
                **rule,
                "active": active,
                "next_due": next_month,
                # Quantos meses ja foram lancados por esta regra. E o que
                # mostra que a repeticao esta de fato funcionando.
                "released": released[rule["id"]],
            })
        return sorted(rows, key=lambda row: (not row["active"], row["description"]))

    def update_expense_series(self, series_id: str, payload: ExpenseSeriesUpdate) -> dict[str, Any]:
        """Muda a regra dos meses que ainda serao lancados.

        O que ja foi lancado fica como esta: o aluguel de julho continua sendo o
        que foi cobrado em julho, mesmo depois do reajuste. Reescrever o passado
        por causa de uma edicao de hoje faria a DRE de tres meses atras mudar
        sozinha, e ninguem conseguiria conferir de onde veio a diferenca.
        """

        rule = self.store.get("expense_series", series_id)
        if not rule:
            raise ValueError("Despesa recorrente não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        if "repeat_until" in changes:
            changes["ends_on"] = changes.pop("repeat_until")
            if month_index(changes["ends_on"]) < month_index(rule["starts_on"]):
                raise ValueError("O mês final não pode ser anterior ao início da repetição.")
        before = {key: rule.get(key) for key in changes}
        rule.update(changes)
        rule["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("expense_series", series_id, rule)
        self._audit(
            self.store, "expense_series", series_id, "editado",
            f"Despesa fixa {rule['description']} atualizada",
            self._diff(before, changes),
        )
        return {
            **rule,
            "detail": "A mudança vale para os próximos meses. Os já lançados continuam como estão — corrija-os um a um se precisar.",
        }

    def stop_expense_series(self, series_id: str) -> dict[str, str]:
        """Para de repetir, sem apagar o que ja foi lancado."""

        rule = self.store.get("expense_series", series_id)
        if not rule:
            raise ValueError("Despesa recorrente não encontrada.")
        rule["active"] = False
        rule["stopped_at"] = datetime.now(timezone.utc).isoformat()
        self.store.put("expense_series", series_id, rule)
        self._audit(self.store, "expense_series", series_id, "encerrado", f"{rule['description']} parou de se repetir")
        return {
            "id": series_id,
            "status": "encerrado",
            "detail": "Não será mais lançada nos próximos meses. Os lançamentos já feitos continuam no histórico.",
        }

    def recurring_monthly_cost(self, month: str | None = None) -> dict[str, Any]:
        """Quanto a estrutura custa no mes informado, pelas regras que se repetem.

        E a base que faltava para projetar lucro: o custo fixo do mes que vem
        nao se descobre olhando o passado e regra de tres, ele esta escrito no
        contrato de aluguel e na folha.
        """

        reference = month or datetime.now(FUSO_LOJA).date().strftime("%Y-%m")
        index = month_index(reference)
        rows = []
        for rule in self.store.list("expense_series"):
            if not rule.get("active", True):
                continue
            if index < month_index(rule["starts_on"]):
                continue
            if rule.get("ends_on") and index > month_index(rule["ends_on"]):
                continue
            if month_label(index) in set(rule.get("skipped", [])):
                continue
            rows.append({"id": rule["id"], "description": rule["description"], "category": rule["category"], "amount": rule["amount"]})
        return {
            "month": reference,
            "total": round(sum(row["amount"] for row in rows), 2),
            "count": len(rows),
            "items": sorted(rows, key=lambda row: row["amount"], reverse=True),
        }

    @staticmethod
    def _due_date(month: str, day: int) -> date:
        """Vencimento do mes, com o dia preso ao ultimo dia quando ele nao existe.

        Aluguel do dia 31 vence no dia 28 em fevereiro. Sem essa trava a geracao
        quebraria em quatro meses do ano.
        """

        year, number = int(month[:4]), int(month[5:7])
        return date(year, number, min(day, monthrange(year, number)[1]))

    def update_expense(self, expense_id: str, payload: ExpenseUpdate) -> dict[str, Any]:
        expense = self.store.get("expenses", expense_id)
        if not expense:
            raise ValueError("Despesa não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        if changes.get("recurring") and not expense.get("series_id"):
            # Marcar como fixa uma despesa que ja existe passa a abrir a serie:
            # sem isso o gestor ligaria o interruptor e nada se repetiria.
            return self._promote_to_series(expense, changes)
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
        detail = "Despesa removida."
        series_id = expense.get("series_id")
        if series_id:
            # Apagar um mes de uma despesa que se repete precisa deixar marca na
            # regra: sem isso a proxima geracao traria de volta, no dia seguinte,
            # exatamente a conta que a loja acabou de tirar.
            rule = self.store.get("expense_series", series_id)
            if rule:
                skipped = set(rule.get("skipped", []))
                skipped.add(expense.get("competence") or str(expense["due_date"])[:7])
                rule["skipped"] = sorted(skipped)
                self.store.put("expense_series", series_id, rule)
                detail = "Mês removido. A despesa continua se repetindo nos outros meses."
        self.store.delete("expenses", expense_id)
        self._audit(self.store, "expense", expense_id, "excluido", f"{expense['description']} — {self._money(expense['amount'])} removida")
        return {"id": expense_id, "status": "excluido", "detail": detail}

    def _promote_to_series(self, expense: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        """Transforma uma despesa avulsa ja lancada em despesa que se repete."""

        expense.update(changes)
        first = date.fromisoformat(str(expense["due_date"])[:10])
        month = first.strftime("%Y-%m")
        now = datetime.now(timezone.utc)
        series = {
            "id": f"serie-{uuid4()}",
            "description": expense["description"],
            "category": expense["category"],
            "amount": expense["amount"],
            "day": first.day,
            "starts_on": month,
            "ends_on": None,
            "active": True,
            "skipped": [],
            "created_at": now.isoformat(),
        }
        self.store.put("expense_series", series["id"], series)
        # O lancamento antigo vira o primeiro mes da serie: mesmo conteudo, o
        # identificador que a geracao reconhece.
        self.store.delete("expenses", expense["id"])
        expense["id"] = f"{series['id']}-{month}"
        expense["series_id"] = series["id"]
        expense["competence"] = month
        expense["updated_at"] = now.isoformat()
        self.store.put("expenses", expense["id"], expense)
        generated = self.materialize_recurring_expenses()
        self._audit(
            self.store, "expense", expense["id"], "editado",
            f"{expense['description']} passou a se repetir todo mês",
            {"meses lançados agora": generated},
        )
        return expense

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

    # ------------------------------------------------------------------
    # Referencias de preco de mercado
    # ------------------------------------------------------------------

    def market_prices(self) -> list[dict[str, Any]]:
        return sorted(
            self.store.list("market_prices"),
            key=lambda linha: (str(linha.get("competitor", "")), str(linha.get("description", ""))),
        )

    def create_market_price(self, payload: MarketPriceCreate) -> dict[str, Any]:
        """Preco que um concorrente cobra por um produto equivalente.

        Nao existe fonte dentro da loja que diga por quanto o vizinho vende.
        Sem este cadastro o preco so pode ser julgado por custo e margem-alvo,
        que responde "quanto preciso cobrar" e nunca "quanto da para cobrar".
        """

        reference = payload.model_dump()
        if reference.get("product_id") and not self.store.get("products", reference["product_id"]):
            raise ValueError("Produto não encontrado no cardápio.")
        reference["id"] = str(uuid4())
        reference["observed_on"] = reference.get("observed_on") or datetime.now(FUSO_LOJA).date().isoformat()
        self.store.put("market_prices", reference["id"], reference)
        self._audit(
            self.store, "market_price", reference["id"], "criada",
            f"Referência de {payload.competitor} a {payload.price:.2f} cadastrada",
        )
        return reference

    def update_market_price(self, reference_id: str, payload: MarketPriceUpdate) -> dict[str, Any]:
        reference = self.store.get("market_prices", reference_id)
        if not reference:
            raise ValueError("Referência não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: reference.get(key) for key in changes}
        reference.update(changes)
        self.store.put("market_prices", reference_id, reference)
        self._audit(
            self.store, "market_price", reference_id, "editada",
            f"Referência de {reference.get('competitor')} atualizada",
            self._diff(before, changes),
        )
        return reference

    def delete_market_price(self, reference_id: str) -> dict[str, str]:
        reference = self.store.get("market_prices", reference_id)
        if not reference:
            raise ValueError("Referência não encontrada.")
        self.store.delete("market_prices", reference_id)
        self._audit(
            self.store, "market_price", reference_id, "excluida",
            f"Referência de {reference.get('competitor')} removida",
        )
        return {"id": reference_id, "status": "excluida", "detail": "Referência removida."}

    # ------------------------------------------------------------------
    # Ancoras esportivas
    # ------------------------------------------------------------------

    def sport_anchors(self) -> list[dict[str, Any]]:
        return sorted(self.store.list("sport_anchors"), key=lambda linha: str(linha.get("name", "")))

    def create_sport_anchor(self, payload: SportAnchorCreate) -> dict[str, Any]:
        """Academia, grupo de corrida ou time do bairro, com os dias de treino.

        O sistema nao tem como medir habito de treino: o relatorio de vendas nao
        traz nem hora nem cliente. A ancora e o gestor declarando a realidade que
        ele conhece, e e por isso que toda sugestao que a usa diz que a origem
        foi declarada, nunca medida.
        """

        anchor = payload.model_dump()
        anchor["id"] = self._identifier(payload.name, "sport")
        if self.store.get("sport_anchors", anchor["id"]):
            raise ValueError("Já existe uma âncora com esse nome.")
        self.store.put("sport_anchors", anchor["id"], anchor)
        self._audit(self.store, "sport_anchor", anchor["id"], "criada", f"Âncora {payload.name} cadastrada")
        return anchor

    def update_sport_anchor(self, anchor_id: str, payload: SportAnchorUpdate) -> dict[str, Any]:
        anchor = self.store.get("sport_anchors", anchor_id)
        if not anchor:
            raise ValueError("Âncora não encontrada.")
        changes = {key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None}
        before = {key: anchor.get(key) for key in changes}
        anchor.update(changes)
        self.store.put("sport_anchors", anchor_id, anchor)
        self._audit(
            self.store, "sport_anchor", anchor_id, "editada",
            f"Âncora {anchor['name']} atualizada",
            self._diff(before, changes),
        )
        return anchor

    def delete_sport_anchor(self, anchor_id: str) -> dict[str, str]:
        anchor = self.store.get("sport_anchors", anchor_id)
        if not anchor:
            raise ValueError("Âncora não encontrada.")
        self.store.delete("sport_anchors", anchor_id)
        self._audit(self.store, "sport_anchor", anchor_id, "excluida", f"Âncora {anchor['name']} removida")
        return {"id": anchor_id, "status": "excluida", "detail": "Âncora removida."}

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
        """Precifica os itens e calcula o que sai do estoque, sem gravar nada.

        A ficha tecnica do produto responde pelo copo; os complementos marcados
        na linha respondem pelo que o cliente pediu por cima. Os dois caem no
        mesmo consumo de insumo, porque a polpa e a banana saem da mesma
        prateleira — o que muda e a explicacao, guardada linha a linha.
        """

        products: dict[str, dict[str, Any]] = {}
        addons: dict[str, dict[str, Any]] = {}
        needed: Counter[str] = Counter()
        for line in lines:
            product = transaction.get("products", line.product_id)
            if not product or not product.get("active", True):
                raise ValueError("Produto indisponível para venda.")
            products[line.product_id] = product
            for recipe_line in product.get("recipe", []):
                needed[recipe_line["inventory_id"]] += recipe_line["quantity"] * line.quantity
            for chosen in getattr(line, "addons", []) or []:
                addon = addons.get(chosen.addon_id) or transaction.get("addons", chosen.addon_id)
                if not addon or not addon.get("active", True):
                    raise ValueError("Complemento indisponível para venda.")
                addons[chosen.addon_id] = addon
                needed[addon["inventory_id"]] += addon["quantity"] * chosen.quantity * line.quantity

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
            recipe_cost = self._recipe_cost(product.get("recipe", []), stock)
            chosen_rows: list[dict[str, Any]] = []
            addons_price = addons_cost = 0.0
            for chosen in getattr(line, "addons", []) or []:
                addon = addons[chosen.addon_id]
                portion_cost = stock.get(addon["inventory_id"], {}).get("unit_cost", 0) * addon["quantity"]
                addons_price += addon["price"] * chosen.quantity
                addons_cost += portion_cost * chosen.quantity
                chosen_rows.append({
                    "addon_id": addon["id"],
                    "name": addon["name"],
                    "inventory_id": addon["inventory_id"],
                    # Porcoes por unidade do produto, e quanto cada porcao tira
                    # do estoque. Guardado no pedido para o cruzamento de
                    # amanha nao depender do cadastro de hoje.
                    "quantity": chosen.quantity,
                    "portion": addon["quantity"],
                    "unit_price": addon["price"],
                    "unit_cost": round(portion_cost, 4),
                })
            unit_cost = recipe_cost + addons_cost
            line_total = (product["price"] + addons_price) * line.quantity
            total += line_total
            total_cost += unit_cost * line.quantity
            line_items.append({
                "product_id": product["id"],
                "name": product["name"],
                "quantity": line.quantity,
                "unit_price": product["price"],
                "unit_cost": round(unit_cost, 2),
                "recipe_cost": round(recipe_cost, 2),
                "addons_price": round(addons_price, 2),
                "addons_cost": round(addons_cost, 2),
                "addons": chosen_rows,
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
                # O complemento fica gravado na propria linha, entao ele volta
                # exato mesmo quando a ficha tecnica ja mudou.
                for chosen in item.get("addons", []):
                    recomposed[chosen["inventory_id"]] += chosen["portion"] * chosen["quantity"] * item["quantity"]
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

    def _occurrence(self, requested: datetime | None, now: datetime, label: str = "pedido") -> datetime:
        if requested is None:
            return now
        moment = requested if requested.tzinfo else requested.replace(tzinfo=FUSO_LOJA)
        moment = moment.astimezone(timezone.utc)
        if moment > now:
            artigo = "do" if label == "pedido" else "da"
            raise ValueError(f"A data {artigo} {label} não pode estar no futuro.")
        return moment

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        """Não grava espaço em branco como se fosse dado de rastreabilidade."""

        return value.strip() or None if value else None

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()

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
            "itens": ", ".join(
                f"{item['quantity']}x {item['name']}"
                + (f" (+{', '.join(chosen['name'] for chosen in item.get('addons', []))})" if item.get("addons") else "")
                for item in order.get("items", [])
            ) or None,
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
