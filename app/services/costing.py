"""Dossie de custo, simulacao de produto novo e resultado por linha de marca.

A tela de Cardapio ja mostrava custo e margem, mas com uma conta que so
considera a ficha tecnica. Isso responde "quanto custa fazer" e nao responde
nenhuma das perguntas que decidem preco:

- quanto do custo e embalagem, que muda quando o fornecedor muda;
- quanto sobra depois da taxa do canal, que e diferente em cada canal;
- quanto o produto precisa pagar de aluguel e energia antes de dar lucro;
- a que preco esse produto atinge a margem que a loja quer.

Tudo aqui e derivado — nada e gravado. O dossie pode ser aberto, conferido e
discordado sem alterar nenhum cadastro.
"""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from statistics import fmean
from typing import Any

from app.repositories.store import Store
from app.schemas import ProductSimulation
from app.services.erp import FUSO_LOJA, ERPService, is_active_order, month_index, month_label, month_name

# Categorias de insumo que sao embalagem, nao ingrediente. A separacao importa
# porque embalagem escala com o numero de copos e ingrediente escala com o
# tamanho do copo: sao alavancas de custo diferentes.
PACKAGING_HINTS = ("embalagem", "descartavel", "copo", "tampa", "colher", "sacola")

# Despesa que se paga todo mes independente de vender. So essas entram no
# rateio: rateio de insumo seria contar o custo duas vezes.
FIXED_CATEGORIES = ("fixo", "fixa", "equipe", "aluguel", "estrutura")

# Quantos meses fechados entram na media do custo fixo. Um ano acomoda a conta
# de luz de verao e a de inverno sem deixar um aluguel de dois anos atras
# puxando a media de hoje.
FIXED_COST_MONTHS = 12


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()


def is_packaging(item: dict[str, Any]) -> bool:
    haystack = f"{_fold(item.get('category', ''))} {_fold(item.get('name', ''))}"
    return any(hint in haystack for hint in PACKAGING_HINTS)


def is_fixed_expense(expense: dict[str, Any]) -> bool:
    if expense.get("recurring"):
        return True
    return any(hint in _fold(expense.get("category", "")) for hint in FIXED_CATEGORIES)


class CostingService:
    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------

    def dossier(self, product_id: str, days: int = 30, target_margin: float = 0.0) -> dict[str, Any]:
        product = self.store.get("products", product_id)
        if not product:
            raise ValueError("Produto não encontrado.")

        inventory = {item["id"]: item for item in self.store.list("inventory")}
        lines, alerts = self._recipe_breakdown(product.get("recipe", []), inventory)
        ingredients = round(sum(row["cost"] for row in lines if not row["packaging"]), 4)
        packaging = round(sum(row["cost"] for row in lines if row["packaging"]), 4)
        direct = round(ingredients + packaging, 4)
        price = float(product["price"])

        sales = self._product_sales(product_id, days)
        fixed = self._fixed_allocation(days, sales["units"])
        full_cost = round(direct + (fixed["per_unit"] or 0), 4)

        channels = self._channel_economics(price, direct)
        target = target_margin or 0.0
        suggested = self._price_for_margin(direct, target) if target else None

        return {
            "product": {
                "id": product["id"],
                "name": product["name"],
                "category": product.get("category"),
                "line": product.get("line", "Classico"),
                "price": round(price, 2),
                "active": product.get("active", True),
                "claims": product.get("claims", []),
                "nutrition": product.get("nutrition", {}),
            },
            "recipe": lines,
            "cost": {
                "ingredients": round(ingredients, 2),
                "packaging": round(packaging, 2),
                "direct": round(direct, 2),
                "packaging_share": round(packaging / direct * 100, 1) if direct else 0.0,
                "fixed_per_unit": fixed["per_unit"],
                "full": round(full_cost, 2) if fixed["per_unit"] is not None else None,
            },
            "fixed": fixed,
            "price_analysis": {
                "price": round(price, 2),
                "contribution": round(price - direct, 2),
                "gross_margin": self._margin(price, direct),
                "markup": round(price / direct, 2) if direct else None,
                # Margem depois do rateio: e o numero que diz se o produto paga
                # a propria parte do aluguel. Sem venda no periodo nao ha base
                # para ratear, e ai fica nulo em vez de virar um numero inventado.
                "net_margin": self._margin(price, full_cost) if fixed["per_unit"] is not None else None,
                "profit_per_unit": round(price - full_cost, 2) if fixed["per_unit"] is not None else None,
            },
            "channels": channels,
            "target": {
                "margin": target,
                "price": suggested,
                "difference": round(suggested - price, 2) if suggested is not None else None,
            } if target else None,
            "price_ladder": [
                {"margin": margin, "price": self._price_for_margin(direct, margin)}
                for margin in (40, 50, 60, 70)
            ],
            "sales": sales,
            "breakeven": self._breakeven(price, direct, fixed["monthly"]),
            "alerts": alerts + self._dossier_alerts(product, direct, price, channels, fixed),
            "period_days": days,
        }

    def simulate(self, payload: ProductSimulation) -> dict[str, Any]:
        """Testa um produto novo antes de ele existir no cardapio.

        A expansao de cardapio costuma ser decidida por intuicao porque o
        sistema so aceita o produto depois de cadastrado. Aqui a conta vem
        primeiro: se o preco imaginado nao paga o insumo mais a taxa do canal,
        da para descobrir antes de imprimir o cardapio.
        """

        inventory = {item["id"]: item for item in self.store.list("inventory")}
        lines, alerts = self._recipe_breakdown([line.model_dump() for line in payload.recipe], inventory)
        recipe_cost = round(sum(row["cost"] for row in lines), 4)
        direct = round(recipe_cost + payload.extra_cost, 4)
        if payload.extra_cost:
            lines.append({
                "inventory_id": None,
                "name": payload.extra_cost_label or "Custo estimado adicional",
                "unit": "un",
                "quantity": 1,
                "unit_cost": round(payload.extra_cost, 4),
                "cost": round(payload.extra_cost, 4),
                "share": 0.0,
                "packaging": False,
                "estimated": True,
            })
        for row in lines:
            row["share"] = round(row["cost"] / direct * 100, 1) if direct else 0.0

        price = payload.price
        contribution = round(price - direct, 2)
        fixed = self._fixed_allocation(30, None)
        monthly_units = payload.expected_daily_units * 30
        channels = self._channel_economics(price, direct)

        verdict, reading = self._verdict(contribution, price, direct, channels)
        return {
            "name": payload.name,
            "price": round(price, 2),
            "recipe": lines,
            "cost": {
                "direct": round(direct, 2),
                "recipe": round(recipe_cost, 2),
                "estimated_extra": round(payload.extra_cost, 2),
            },
            "contribution": contribution,
            "gross_margin": self._margin(price, direct),
            "target": {
                "margin": payload.target_margin,
                "price": self._price_for_margin(direct, payload.target_margin) if payload.target_margin else None,
            },
            "price_ladder": [
                {"margin": margin, "price": self._price_for_margin(direct, margin)}
                for margin in (40, 50, 60, 70)
            ],
            "channels": channels,
            "projection": {
                "daily_units": payload.expected_daily_units,
                "monthly_units": round(monthly_units, 1),
                "monthly_revenue": round(price * monthly_units, 2),
                "monthly_contribution": round(contribution * monthly_units, 2),
                # Quantas unidades por mes esse produto sozinho precisaria para
                # cobrir toda a estrutura. Serve de escala, nao de meta: nenhum
                # produto paga o aluguel sozinho.
                "units_to_cover_fixed": (
                    round(fixed["monthly"] / contribution, 1) if contribution > 0 and fixed["monthly"] else None
                ),
            },
            "fixed": fixed,
            "verdict": verdict,
            "reading": reading,
            "alerts": alerts,
        }

    def line_performance(self, days: int = 30) -> dict[str, Any]:
        """Resultado por linha de marca.

        Uma linha nova — Sport, por exemplo — quase sempre comeca pequena. Se o
        numero dela so aparecer somado ao cardapio inteiro, ela e julgada pela
        media da loja e morre antes de mostrar o que faz. Aqui cada linha tem
        faturamento, margem e ticket proprios.
        """

        days = max(1, min(days, 365))
        start = self.erp._window_start(days)
        products = {item["id"]: item for item in self.store.list("products")}
        inventory = {item["id"]: item for item in self.store.list("inventory")}

        rows: dict[str, dict[str, Any]] = {}
        for product in products.values():
            line = product.get("line") or "Classico"
            rows.setdefault(line, {
                "name": line, "revenue": 0.0, "cost": 0.0, "units": 0.0,
                "orders": set(), "products": 0, "active_products": 0,
            })
            rows[line]["products"] += 1
            if product.get("active", True):
                rows[line]["active_products"] += 1

        for order in self.store.list("orders"):
            if not is_active_order(order) or self.erp._timestamp(order["created_at"]) < start:
                continue
            for item in order.get("items", []):
                product = products.get(item["product_id"])
                line = (product or {}).get("line") or "Classico"
                row = rows.setdefault(line, {
                    "name": line, "revenue": 0.0, "cost": 0.0, "units": 0.0,
                    "orders": set(), "products": 0, "active_products": 0,
                })
                row["revenue"] += item["line_total"]
                row["units"] += item["quantity"]
                row["cost"] += item.get("unit_cost", 0) * item["quantity"]
                row["orders"].add(order["id"])

        result = []
        for row in rows.values():
            revenue = row["revenue"]
            cost = row["cost"]
            result.append({
                "name": row["name"],
                "revenue": round(revenue, 2),
                "cost": round(cost, 2),
                "contribution": round(revenue - cost, 2),
                "margin": self._margin(revenue, cost),
                "units": round(row["units"], 1),
                "orders": len(row["orders"]),
                "average_ticket": round(revenue / len(row["orders"]), 2) if row["orders"] else 0.0,
                "products": row["products"],
                "active_products": row["active_products"],
                "share": 0.0,
            })
        total = sum(row["revenue"] for row in result) or 1
        for row in result:
            row["share"] = round(row["revenue"] / total * 100, 1)
        result.sort(key=lambda row: row["revenue"], reverse=True)

        catalog = []
        for product in sorted(products.values(), key=lambda item: item["name"]):
            cost = sum(
                inventory.get(line["inventory_id"], {}).get("unit_cost", 0) * line["quantity"]
                for line in product.get("recipe", [])
            )
            catalog.append({
                "id": product["id"],
                "name": product["name"],
                "line": product.get("line", "Classico"),
                "price": product["price"],
                "cost": round(cost, 2),
                "margin": self._margin(product["price"], cost),
                "active": product.get("active", True),
                "claims": product.get("claims", []),
            })

        return {"period_days": days, "lines": result, "catalog": catalog}

    # ------------------------------------------------------------------

    def _recipe_breakdown(self, recipe: list[dict[str, Any]], inventory: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        lines: list[dict[str, Any]] = []
        alerts: list[dict[str, str]] = []
        total = 0.0
        for entry in recipe:
            item = inventory.get(entry["inventory_id"])
            if not item:
                alerts.append({
                    "type": "attention",
                    "title": "Insumo fora do cadastro",
                    "description": "A ficha técnica aponta para um insumo que não existe mais no estoque, então o custo dele ficou de fora da conta.",
                })
                lines.append({
                    "inventory_id": entry["inventory_id"], "name": "Insumo removido do estoque",
                    "unit": "?", "quantity": entry["quantity"], "unit_cost": None,
                    "cost": 0.0, "share": 0.0, "packaging": False, "missing": True,
                })
                continue
            cost = item["unit_cost"] * entry["quantity"]
            total += cost
            lines.append({
                "inventory_id": item["id"],
                "name": item["name"],
                "unit": item["unit"],
                "quantity": entry["quantity"],
                "unit_cost": item["unit_cost"],
                "cost": round(cost, 4),
                "share": 0.0,
                "packaging": is_packaging(item),
                "missing": False,
            })
            if not item["unit_cost"]:
                alerts.append({
                    "type": "attention",
                    "title": f"{item['name']} está com custo zero",
                    "description": "Enquanto o custo por unidade não for lançado, a margem deste produto aparece maior do que é.",
                })
        for row in lines:
            row["share"] = round(row["cost"] / total * 100, 1) if total else 0.0
        lines.sort(key=lambda row: row["cost"], reverse=True)
        return lines, alerts

    def _channel_economics(self, price: float, direct_cost: float) -> list[dict[str, Any]]:
        """O mesmo produto, canal a canal, depois da comissão."""

        result = []
        for channel in self.erp.channels():
            if not channel.get("active", True):
                continue
            fee = round(price * (channel.get("fee_percent", 0) / 100) + channel.get("fee_fixed", 0), 2)
            net_price = round(price - fee, 2)
            contribution = round(net_price - direct_cost, 2)
            result.append({
                "name": channel["name"],
                "fee_percent": channel.get("fee_percent", 0),
                "fee_fixed": channel.get("fee_fixed", 0),
                "fee": fee,
                "net_price": net_price,
                "contribution": contribution,
                "margin": self._margin(net_price, direct_cost) if net_price > 0 else None,
                # Preco que devolveria, neste canal, a mesma margem do balcao.
                "price_to_match": (
                    round((direct_cost + (price - direct_cost)) / (1 - channel.get("fee_percent", 0) / 100) + channel.get("fee_fixed", 0), 2)
                    if channel.get("fee_percent", 0) < 100 else None
                ),
                "profitable": contribution > 0,
            })
        result.sort(key=lambda row: row["contribution"], reverse=True)
        return result

    def _fixed_allocation(self, days: int, units: float | None) -> dict[str, Any]:
        """Rateia a estrutura de UM mes por unidade vendida.

        O custo fixo sai da media dos meses fechados, nunca da soma de tudo que
        ja foi lancado. Somar tudo trataria o aluguel de cada mes como se fosse
        do mesmo mes: no sexto mes de uso a loja leria "estrutura de seis
        alugueis", e o ponto de equilibrio subiria junto — um erro que piora
        exatamente na medida em que o gestor usa mais o sistema.

        O mes corrente fica de fora da media porque quase sempre ainda tem
        conta por lancar; entrar nela faria a estrutura parecer barata no comeco
        de todo mes. Ele so e usado quando nao ha nenhum mes fechado, e ai o
        resultado vem marcado como parcial.

        Sem volume de venda, `per_unit` fica nulo em vez de dividir por zero ou
        chutar um denominador: o dossie diz "falta base" e a loja sabe o que
        fazer.
        """

        by_month, records = self._fixed_by_month()
        current_index = month_index(datetime.now(FUSO_LOJA).date().strftime("%Y-%m"))
        window = [month_label(index) for index in range(current_index - FIXED_COST_MONTHS, current_index)]
        closed = [month for month in window if by_month.get(month)]

        if closed:
            monthly = round(fmean(by_month[month] for month in closed), 2)
            months, partial = len(closed), False
            counted = sum(records[month] for month in closed)
            if len(closed) == 1:
                expense_basis = f"o mês fechado de {month_name(closed[0])}"
            else:
                expense_basis = f"média dos {len(closed)} meses fechados entre {month_name(closed[0])} e {month_name(closed[-1])}"
        elif by_month.get(month_label(current_index)):
            # Primeiro mes de uso: nao ha mes fechado para medir. Usar o
            # corrente e melhor do que devolver zero, desde que fique dito que
            # ele ainda esta em formacao.
            month = month_label(current_index)
            monthly = round(by_month[month], 2)
            months, partial = 0, True
            counted = records[month]
            expense_basis = f"o mês corrente ({month_name(month)}), que ainda pode receber lançamentos"
        else:
            monthly, months, partial, counted = 0.0, 0, False, 0
            expense_basis = "nenhuma despesa fixa lançada"

        monthly_units = round((units or 0) / days * 30, 1) if units else 0.0
        per_unit = round(monthly / monthly_units, 4) if monthly and monthly_units else None
        return {
            "monthly": monthly,
            "expenses": counted,
            # Quantos meses fechados sustentam o valor. Zero significa que a
            # conta veio do mes corrente, ainda incompleto.
            "months": months,
            "partial": partial,
            "expense_basis": expense_basis,
            "monthly_units": monthly_units,
            "per_unit": per_unit,
            "basis": (
                f"{monthly_units:g} unidades/mês, projetadas a partir dos últimos {days} dias"
                if per_unit is not None
                else "sem volume de vendas no período para ratear a estrutura"
            ),
        }

    def _fixed_by_month(self) -> tuple[dict[str, float], dict[str, int]]:
        """Agrupa a despesa fixa pelo mes de vencimento, que e a competencia dela."""

        total: dict[str, float] = defaultdict(float)
        count: dict[str, int] = defaultdict(int)
        # Via ERP e nao direto no store: a leitura precisa vir depois de as
        # despesas que se repetem terem lancado o mes que venceu, senao o
        # rateio ficaria olhando meses vazios.
        for expense in self.erp.expenses():
            if not is_fixed_expense(expense):
                continue
            try:
                month = date.fromisoformat(str(expense["due_date"])[:10]).strftime("%Y-%m")
            except (KeyError, TypeError, ValueError):
                # Sem vencimento legivel nao da para dizer de que mes a conta e,
                # e chuta-la no mes corrente inflaria a estrutura deste mes.
                continue
            total[month] += float(expense.get("amount", 0))
            count[month] += 1
        return total, count

    def _product_sales(self, product_id: str, days: int) -> dict[str, Any]:
        start = self.erp._window_start(days)
        units = revenue = 0.0
        store_revenue = 0.0
        by_channel: Counter[str] = Counter()
        by_day: dict[str, float] = defaultdict(float)
        for order in self.store.list("orders"):
            if not is_active_order(order) or self.erp._timestamp(order["created_at"]) < start:
                continue
            store_revenue += order["total"]
            for item in order.get("items", []):
                if item["product_id"] != product_id:
                    continue
                units += item["quantity"]
                revenue += item["line_total"]
                by_channel[order["channel"]] += item["quantity"]
                by_day[self.erp._timestamp(order["created_at"]).date().isoformat()] += item["quantity"]
        return {
            "units": round(units, 1),
            "revenue": round(revenue, 2),
            "daily_average": round(units / days, 2),
            "share_of_revenue": round(revenue / store_revenue * 100, 1) if store_revenue else 0.0,
            "by_channel": [{"name": name, "units": quantity} for name, quantity in by_channel.most_common()],
            "days_with_sales": len(by_day),
        }

    def _breakeven(self, price: float, direct_cost: float, monthly_fixed: float) -> dict[str, Any]:
        contribution = price - direct_cost
        if contribution <= 0:
            return {
                "units_per_month": None,
                "note": "Cada unidade vendida dá prejuízo antes mesmo da estrutura: o preço não cobre o próprio insumo.",
            }
        if not monthly_fixed:
            return {"units_per_month": None, "note": "Nenhuma despesa fixa cadastrada para calcular o ponto de equilíbrio."}
        units = monthly_fixed / contribution
        return {
            "units_per_month": round(units, 1),
            "units_per_day": round(units / 30, 1),
            "note": (
                f"Vendendo só este produto, seriam {units:,.0f} unidades por mês para pagar "
                f"{self._money(monthly_fixed)} de estrutura."
            ).replace(",", "."),
        }

    def _dossier_alerts(self, product: dict[str, Any], direct: float, price: float, channels: list[dict[str, Any]], fixed: dict[str, Any]) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        if direct and price <= direct:
            alerts.append({
                "type": "attention",
                "title": "Preço abaixo do custo",
                "description": f"O insumo custa {self._money(direct)} e o preço é {self._money(price)}. Cada venda aumenta o prejuízo.",
            })
        losing = [row["name"] for row in channels if not row["profitable"]]
        if losing:
            alerts.append({
                "type": "attention",
                "title": "Canal no prejuízo",
                "description": f"Em {', '.join(losing)}, a taxa consome tudo que sobraria. Reveja o preço nesses canais ou tire o produto de lá.",
            })
        if fixed["per_unit"] is None and fixed["monthly"]:
            alerts.append({
                "type": "info",
                "title": "Sem base para ratear a estrutura",
                "description": "Não houve venda no período, então a margem mostrada é só sobre o insumo — ela ainda não paga aluguel, energia e equipe.",
            })
        if not fixed["monthly"]:
            alerts.append({
                "type": "info",
                "title": "Despesas fixas não cadastradas",
                "description": "Lance aluguel, energia e equipe no Financeiro para o dossiê mostrar a margem depois da estrutura, e não só sobre o insumo.",
            })
        elif fixed["partial"]:
            alerts.append({
                "type": "info",
                "title": "Estrutura medida num mês ainda aberto",
                "description": (
                    f"Não há mês fechado com despesa fixa, então a estrutura de {self._money(fixed['monthly'])} veio do mês corrente, "
                    "que ainda pode receber contas. O ponto de equilíbrio tende a subir quando o mês fechar."
                ),
            })
        if not channels:
            alerts.append({
                "type": "info",
                "title": "Nenhum canal ativo",
                "description": "Cadastre ao menos um canal de distribuição para ver quanto sobra depois das taxas.",
            })
        return alerts

    @staticmethod
    def _verdict(contribution: float, price: float, direct: float, channels: list[dict[str, Any]]) -> tuple[str, str]:
        if contribution <= 0:
            return "inviavel", "O preço não cobre nem o insumo. Suba o preço ou reveja a ficha técnica antes de seguir."
        margin = (contribution / price) * 100 if price else 0
        worst = min((row["contribution"] for row in channels), default=contribution)
        if worst <= 0:
            return "atencao", "Fecha no balcão, mas fica no vermelho no canal com a maior taxa. Defina um preço próprio para esse canal."
        if margin < 40:
            return "apertado", f"Margem de {margin:.0f}% sobre o insumo. Funciona como item de entrada, não como produto principal."
        if margin < 60:
            return "saudavel", f"Margem de {margin:.0f}% sobre o insumo, dentro do que a operação sustenta."
        return "forte", f"Margem de {margin:.0f}% sobre o insumo. Espaço para promoção ou para bancar um insumo melhor."

    @staticmethod
    def _price_for_margin(cost: float, margin: float) -> float | None:
        if margin >= 100:
            return None
        return round(cost / (1 - margin / 100), 2)

    @staticmethod
    def _margin(revenue: float, cost: float) -> float:
        return round(((revenue - cost) / revenue) * 100, 1) if revenue else 0.0

    @staticmethod
    def _money(value: float) -> str:
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
