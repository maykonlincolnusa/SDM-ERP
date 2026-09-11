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
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any

# Acima de quantas vezes o preco o custo da ficha deixa de ser "produto caro" e
# passa a ser "cadastro errado". Tres e folgado de proposito: produto vendido no
# custo, ou com prejuizo deliberado numa promocao, nao pode cair aqui.
SUSPEITA_DE_CUSTO = 3.0

from app.repositories.store import Store
from app.schemas import ProductSimulation
from app.services.erp import (
    FUSO_LOJA,
    PRODUCTION_KIND,
    REVERSAL_PREFIX,
    SALE_REASON,
    ERPService,
    is_active_order,
    month_index,
    month_label,
    month_name,
)

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

# Teto da quebra usada no custo. Uma taxa perto de 100% viria de um insumo que
# quase nao foi vendido no periodo — meia duzia de degustacoes de um sabor novo
# — e faria o custo tender ao infinito. Acima do teto o numero deixa de ser
# informacao de custo e vira um alerta de cadastro.
SHRINKAGE_CAP = 0.5


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

    # ------------------------------------------------------------------
    # Sanidade da ficha tecnica
    #
    # Esta trava nasceu de um numero encontrado nos dados reais da loja: a ficha
    # do copo de 330 ml somava R$ 1.828 de custo para um produto vendido a
    # R$ 7,87. A causa nao era calculo — era unidade. As quantidades foram
    # cadastradas na medida em que a cozinha pensa (16 g de amendoim, 30 ml de
    # calda, 99 g de banana) enquanto o insumo esta cadastrado em quilo e litro.
    # Mil vezes acima, em quinze linhas de quinze fichas.
    #
    # Um erro desse tamanho nao se descobre olhando: ele aparece como um custo
    # plausivelmente alto numa tela e some no meio de outros numeros. E, uma vez
    # dentro do sistema, contamina margem, preco sugerido, promocao e o custo
    # dos produtos vendidos — tudo derivado da mesma ficha.
    #
    # A regra aqui e a mesma que o resto do sistema aplica ao custo desconhecido:
    # e melhor recusar um numero do que apresentar um numero errado com cara de
    # certo. O que a auditoria faz e transformar "os relatorios estao estranhos"
    # numa lista de linhas para corrigir, com a correcao provavel ao lado.
    # ------------------------------------------------------------------

    def recipe_audit(self) -> dict[str, Any]:
        """Aponta fichas cujo custo nao pode estar certo, e por que.

        A suspeita nao e "o produto da prejuizo" — isso acontece e e uma decisao
        de preco. A suspeita e o custo ser multiplo do preco: nenhuma loja vende
        por R$ 8 o que lhe custa R$ 1.800, entao a ficha esta errada, nao o
        negocio.
        """

        insumos = {item["id"]: item for item in self.store.list("inventory")}
        suspeitos: list[dict[str, Any]] = []
        for produto in self.store.list("products"):
            if produto.get("active") is False:
                continue
            preco = produto.get("price") or 0
            linhas = []
            custo = 0.0
            for linha in produto.get("recipe") or []:
                insumo = insumos.get(linha["inventory_id"])
                if not insumo:
                    continue
                valor = (insumo.get("unit_cost") or 0) * linha["quantity"]
                custo += valor
                linhas.append({
                    "inventory_id": insumo["id"],
                    "name": insumo["name"],
                    "unit": insumo.get("unit"),
                    "quantity": linha["quantity"],
                    "unit_cost": insumo.get("unit_cost") or 0,
                    "cost": round(valor, 2),
                })
            if not linhas or preco <= 0:
                continue
            if custo <= preco * SUSPEITA_DE_CUSTO:
                continue

            # A linha que mais pesa e quase sempre a que tem o erro de unidade,
            # e ordenar por peso poe a correcao mais util em primeiro lugar.
            linhas.sort(key=lambda item: -item["cost"])
            suspeitos.append({
                "double_counted": self._duplo_estagio(linhas),
                "product_id": produto["id"],
                "name": produto["name"],
                "price": round(preco, 2),
                "recipe_cost": round(custo, 2),
                "ratio": round(custo / preco, 1),
                "lines": [{**linha, "diagnosis": self._diagnostico_da_linha(linha, preco)} for linha in linhas[:6]],
            })

        suspeitos.sort(key=lambda linha: -linha["ratio"])
        return {
            "suspects": suspeitos,
            "checked": len([p for p in self.store.list("products") if p.get("active") is not False]),
            "threshold": SUSPEITA_DE_CUSTO,
        }

    @staticmethod
    def _duplo_estagio(linhas: list[dict[str, Any]]) -> list[str] | None:
        """O mesmo insumo em dois estagios da mesma ficha.

        A barra sai do freezer e vira base pronta na batida; a base pronta e o
        que o copo consome. Ter os dois na mesma ficha tira o acai do estoque
        duas vezes — uma como barra, outra como base — e infla o custo do copo
        pelo caminho. E um erro que nenhuma conta de unidade corrige: mesmo com
        gramas e quilos certos, o acai continua contado em dobro.

        Nao e acusacao: receita com pedaco de barra por cima da base existe. Por
        isso a tela pergunta em vez de afirmar.
        """

        # "Acai", "Base de acai" e "barra 12%" sao o mesmo insumo em estagios
        # diferentes. O nome exato varia de loja para loja, entao o que vale e
        # a palavra: o que e acai e nao e barra nem polpa esta pronto.
        nomes = {str(linha.get("name") or "") for linha in linhas}
        bruto = {nome for nome in nomes if any(p in _fold(nome) for p in ("barra", "polpa"))}
        pronto = {
            nome for nome in nomes - bruto
            if "acai" in _fold(nome)
        }
        return sorted(pronto | bruto) if pronto and bruto else None

    @classmethod
    def _diagnostico_da_linha(cls, linha: dict[str, Any], preco: float) -> str | None:
        """O que esta linha tem de impossivel — e as duas explicacoes possiveis.

        A conferencia consegue provar que a linha esta errada: ela sozinha custa
        mais do que o produto inteiro e vendido. O que ela **nao** consegue e
        dizer qual dos dois numeros errou, e essa distincao importa porque as
        correcoes sao opostas.

        Ou a quantidade esta na medida da cozinha e o cadastro esta em quilo (16
        g de amendoim lancados como 16 kg), ou o custo do insumo e que esta na
        escala errada. Numa mesma ficha da loja as duas coisas aparecem: o acai
        tem custo por mililitro com unidade escrita "litros" — e ali a conta
        fecha —, enquanto o amendoim tem custo real por quilo com quantidade em
        grama. Sugerir "divida por mil" para todas mandaria trocar justamente a
        que estava certa, e a barra de acai, que a loja compra por quilo e usa
        aos gramas de verdade, viraria 20 g num copo de 500 ml.

        Entao a frase mostra a conta e devolve a decisao para quem conhece a
        receita, em vez de dar uma instrucao que pode estar invertida.
        """

        if linha["cost"] <= preco:
            return None

        unidade = str(linha.get("unit") or "").strip()
        menor = unidade.lower()
        de_massa_ou_volume = any(menor.startswith(inicio) for inicio in ("kg", "quilo", "l", "litro"))
        base = (
            f"Esta linha sozinha custa {cls._money(linha['cost'])} — mais que o produto inteiro, "
            f"vendido a {cls._money(preco)}."
        )
        if not de_massa_ou_volume:
            return base

        medida = "grama" if menor.startswith(("kg", "quilo")) else "mililitro"
        por_menor = linha["unit_cost"] / 1000
        return (
            f"{base} Ou a quantidade está em {medida} e o cadastro em {unidade} "
            f"({linha['quantity']:g} viraria {linha['quantity'] / 1000:g} {unidade}, "
            f"e a linha, {cls._money(linha['unit_cost'] * linha['quantity'] / 1000)}); "
            f"ou o custo é que deveria ser por {medida} "
            f"({cls._money(linha['unit_cost'])} por {unidade} viraria {cls._money(por_menor)} por {medida}). "
            f"Quem sabe a receita decide qual dos dois."
        )

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

        # Custo com quebra: para o cliente receber a porcao da ficha, a loja
        # tirou mais do que ela do estoque. Dividir pelo aproveitamento e o que
        # transforma "quanto a receita pesa" em "quanto sai do freezer".
        shrink_rates = self._shrinkage_rates(days)
        shrunk = 0.0
        for row in lines:
            taxa = shrink_rates.get(row["inventory_id"], 0.0)
            row["shrinkage_rate"] = round(taxa * 100, 1)
            row["cost_with_shrinkage"] = round(row["cost"] / (1 - taxa), 4)
            shrunk += row["cost_with_shrinkage"]
        shrunk = round(shrunk, 4)
        shrinkage = round(shrunk - direct, 4)

        channels = self._channel_economics(price, direct)
        target = target_margin or 0.0
        # O preco-alvo e o ponto de equilibrio usam o custo com quebra: sao os
        # numeros que decidem preco, e decidir preco pelo custo da ficha e
        # decidir por um custo que a loja nunca teve.
        suggested = self._price_for_margin(shrunk, target) if target else None

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
                # O custo da ficha continua ali do lado de proposito: e ele que
                # se compara com a ficha de outro produto, e e a diferenca
                # entre os dois que mede o tamanho da quebra.
                "with_shrinkage": round(shrunk, 2),
                "shrinkage": round(shrinkage, 2),
                "shrinkage_share": round(shrinkage / direct * 100, 1) if direct else 0.0,
                "shrinkage_days": days,
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
                {"margin": margin, "price": self._price_for_margin(shrunk, margin)}
                for margin in (40, 50, 60, 70)
            ],
            "sales": sales,
            "breakeven": self._breakeven(price, shrunk, fixed["monthly"]),
            "alerts": alerts + self._dossier_alerts(product, direct, price, channels, fixed)
            + self._shrinkage_alerts(lines, shrinkage, direct, days),
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

    def _shrinkage_alerts(self, lines: list[dict[str, Any]], shrinkage: float, direct: float, days: int) -> list[dict[str, str]]:
        """Avisa quando a quebra ja mudou o custo o bastante para mudar o preco."""

        if not direct or shrinkage / direct < 0.05:
            return []
        piores = sorted(
            (row for row in lines if row.get("shrinkage_rate")),
            key=lambda row: row["cost_with_shrinkage"] - row["cost"],
            reverse=True,
        )[:2]
        nomes = ", ".join(f"{row['name']} ({self._money(row['cost_with_shrinkage'] - row['cost'])} por unidade)" for row in piores)
        return [{
            "type": "attention",
            "title": f"A quebra soma {self._money(shrinkage)} ao custo deste produto",
            "description": (
                f"Nos últimos {days} dias, parte do insumo saiu do estoque sem virar venda: {nomes}. "
                "O preço-alvo e o ponto de equilíbrio já usam o custo com quebra; o custo da ficha continua "
                "ao lado para comparar."
            ),
        }]

    def _shrinkage_rates(self, days: int) -> dict[str, float]:
        """Quanto de cada insumo sai do estoque sem virar venda, em proporcao.

        A ficha tecnica diz que o copo leva 400 g de polpa. Ela nao diz que
        para entregar esses 400 g a loja tirou 430 do freezer, porque parte
        virou perda e parte saiu na producao da manha. Essa diferenca e a razao
        de o custo do dossie ser sempre menor que o custo do extrato, e e ela
        que faz o preco-alvo sair baixo.

        A taxa e por insumo, e nao uma media da loja: quem perde e a fruta,
        nao o copo descartavel, e ratear a quebra da fruta sobre a embalagem
        encareceria o produto errado.
        """

        inicio = datetime.now(FUSO_LOJA).date() - timedelta(days=days - 1)
        saida: dict[str, dict[str, float]] = defaultdict(lambda: {"total": 0.0, "quebra": 0.0})
        for movimentacao in self.store.list("inventory_movements"):
            if movimentacao["quantity_delta"] >= 0:
                continue
            motivo = movimentacao.get("reason", "")
            if motivo.startswith(REVERSAL_PREFIX):
                continue
            # A barra que virou base nao se perdeu: ela mudou de item. Contar
            # como quebra faria a barra parecer 100% desperdicada e inflaria o
            # custo de todo produto que usa a base.
            if movimentacao.get("kind") == PRODUCTION_KIND:
                continue
            if self.erp._local_day(movimentacao["created_at"]) < inicio:
                continue
            quantidade = abs(movimentacao["quantity_delta"])
            linha = saida[movimentacao["inventory_id"]]
            linha["total"] += quantidade
            # Retirada e perda contam juntas aqui, embora sejam separadas no
            # resultado: as duas sao insumo que saiu e nao apareceu no copo que
            # o cliente pagou, que e o que o custo precisa cobrir.
            if motivo != SALE_REASON:
                linha["quebra"] += quantidade
        return {
            item_id: min(linha["quebra"] / linha["total"], SHRINKAGE_CAP)
            for item_id, linha in saida.items()
            if linha["total"] > 0 and linha["quebra"] > 0
        }

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
