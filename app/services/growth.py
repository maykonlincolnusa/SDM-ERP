"""Sugestoes de crescimento tiradas do proprio movimento da loja.

Conselho de marketing generico a loja ja recebe de graca em qualquer lugar.
O que ninguem entrega e a versao ancorada no que esta loja vendeu: qual par de
produtos ja sai junto, qual dia da semana esta vazio, quanto o marketplace
levou no mes, qual insumo esta parado no freezer.

Tres regras valem para tudo aqui:

1. Nenhuma sugestao aparece sem a evidencia numerica que a gerou. O gestor
   precisa poder discordar da conta, e para isso ele precisa ver a conta.
2. Impacto estimado sempre vem com a premissa escrita ao lado. "Pode faturar
   mais" sem premissa e chute com cara de projecao.
3. Amostra pequena rebaixa a confianca em vez de sumir com a sugestao. Uma
   loja com duas semanas de historico ainda merece direcao — desde que saiba
   que e direcao, nao certeza.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from statistics import median
from typing import Any

from app.repositories.store import Store
from app.services.costing import CostingService, is_packaging
from app.services.erp import ERPService, is_active_order

DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

# Canais que cobram comissao para operar. Um deles com taxa zero no cadastro
# nao e vantagem: e margem contada a mais em todo pedido que passa por ele.
MARKETPLACES = ("ifood", "99food", "99", "rappi", "uber eats", "ubereats")

# Abaixo disso o par de produtos pode ter saido junto por acaso.
# Acima de quantas vezes o preco o custo da ficha deixa de ser "produto caro" e
# passa a ser cadastro errado. Mesmo limite que a conferencia do cardapio usa.
SUSPEITA_DE_CUSTO = 3.0

# Quanto um tamanho pode estar perto de um degrau da escada e ainda ser "o
# mesmo degrau". Quinze por cento cobre 330 contra 300 e 770 contra 700.
PROXIMIDADE_DE_TAMANHO = 0.15

MINIMO_PEDIDOS_PARA_CESTA = 20
MINIMO_OCORRENCIAS_DO_PAR = 3

# Fracao dos pedidos avulsos que se assume converter para o combo. Numero
# deliberadamente conservador: serve para dar ordem de grandeza, e aparece
# escrito na tela como premissa, nao como previsao.
CONVERSAO_ASSUMIDA = 0.25

# Desconto que torna o combo atraente sem comer a margem inteira.
DESCONTO_COMBO = 0.08

# Como o canal de venda presencial costuma ser nomeado. Ele fatura sem comissao
# como o link proprio, mas nao serve para publicar promocao, entao fica fora da
# comparacao entre canal proprio e marketplace.
BALCAO_HINTS = ("balcao", "loja", "pdv", "presencial", "retirada")


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class GrowthService:
    """Le vendas, cardapio, estoque e canais e devolve o que dá para fazer."""

    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)
        self.costing = CostingService(store)

    def opportunities(self, days: int = 90) -> dict[str, Any]:
        days = max(7, min(days, 365))
        start = self.erp._window_start(days)

        orders = [
            order for order in self.store.list("orders")
            if is_active_order(order) and self.erp._timestamp(order["created_at"]) >= start
        ]
        products = {item["id"]: item for item in self.store.list("products")}
        inventory = {item["id"]: item for item in self.store.list("inventory")}
        detailed = [order for order in orders if order.get("items")]

        contexto = self._context(orders, detailed, days)

        suggestions: list[dict[str, Any]] = []
        suggestions += self._combo_rules(detailed, products, inventory, contexto)
        suggestions += self._product_rules(detailed, products, inventory, contexto)
        suggestions += self._pricing_rules(detailed, products, inventory, contexto)
        suggestions += self._channel_rules(orders, contexto)
        suggestions += self._marketing_rules(orders, detailed, products, contexto)
        suggestions += self._service_rules(orders, contexto)

        # Ordena por impacto mensal; sem impacto calculavel, vai depois — mas
        # nao some, porque parte do que importa nao tem numero (base de
        # clientes, por exemplo) e ainda assim precisa ser feito.
        suggestions.sort(key=lambda row: (row["impact_monthly"] is None, -(row["impact_monthly"] or 0)))
        for index, row in enumerate(suggestions, start=1):
            row["rank"] = index

        return {
            "period_days": days,
            "context": contexto,
            "suggestions": suggestions,
            "totals": {
                "count": len(suggestions),
                "quantified": sum(1 for row in suggestions if row["impact_monthly"] is not None),
                "impact_monthly": round(sum(row["impact_monthly"] or 0 for row in suggestions), 2),
            },
            "by_type": [
                {"type": tipo, "count": total}
                for tipo, total in Counter(row["type"] for row in suggestions).most_common()
            ],
            "blockers": self._blockers(orders, detailed, contexto),
        }

    # ------------------------------------------------------------------
    # Contexto: o denominador de todas as contas abaixo
    # ------------------------------------------------------------------

    def _context(self, orders: list[dict[str, Any]], detailed: list[dict[str, Any]], days: int) -> dict[str, Any]:
        revenue = sum(order["total"] for order in orders)
        months = days / 30
        identified = sum(1 for order in orders if order.get("customer_name"))
        units = sum(item["quantity"] for order in detailed for item in order.get("items", []))
        return {
            "orders": len(orders),
            "detailed_orders": len(detailed),
            # Pedido importado da Anota Ai nao traz os itens; toda analise de
            # cesta e de produto so enxerga essa fatia, e a tela precisa dizer.
            "detail_coverage": round(len(detailed) / len(orders) * 100, 1) if orders else 0.0,
            "revenue": round(revenue, 2),
            "monthly_revenue": round(revenue / months, 2) if months else 0.0,
            "months": round(months, 2),
            "average_ticket": round(revenue / len(orders), 2) if orders else 0.0,
            "units": round(units, 1),
            "items_per_order": round(units / len(detailed), 2) if detailed else 0.0,
            "identified_customers": identified,
            "identified_share": round(identified / len(orders) * 100, 1) if orders else 0.0,
        }

    # ------------------------------------------------------------------
    # Combos
    # ------------------------------------------------------------------

    def _combo_rules(self, detailed, products, inventory, contexto) -> list[dict[str, Any]]:
        if len(detailed) < MINIMO_PEDIDOS_PARA_CESTA:
            return [self._suggestion(
                "combo-sem-base", "combo",
                "Ainda não dá para sugerir combo com segurança",
                f"São {len(detailed)} pedidos com itens detalhados no período — abaixo dos {MINIMO_PEDIDOS_PARA_CESTA} "
                "necessários para separar par que sai junto de coincidência.",
                "Continue registrando as vendas item a item. Assim que o histórico crescer, esta tela passa a apontar "
                "os pares reais do seu balcão em vez de um combo genérico.",
                confidence="baixa",
                confidence_note="amostra insuficiente para análise de cesta",
            )]

        # Contagem de pares dentro do mesmo pedido.
        pares: Counter[tuple[str, str]] = Counter()
        isolados: Counter[str] = Counter()
        aparicoes: Counter[str] = Counter()
        for order in detailed:
            ids = sorted({item["product_id"] for item in order["items"]})
            for produto in ids:
                aparicoes[produto] += 1
            if len(ids) == 1:
                isolados[ids[0]] += 1
            for i, primeiro in enumerate(ids):
                for segundo in ids[i + 1:]:
                    pares[(primeiro, segundo)] += 1

        total = len(detailed)
        sugestoes: list[dict[str, Any]] = []
        for (a, b), juntos in pares.most_common(12):
            if juntos < MINIMO_OCORRENCIAS_DO_PAR:
                continue
            produto_a, produto_b = products.get(a), products.get(b)
            if not produto_a or not produto_b:
                continue
            # Lift: quantas vezes mais o par aparece junto do que apareceria se
            # os dois fossem escolhidos de forma independente.
            esperado = (aparicoes[a] / total) * (aparicoes[b] / total) * total
            lift = juntos / esperado if esperado else 0
            if lift < 1.15:
                continue

            preco_cheio = produto_a["price"] + produto_b["price"]
            custo_a = self._custo_confiavel(produto_a, inventory)
            custo_b = self._custo_confiavel(produto_b, inventory)
            if custo_a is None or custo_b is None:
                # Combo montado sobre custo errado sugere desconto que fura o
                # piso. Sem os dois custos confiaveis, o par nao vira sugestao.
                continue
            custo = custo_a + custo_b
            preco_combo = round(preco_cheio * (1 - DESCONTO_COMBO), 2)
            sobra_combo = round(preco_combo - custo, 2)
            if sobra_combo <= 0:
                continue

            # O que já acontece hoje, sem nenhuma ação.
            juntos_por_mes = juntos / contexto["months"] if contexto["months"] else 0
            # O que a conversão assumida acrescentaria, vinda dos pedidos que
            # hoje levam só um dos dois.
            avulsos = isolados[a] + isolados[b]
            conversao_mes = (avulsos / contexto["months"]) * CONVERSAO_ASSUMIDA if contexto["months"] else 0
            sobra_avulsa_media = round(
                (produto_a["price"] - custo_a + produto_b["price"] - custo_b) / 2, 2)
            ganho = round(conversao_mes * (sobra_combo - sobra_avulsa_media), 2)
            perda_desconto = round(juntos_por_mes * (preco_cheio - preco_combo), 2)
            impacto = round(ganho - perda_desconto, 2)

            sugestoes.append(self._suggestion(
                f"combo-{a}-{b}", "combo",
                f"Combo {produto_a['name']} + {produto_b['name']}",
                f"Os dois já saíram no mesmo pedido {juntos} vezes ({round(juntos / total * 100, 1)}% dos pedidos "
                f"detalhados) — {round(lift, 1)}× mais do que sairiam por acaso.",
                f"Monte o combo a {_money(preco_combo)} ({round(DESCONTO_COMBO * 100)}% abaixo dos "
                f"{_money(preco_cheio)} separados). Sobra {_money(sobra_combo)} por combo vendido.",
                impact_monthly=impacto if impacto > 0 else None,
                impact_note=(
                    f"Premissa: {round(CONVERSAO_ASSUMIDA * 100)}% dos {round(avulsos / contexto['months'])} pedidos/mês "
                    f"que hoje levam só um dos dois migram para o combo, descontando o desconto dado a quem já comprava "
                    f"os dois juntos."
                ),
                confidence="alta" if juntos >= 8 else "media",
                confidence_note=f"{juntos} ocorrências do par em {total} pedidos detalhados",
                metrics=[
                    {"label": "Preço separado", "value": _money(preco_cheio)},
                    {"label": "Preço do combo", "value": _money(preco_combo)},
                    {"label": "Custo", "value": _money(round(custo, 2))},
                    {"label": "Sobra por combo", "value": _money(sobra_combo)},
                ],
            ))
            if len(sugestoes) >= 3:
                break

        # Pedido de item unico dominante: a oportunidade é o anexo, não o par.
        avulsos_total = sum(isolados.values())
        if total and avulsos_total / total >= 0.6:
            barato = self._cheapest_high_margin(products, inventory)
            if barato:
                nome, preco, sobra = barato
                conversao = (avulsos_total / contexto["months"]) * 0.15 if contexto["months"] else 0
                sugestoes.append(self._suggestion(
                    "combo-anexo", "combo",
                    f"Oferta de anexo: {nome} por {_money(preco)}",
                    f"{round(avulsos_total / total * 100)}% dos pedidos levam um item só. "
                    f"O ticket médio é {_money(contexto['average_ticket'])} e cada pedido carrega "
                    f"{contexto['items_per_order']} itens.",
                    f"Ofereça {nome} no fechamento de todo pedido de item único. É o item de maior margem entre os "
                    f"mais baratos do cardápio: cada um vendido deixa {_money(sobra)}.",
                    impact_monthly=round(conversao * sobra, 2) if conversao else None,
                    impact_note="Premissa: 15% dos pedidos de item único aceitam o anexo — taxa baixa, típica de oferta no caixa.",
                    confidence="media",
                    confidence_note="baseado no padrão de itens por pedido, não em teste real de oferta",
                ))
        return sugestoes

    # ------------------------------------------------------------------
    # Produtos e linhas
    # ------------------------------------------------------------------

    def _product_rules(self, detailed, products, inventory, contexto) -> list[dict[str, Any]]:
        sugestoes: list[dict[str, Any]] = []
        ativos = [p for p in products.values() if p.get("active", True)]

        # 1. Lacuna de tamanho dentro da mesma categoria.
        for categoria, faltando, referencia in self._size_gaps(ativos):
            preco, custo = self._scaled_price(referencia, faltando, inventory)
            if preco is None:
                continue
            sugestoes.append(self._suggestion(
                f"produto-tamanho-{categoria}-{faltando}", "produto",
                f"Falta o tamanho {faltando} ml em {categoria}",
                f"A categoria {categoria} tem "
                f"{', '.join(str(t) + ' ml' for t in sorted(self._sizes(ativos, categoria)))} — "
                f"mas não {faltando} ml.",
                f"Simule {faltando} ml a {_money(preco)}. Pela proporção da receita atual, o custo fica em "
                f"{_money(custo)}, mantendo a mesma margem dos tamanhos que já vendem.",
                impact_monthly=None,
                impact_note="Sem histórico do tamanho novo não há base para projetar volume. Use o simulador antes de decidir.",
                confidence="media",
                confidence_note="preço e custo derivados por proporção da receita existente",
                metrics=[
                    {"label": "Preço sugerido", "value": _money(preco)},
                    {"label": "Custo estimado", "value": _money(custo)},
                ],
            ))

        # 2. Insumo parado: capital no freezer que nenhum produto puxa.
        for item, valor in self._idle_inventory(products, inventory):
            sugestoes.append(self._suggestion(
                f"produto-insumo-{item['id']}", "produto",
                f"{item['name']} está parado no estoque",
                f"São {item['quantity']} {item['unit']} em estoque ({_money(valor)}) e nenhum produto ativo do "
                f"cardápio usa esse insumo.",
                f"Crie um produto que use {item['name']} ou tire o item do estoque. Enquanto ele fica parado, "
                f"é dinheiro imobilizado que também corre risco de vencer.",
                impact_monthly=None,
                impact_note=f"{_money(valor)} de capital imobilizado — recuperável, não recorrente.",
                confidence="alta",
                confidence_note="nenhuma ficha técnica ativa referencia este insumo",
            ))

        # 3. Linha de marca sem produto ativo.
        linhas_com_produto = {p.get("line", "Classico") for p in ativos}
        if "Sport" not in linhas_com_produto and ativos:
            proteina = [i for i in inventory.values() if any(
                hint in _fold(i["name"]) for hint in ("whey", "proteina", "castanha", "pasta de amendoim", "aveia", "granola"))]
            base = ", ".join(i["name"] for i in proteina[:3]) if proteina else None
            sugestoes.append(self._suggestion(
                "produto-linha-sport", "produto",
                "A linha Sport não tem nenhum produto",
                f"O cardápio tem {len(ativos)} produtos ativos, todos fora da linha Sport. "
                + (f"No estoque já existem insumos que sustentam a promessa: {base}." if base
                   else "Nenhum insumo de perfil proteico está cadastrado ainda."),
                "Use o simulador para testar um item Sport. Ele aceita insumo que a loja ainda não compra, "
                "então dá para saber se o produto se paga antes de fechar com o fornecedor.",
                impact_monthly=None,
                impact_note="Linha nova não tem histórico; o simulador dá o número antes do investimento.",
                confidence="media",
                confidence_note="oportunidade de posicionamento, ainda sem venda para comprovar demanda",
            ))

        # 3b. Ancora esportiva cadastrada sem nenhum produto para ela.
        #
        # Aqui a lacuna deixa de ser hipotese: o gestor declarou um publico com
        # dia e horario, e o cardapio nao tem o que oferecer a ele. E a unica
        # sugestao de produto novo deste modulo com demanda declarada do outro
        # lado, e por isso ela vem com o esboco de ficha ja precificado.
        ancoras = [a for a in self.store.list("sport_anchors") if a.get("active", True)]
        if ancoras and "Sport" not in linhas_com_produto and ativos:
            pessoas = sum(a.get("people") or 0 for a in ancoras)
            nomes = ", ".join(a["name"] for a in ancoras[:2])
            referencia = max(ativos, key=lambda p: p["price"])
            custo_referencia = self._recipe_cost(referencia, inventory)
            proteicos = [i for i in inventory.values() if any(
                hint in _fold(i["name"]) for hint in ("whey", "proteina", "castanha", "pasta de amendoim", "aveia", "granola"))]
            ponte = ", ".join(i["name"] for i in proteicos[:3])
            sugestoes.append(self._suggestion(
                "produto-sport-para-ancora", "produto",
                f"{nomes} está cadastrado e o cardápio não tem produto Sport",
                f"{len(ancoras)} âncora(s) esportiva(s) cadastrada(s)"
                + (f", somando {pessoas} pessoas declaradas" if pessoas else "")
                + f". O cardápio tem {len(ativos)} produtos ativos e nenhum na linha Sport, então não há o que "
                "oferecer a esse público nos dias em que ele treina."
                + (f" No estoque já existem {ponte} para sustentar a promessa." if ponte else ""),
                f"Monte um item proteico partindo de {referencia['name']} (custo de ficha {_money(round(custo_referencia, 2))}) "
                "e passe pelo simulador em Cardápio: ele aceita insumo que a loja ainda não compra e devolve o preço "
                "para a margem que você quer. Cadastre proteína e calorias no produto — sem esses números a oferta "
                "esportiva não tem o que comunicar, e a promoção da semana vira desconto genérico.",
                impact_monthly=round(pessoas * 0.05 * 4.3 * (referencia["price"] - custo_referencia), 2) if pessoas else None,
                impact_note=(
                    f"Premissa: 5% das {pessoas} pessoas declaradas compram uma vez por semana, com a sobra por "
                    f"unidade de {referencia['name']}. É premissa sobre público declarado, não medição."
                ) if pessoas else "Sem público declarado nas âncoras, o impacto não tem base para ser estimado.",
                confidence="media",
                confidence_note="a demanda vem do seu cadastro de âncoras; o custo vem da ficha técnica",
            ))

        # 4. Produto de margem alta e giro baixo: o problema é exposição, não preço.
        vendas = self._unit_sales(detailed)
        if vendas and len(detailed) >= MINIMO_PEDIDOS_PARA_CESTA:
            margens = [(p, self._margin(p["price"], self._recipe_cost(p, inventory))) for p in ativos]
            mediana_margem = median([m for _, m in margens]) if margens else 0
            mediana_giro = median([vendas.get(p["id"], 0) for p in ativos]) if ativos else 0
            for produto, margem in sorted(margens, key=lambda row: -row[1]):
                giro = vendas.get(produto["id"], 0)
                if margem <= mediana_margem or giro >= mediana_giro or giro == 0:
                    continue
                sobra = round(produto["price"] - self._recipe_cost(produto, inventory), 2)
                extra_mes = (mediana_giro - giro) / contexto["months"] if contexto["months"] else 0
                sugestoes.append(self._suggestion(
                    f"produto-empurrar-{produto['id']}", "marketing",
                    f"{produto['name']} tem a melhor margem e o pior giro",
                    f"Margem de {margem}% (mediana do cardápio: {round(mediana_margem, 1)}%) mas só {round(giro)} "
                    f"unidades no período, contra {round(mediana_giro)} da mediana.",
                    f"Coloque {produto['name']} em destaque no balcão e como sugestão padrão do atendente. "
                    f"Cada unidade deixa {_money(sobra)} — a maior sobra por unidade do cardápio.",
                    impact_monthly=round(extra_mes * sobra, 2) if extra_mes > 0 else None,
                    impact_note="Premissa: o produto passa a girar como a mediana do cardápio, sem mudança de preço.",
                    confidence="media",
                    confidence_note=f"{round(giro)} unidades vendidas — amostra pequena para o produto",
                ))
                break
        return sugestoes

    # ------------------------------------------------------------------
    # Preço
    # ------------------------------------------------------------------

    def _pricing_rules(self, detailed, products, inventory, contexto) -> list[dict[str, Any]]:
        sugestoes: list[dict[str, Any]] = []
        todos = [p for p in products.values() if p.get("active", True) and p.get("recipe")]
        # Margem calculada sobre ficha errada nao e margem baixa: e ficha errada.
        # O produto sai da amostra em vez de puxar a mediana para o absurdo.
        ativos = [p for p in todos if self._custo_confiavel(p, inventory) is not None]
        cegos = [p for p in todos if self._custo_confiavel(p, inventory) is None]
        if cegos:
            sugestoes.append(self._suggestion(
                "preco-ficha-impossivel", "preco",
                f"{len(cegos)} produto(s) não dá para avaliar: a ficha técnica custa mais que o preço",
                f"{', '.join(p['name'] for p in cegos[:5])} têm ficha somando mais de "
                f"{SUSPEITA_DE_CUSTO:g}× o preço de venda. Nenhuma loja vende por oito o que lhe custa mil — "
                "é erro de cadastro, quase sempre de unidade.",
                "Corrija em Cardápio, no quadro 'Fichas técnicas com custo impossível'. Enquanto não for "
                "corrigido, margem, preço sugerido, combo e promoção destes produtos ficam de fora — "
                "publicar número tirado de ficha errada é pior que não publicar.",
                confidence="alta",
                confidence_note="custo da ficha acima de três vezes o preço de venda",
            ))
        if not ativos:
            return sugestoes
        vendas = self._unit_sales(detailed)
        margens = {p["id"]: self._margin(p["price"], self._recipe_cost(p, inventory)) for p in ativos}
        alvo = median(margens.values())

        # Produto abaixo da margem mediana, com giro que justifique mexer.
        for produto in sorted(ativos, key=lambda p: margens[p["id"]]):
            margem = margens[produto["id"]]
            giro = vendas.get(produto["id"], 0)
            if margem >= alvo - 5 or giro <= 0:
                continue
            custo = self._recipe_cost(produto, inventory)
            novo_preco = round(custo / (1 - alvo / 100), 2) if alvo < 100 else None
            if not novo_preco or novo_preco <= produto["price"]:
                continue
            por_mes = giro / contexto["months"] if contexto["months"] else 0
            sugestoes.append(self._suggestion(
                f"preco-{produto['id']}", "preco",
                f"{produto['name']} está {round(alvo - margem, 1)} pontos abaixo da margem do cardápio",
                f"Margem de {margem}% contra {round(alvo, 1)}% da mediana. Custa {_money(round(custo, 2))} e "
                f"vende a {_money(produto['price'])}, com {round(giro)} unidades no período.",
                f"Levar o preço para {_money(novo_preco)} iguala a margem ao resto do cardápio — "
                f"aumento de {_money(round(novo_preco - produto['price'], 2))} por unidade.",
                impact_monthly=round(por_mes * (novo_preco - produto["price"]), 2),
                impact_note="Premissa: o volume se mantém após o reajuste. Reajuste acima de 10% costuma custar volume — teste antes.",
                confidence="alta" if giro >= 20 else "media",
                confidence_note=f"{round(giro)} unidades vendidas no período",
                metrics=[
                    {"label": "Preço hoje", "value": _money(produto["price"])},
                    {"label": "Preço sugerido", "value": _money(novo_preco)},
                    {"label": "Margem hoje", "value": f"{margem}%"},
                    {"label": "Margem alvo", "value": f"{round(alvo, 1)}%"},
                ],
            ))
            break

        # Produto que dá prejuízo em algum canal por causa da comissão.
        canais = [c for c in self.erp.channels() if c.get("active", True) and c.get("fee_percent", 0) > 0]
        for produto in ativos:
            custo = self._recipe_cost(produto, inventory)
            for canal in canais:
                liquido = produto["price"] * (1 - canal["fee_percent"] / 100) - canal.get("fee_fixed", 0)
                if liquido > custo:
                    continue
                preco_canal = round((custo / (1 - canal["fee_percent"] / 100)) * 1.6, 2)
                sugestoes.append(self._suggestion(
                    f"preco-canal-{produto['id']}-{canal['id']}", "preco",
                    f"{produto['name']} dá prejuízo no {canal['name']}",
                    f"A comissão de {canal['fee_percent']}% deixa {_money(round(liquido, 2))} para um produto que "
                    f"custa {_money(round(custo, 2))} para fazer.",
                    f"Use preço próprio de {_money(preco_canal)} no {canal['name']} ou tire o produto de lá. "
                    f"Vender no prejuízo com volume só aumenta o prejuízo.",
                    impact_monthly=None,
                    impact_note="O ganho é a perda evitada, que depende de quanto o produto vende nesse canal.",
                    confidence="alta",
                    confidence_note="conta direta entre preço, comissão e custo da ficha técnica",
                ))
                return sugestoes
        return sugestoes

    # ------------------------------------------------------------------
    # Canais
    # ------------------------------------------------------------------

    def _channel_rules(self, orders, contexto) -> list[dict[str, Any]]:
        sugestoes: list[dict[str, Any]] = []
        desempenho = self.erp.channel_performance(int(min(contexto["months"] * 30, 365)) or 30)
        meses = contexto["months"] or 1

        # Marketplace cadastrado com taxa zero. É o erro mais perigoso do
        # cadastro de canais porque ele se disfarça de boa notícia: o canal
        # aparece com a melhor margem da loja justamente por ninguém ter
        # lançado a comissão dele, e a decisão de para onde empurrar volume sai
        # invertida.
        for canal in desempenho["channels"]:
            if not canal["orders"] or canal["fee_percent"] or canal["fee_fixed"]:
                continue
            if _fold(canal["name"]) not in MARKETPLACES:
                continue
            melhor = max(desempenho["channels"], key=lambda linha: linha["contribution_margin"] or 0)
            sugestoes.append(self._suggestion(
                f"canal-taxa-zero-{_fold(canal['name'])}", "canal",
                f"{canal['name']} está cadastrado sem comissão — e isso não é verdade",
                f"{canal['name']} tem {canal['orders']} pedido(s) e {_money(canal['revenue'])} no período, "
                f"com taxa de 0% no cadastro. Por isso ele aparece com margem de "
                f"{canal['contribution_margin']}%"
                + (f", a melhor da loja" if melhor["name"] == canal["name"] else "")
                + ". Marketplace não trabalha de graça.",
                f"Abra o extrato de {canal['name']}, pegue a comissão real do seu contrato e lance em Canais. "
                "Enquanto ela for zero, a margem deste canal está inflada, o resultado da loja também, e qualquer "
                "decisão de para onde empurrar volume sai invertida.",
                impact_monthly=None,
                impact_note="Não é ganho: é erro de medição. O impacto só pode ser calculado depois que a taxa real entrar.",
                confidence="alta",
                confidence_note="taxa zero é dado do cadastro, não estimativa",
                metrics=[
                    {"label": "Faturamento no período", "value": _money(canal["revenue"])},
                    {"label": "Margem exibida hoje", "value": f"{canal['contribution_margin']}%"},
                    {"label": "Comissão lançada", "value": "0%"},
                ],
            ))

        # Comissão pesada: quantifica o que já foi pago e o que migrar economiza.
        for canal in desempenho["channels"]:
            if not canal["fee"] or canal["share"] < 15:
                continue
            por_mes = canal["fee"] / meses
            sugestoes.append(self._suggestion(
                f"canal-taxa-{_fold(canal['name'])}", "canal",
                f"{canal['name']} levou {_money(canal['fee'])} em comissão",
                f"{canal['name']} responde por {canal['share']}% do faturamento e cobra "
                f"{canal['fee_percent']}%. No período isso deu {_money(canal['fee'])} — "
                f"{_money(round(por_mes, 2))} por mês.",
                f"Migrar 30% desses pedidos para WhatsApp ou balcão devolve {_money(round(por_mes * 0.3, 2))} por mês. "
                f"Um cartão no pedido entregue, com desconto de 5% na compra direta, é a forma mais barata de fazer isso.",
                impact_monthly=round(por_mes * 0.3 - (canal["revenue"] / meses * 0.3 * 0.05), 2),
                impact_note="Premissa: 30% dos pedidos migram e recebem 5% de desconto pela compra direta. O saldo é a comissão economizada menos o desconto dado.",
                confidence="alta" if canal["orders"] >= 20 else "media",
                confidence_note=f"{canal['orders']} pedidos por este canal no período",
                metrics=[
                    {"label": "Faturamento", "value": _money(canal["revenue"])},
                    {"label": "Comissão paga", "value": _money(canal["fee"])},
                    {"label": "Ficou na loja", "value": _money(canal["net_revenue"])},
                ],
            ))
            break

        # Canal cadastrado que nunca vendeu.
        if desempenho["idle_channels"]:
            nomes = ", ".join(desempenho["idle_channels"])
            sugestoes.append(self._suggestion(
                "canal-ocioso", "canal",
                f"Canal sem nenhuma venda: {nomes}",
                f"{nomes} está cadastrado e ativo, mas não registrou pedido no período.",
                "Ative o canal de verdade ou desative o cadastro. Canal ativo sem venda distorce a comparação "
                "entre canais e esconde qual deles realmente sustenta a loja.",
                impact_monthly=None,
                impact_note="Sem histórico do canal não há base para projetar receita.",
                confidence="alta",
                confidence_note="nenhum pedido registrado no período",
            ))

        # Canal próprio contra marketplace: a comparação que decide onde investir.
        #
        # No histórico real desta loja o link próprio fatura cinco vezes o iFood
        # e não paga comissão nenhuma. Sem essa linha na tela, a tendência é
        # tratar o marketplace como "o canal digital" e o link próprio como
        # detalhe — quando é o contrário.
        proprios = [c for c in desempenho["channels"] if not c["fee_percent"] and c["revenue"] > 0]
        marketplaces = [c for c in desempenho["channels"] if c["fee_percent"] > 0 and c["revenue"] > 0]
        # O balcao nao entra na comparacao: ele fatura sem comissao, mas nao e
        # onde se publica uma promocao. A pergunta aqui e qual canal *digital*
        # proprio disputa com o marketplace — e quase sempre ele ja esta
        # ganhando sem ninguem ter reparado.
        digitais = [c for c in proprios if not any(hint in _fold(c["name"]) for hint in BALCAO_HINTS)]
        if (digitais or proprios) and marketplaces:
            melhor = max(digitais or proprios, key=lambda c: c["revenue"])
            pior = max(marketplaces, key=lambda c: c["fee"])
            if melhor["revenue"] > pior["revenue"]:
                vezes = round(melhor["revenue"] / pior["revenue"], 1) if pior["revenue"] else None
                sugestoes.append(self._suggestion(
                    "canal-proprio-vence", "canal",
                    f"{melhor['name']} fatura {vezes}x o {pior['name']} e não paga comissão",
                    f"{melhor['name']}: {_money(melhor['revenue'])} em {melhor['orders']} pedidos, sem comissão. "
                    f"{pior['name']}: {_money(pior['revenue'])} em {pior['orders']} pedidos, dos quais "
                    f"{_money(pior['fee'])} ficaram com a plataforma.",
                    f"Trate {melhor['name']} como o canal principal, não como sobra do marketplace: é nele que a "
                    f"promoção da semana deve ser publicada, porque lá o desconto vai inteiro para o cliente em vez "
                    f"de ser dividido com a plataforma. Um QR no balcão e o link na bio custam quase nada e alimentam "
                    "o canal que já está ganhando.",
                    impact_monthly=round(pior["fee"] / meses, 2) if pior["fee"] else None,
                    impact_note=(
                        f"O número é a comissão que o {pior['name']} cobrou por mês — o teto do que migrar o "
                        "pedido para o canal próprio economiza."
                    ),
                    confidence="alta" if melhor["orders"] >= 20 else "media",
                    confidence_note=f"{melhor['orders']} pedidos pelo canal próprio no período",
                    metrics=[
                        {"label": f"{melhor['name']}", "value": _money(melhor["revenue"])},
                        {"label": f"{pior['name']}", "value": _money(pior["revenue"])},
                        {"label": "Comissão paga", "value": _money(pior["fee"])},
                    ],
                ))

        # Ticket muito diferente entre canais: onde crescer rende mais por pedido.
        com_ticket = [c for c in desempenho["channels"] if c["orders"] >= 10 and c.get("average_ticket")]
        if len(com_ticket) >= 2:
            alto = max(com_ticket, key=lambda c: c["average_ticket"])
            baixo = min(com_ticket, key=lambda c: c["average_ticket"])
            diferenca = alto["average_ticket"] - baixo["average_ticket"]
            if baixo["average_ticket"] and diferenca / baixo["average_ticket"] >= 0.25 and alto["share"] < 50:
                migrados = baixo["orders"] * 0.1 / meses
                sugestoes.append(self._suggestion(
                    "canal-ticket", "canal",
                    f"O pedido de {alto['name']} vale {round(diferenca / baixo['average_ticket'] * 100)}% mais que o de {baixo['name']}",
                    f"Ticket médio de {_money(alto['average_ticket'])} em {alto['name']} contra "
                    f"{_money(baixo['average_ticket'])} em {baixo['name']}, com {alto['share']}% do faturamento "
                    f"vindo do canal de ticket maior.",
                    f"Crescer {alto['name']} rende mais por pedido atendido do que crescer {baixo['name']}. "
                    f"Ofereça {alto['name']} a quem já compra no balcão — é o mesmo cliente, gastando mais por visita.",
                    impact_monthly=round(migrados * diferenca, 2) if migrados else None,
                    impact_note=(
                        f"Premissa: 10% dos pedidos de {baixo['name']} passam a sair por {alto['name']}, "
                        "com a diferença de ticket observada. Não inclui o custo de entrega."
                    ),
                    confidence="media",
                    confidence_note=f"{alto['orders']} e {baixo['orders']} pedidos nos dois canais",
                ))

        # Concentração: um canal só sustentando a loja é risco, não conquista.
        if desempenho["channels"]:
            lider = desempenho["channels"][0]
            if lider["share"] >= 70 and len(desempenho["channels"]) > 1:
                sugestoes.append(self._suggestion(
                    "canal-concentracao", "canal",
                    f"{lider['share']}% do faturamento depende de um canal só",
                    f"{lider['name']} concentra {lider['share']}% das vendas. Uma mudança de taxa, de algoritmo "
                    f"ou de política desse canal atinge quase todo o faturamento de uma vez.",
                    "Defina uma meta de participação para o segundo canal — 25% costuma ser o suficiente para "
                    "a loja não ficar refém — e trate isso como projeto, não como consequência.",
                    impact_monthly=None,
                    impact_note="É redução de risco, não aumento de receita. O impacto aparece no mês em que o canal principal muda as regras.",
                    confidence="alta",
                    confidence_note=f"{lider['orders']} de {sum(c['orders'] for c in desempenho['channels'])} pedidos",
                ))
        return sugestoes

    # ------------------------------------------------------------------
    # Marketing
    # ------------------------------------------------------------------

    def _marketing_rules(self, orders, detailed, products, contexto) -> list[dict[str, Any]]:
        sugestoes: list[dict[str, Any]] = []

        # Dia fraco da semana: a promoção mais barata é a que preenche ocioso.
        por_dia_semana: dict[int, list[float]] = defaultdict(list)
        por_data: dict[Any, float] = defaultdict(float)
        for order in orders:
            dia = self.erp._local_day(order["created_at"])
            por_data[dia] += order["total"]
        for dia, valor in por_data.items():
            por_dia_semana[dia.weekday()].append(valor)

        if len(por_data) >= 14:
            medias = {
                dia: sum(valores) / len(valores)
                for dia, valores in por_dia_semana.items()
                if len(valores) >= 2
            }
            if len(medias) >= 5:
                mediana_dia = median(medias.values())
                pior = min(medias, key=lambda dia: medias[dia])
                if medias[pior] < mediana_dia * 0.75:
                    gap = mediana_dia - medias[pior]
                    sugestoes.append(self._suggestion(
                        "marketing-dia-fraco", "marketing",
                        f"{DIAS[pior].capitalize()} fatura {round((1 - medias[pior] / mediana_dia) * 100)}% abaixo dos outros dias",
                        f"Média de {_money(round(medias[pior], 2))} na {DIAS[pior]}, contra {_money(round(mediana_dia, 2))} "
                        f"de um dia mediano — medido em {len(por_dia_semana[pior])} {DIAS[pior]}s.",
                        f"Crie uma ação exclusiva de {DIAS[pior]}: combo do dia, segunda unidade com desconto ou "
                        f"parceria com quem tem público nesse dia. O custo fixo da loja corre igual — "
                        f"{DIAS[pior]} vazia é aluguel pago sem contrapartida.",
                        impact_monthly=round(gap * 4.3 * 0.4, 2),
                        impact_note=f"Premissa: a ação recupera 40% da diferença para a mediana, em ~4,3 {DIAS[pior]}s por mês.",
                        confidence="alta" if len(por_dia_semana[pior]) >= 6 else "media",
                        confidence_note=f"{len(por_dia_semana[pior])} observações desse dia da semana",
                    ))

        # Base de clientes: pré-requisito de qualquer marketing de retenção.
        if contexto["orders"] >= 20 and contexto["identified_share"] < 25:
            sugestoes.append(self._suggestion(
                "marketing-base-clientes", "marketing",
                f"Só {contexto['identified_share']}% dos pedidos têm cliente identificado",
                f"De {contexto['orders']} pedidos no período, {contexto['identified_customers']} registraram o nome do cliente.",
                "Passe a preencher o nome em toda venda. Sem base identificada não existe recompra dirigida, "
                "aniversário, reativação de quem sumiu nem clube — todas as ações que custam menos que atrair "
                "cliente novo dependem de saber quem já comprou.",
                impact_monthly=None,
                impact_note="É pré-requisito, não campanha: destrava as ações de retenção, que só podem ser medidas depois.",
                confidence="alta",
                confidence_note="contagem direta sobre os pedidos do período",
            ))

        # Concentração de cardápio: dependência de um produto só.
        if detailed:
            receita_produto: Counter[str] = Counter()
            for order in detailed:
                for item in order["items"]:
                    receita_produto[item["name"]] += item["line_total"]
            total = sum(receita_produto.values())
            if total and receita_produto:
                nome, valor = receita_produto.most_common(1)[0]
                share = valor / total * 100
                if share >= 55 and len(receita_produto) > 1:
                    sugestoes.append(self._suggestion(
                        "marketing-concentracao-produto", "marketing",
                        f"{nome} sozinho é {round(share)}% do faturamento detalhado",
                        f"{nome} respondeu por {_money(round(valor, 2))} de {_money(round(total, 2))} vendidos em "
                        f"pedidos com itens detalhados.",
                        f"Trabalhe uma segunda âncora no cardápio. Depender de um item só significa que uma alta no "
                        f"insumo principal dele ou um concorrente copiando esse produto atinge o faturamento inteiro.",
                        impact_monthly=None,
                        impact_note="Redução de risco e ampliação de público; o retorno aparece no mix, não no mês seguinte.",
                        confidence="media",
                        confidence_note=f"medido sobre {contexto['detail_coverage']}% dos pedidos, que são os que têm itens",
                    ))
        return sugestoes

    # ------------------------------------------------------------------
    # Serviços
    # ------------------------------------------------------------------

    def _service_rules(self, orders, contexto) -> list[dict[str, Any]]:
        sugestoes: list[dict[str, Any]] = []

        # Perda recorrente vira serviço: escoar o que ia para o lixo.
        consumo = self.erp.consumption(30)
        perda = consumo["totals"]["loss_cost"]
        if perda > 0 and consumo["totals"]["total_cost"] and consumo["totals"]["loss_share"] >= 5:
            sugestoes.append(self._suggestion(
                "servico-ultima-hora", "servico",
                f"{consumo['totals']['loss_share']}% do insumo consumido virou perda",
                f"Nos últimos 30 dias, {_money(perda)} de insumo saíram do estoque sem virar venda.",
                "Crie uma oferta de última hora na hora do fechamento, com desconto sobre o que estraga primeiro. "
                "Vender com margem menor ainda é melhor do que descartar com margem negativa.",
                impact_monthly=round(perda * 0.5, 2),
                impact_note="Premissa: metade da perda é evitável e vira venda com desconto. A outra metade é quebra operacional, que se resolve no processo, não na oferta.",
                confidence="alta" if perda > 50 else "media",
                confidence_note="medido sobre as movimentações de estoque dos últimos 30 dias",
            ))

        # Recorrência identificada abre clube/assinatura.
        clientes: Counter[str] = Counter()
        for order in orders:
            nome = (order.get("customer_name") or "").strip()
            if nome:
                clientes[_fold(nome)] += 1
        recorrentes = [nome for nome, vezes in clientes.items() if vezes >= 3]
        if recorrentes and contexto["average_ticket"]:
            mensal = round(contexto["average_ticket"] * 4, 2)
            sugestoes.append(self._suggestion(
                "servico-clube", "servico",
                f"{len(recorrentes)} clientes já compram três vezes ou mais",
                f"Entre os {len(clientes)} clientes identificados no período, {len(recorrentes)} repetiram três vezes ou mais.",
                f"Teste um clube mensal em torno de {_money(mensal)} — quatro unidades no mês, pagas adiantado. "
                f"Adianta caixa, trava a recompra e transforma cliente frequente em receita previsível.",
                impact_monthly=round(len(recorrentes) * mensal * 0.3, 2),
                impact_note="Premissa: 30% dos clientes recorrentes aderem. É a faixa típica de adesão de clube em teste inicial.",
                confidence="media" if len(recorrentes) >= 10 else "baixa",
                confidence_note=f"{len(recorrentes)} clientes recorrentes identificados — a base real é maior, mas só parte dos pedidos tem nome",
            ))
        elif contexto["identified_share"] >= 25 and not recorrentes:
            sugestoes.append(self._suggestion(
                "servico-clube-sem-base", "servico",
                "Nenhum cliente identificado repetiu três vezes",
                f"Dos {contexto['identified_customers']} pedidos com nome, nenhum cliente aparece três vezes ou mais no período.",
                "Antes de pensar em clube ou assinatura, trabalhe a segunda compra: um cupom de retorno válido por "
                "15 dias entregue junto com o pedido é a ação mais direta para isso.",
                impact_monthly=None,
                impact_note="Recorrência ainda não existe na base; medir a segunda compra vem antes de vender a quarta.",
                confidence="media",
                confidence_note="baseado apenas nos pedidos com cliente identificado",
            ))
        return sugestoes

    # ------------------------------------------------------------------
    # O que impede sugestões melhores
    # ------------------------------------------------------------------

    def _blockers(self, orders, detailed, contexto) -> list[dict[str, str]]:
        """O que falta cadastrar para as sugestões ficarem melhores.

        Vale tanto quanto as sugestões: dizer que a análise de cesta enxerga
        30% dos pedidos evita que o gestor tome a recomendação como se ela
        cobrisse a loja inteira.
        """

        pendencias: list[dict[str, str]] = []
        if orders and contexto["detail_coverage"] < 90:
            pendencias.append({
                "title": f"{round(100 - contexto['detail_coverage'], 1)}% dos pedidos não têm itens detalhados",
                "description": "Pedidos importados da Anota Aí trazem só o faturamento. Combo, mix e concentração de "
                               "cardápio são calculados apenas sobre a parte detalhada.",
            })
        if contexto["identified_share"] < 25:
            pendencias.append({
                "title": "Base de clientes quase inexistente",
                "description": "Sem nome no pedido não dá para medir recompra, e nenhuma sugestão de retenção pode "
                               "ser quantificada.",
            })
        if not any(row.get("recurring") or _fold(row.get("category", "")) in ("fixo", "fixa")
                   for row in self.store.list("expenses")):
            pendencias.append({
                "title": "Despesas fixas não cadastradas",
                "description": "Sem aluguel, energia e equipe lançados, nenhuma sugestão consegue dizer se o ganho "
                               "estimado cobre a estrutura.",
            })
        sem_custo = [p for p in self.store.list("products") if p.get("active", True) and not p.get("recipe")]
        if sem_custo:
            pendencias.append({
                "title": f"{len(sem_custo)} produto(s) sem ficha técnica",
                "description": f"{', '.join(p['name'] for p in sem_custo[:3])} não têm receita cadastrada, então "
                               "margem e combo não podem ser calculados para eles.",
            })
        return pendencias

    # ------------------------------------------------------------------
    # Apoio
    # ------------------------------------------------------------------

    @staticmethod
    def _suggestion(
        identifier: str, tipo: str, title: str, evidence: str, action: str,
        impact_monthly: float | None = None, impact_note: str = "",
        confidence: str = "media", confidence_note: str = "",
        metrics: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "type": tipo,
            "title": title,
            "evidence": evidence,
            "action": action,
            "impact_monthly": impact_monthly,
            "impact_note": impact_note,
            "confidence": confidence,
            "confidence_note": confidence_note,
            "metrics": metrics or [],
        }

    @staticmethod
    def _recipe_cost(product: dict[str, Any], inventory: dict[str, dict[str, Any]]) -> float:
        return sum(
            inventory.get(line["inventory_id"], {}).get("unit_cost", 0) * line["quantity"]
            for line in product.get("recipe", [])
        )

    @classmethod
    def _custo_confiavel(cls, product: dict[str, Any], inventory: dict[str, dict[str, Any]]) -> float | None:
        """O custo da ficha, ou nada quando ele nao pode estar certo.

        Custo de ficha alimenta margem, preco sugerido, combo e promocao. Quando
        a ficha esta errada — e nesta loja as nove estao, por erro de unidade —
        ele nao erra sozinho: ele sai por todos esses lugares com cara de numero
        calculado. O motor chegou a publicar "um copo de R$ 7,87 custa
        R$ 1.828,17 para fazer", e uma frase dessas nao e so inutil: ela ensina
        o dono a desconfiar de tudo o que a tela diz.

        Nenhuma loja vende por oito o que lhe custa mil e oitocentos. Acima de
        tres vezes o preco, o custo e cadastro errado e nao produto caro — e a
        sugestao que dependeria dele e omitida, em vez de publicada torta.
        """

        preco = product.get("price") or 0
        custo = cls._recipe_cost(product, inventory)
        if preco <= 0 or not product.get("recipe"):
            return None
        return None if custo > preco * SUSPEITA_DE_CUSTO else custo

    @staticmethod
    def _margin(price: float, cost: float) -> float:
        return round(((price - cost) / price) * 100, 1) if price else 0.0

    @staticmethod
    def _unit_sales(detailed: list[dict[str, Any]]) -> Counter[str]:
        vendas: Counter[str] = Counter()
        for order in detailed:
            for item in order["items"]:
                vendas[item["product_id"]] += item["quantity"]
        return vendas

    def _cheapest_high_margin(self, products, inventory) -> tuple[str, float, float] | None:
        """Item barato de maior sobra: o candidato natural a anexo no caixa."""

        candidatos = [p for p in products.values() if p.get("active", True) and p.get("recipe")]
        if not candidatos:
            return None
        mediana_preco = median([p["price"] for p in candidatos])
        baratos = [p for p in candidatos if p["price"] <= mediana_preco] or candidatos
        melhor = max(baratos, key=lambda p: p["price"] - self._recipe_cost(p, inventory))
        return melhor["name"], melhor["price"], round(melhor["price"] - self._recipe_cost(melhor, inventory), 2)

    @staticmethod
    def _sizes(products: list[dict[str, Any]], category: str) -> set[int]:
        tamanhos = set()
        for product in products:
            if product.get("category") != category:
                continue
            match = re.search(r"(\d{2,4})\s*ml", _fold(product["name"]))
            if match:
                tamanhos.add(int(match.group(1)))
        return tamanhos

    def _size_gaps(self, products: list[dict[str, Any]]) -> list[tuple[str, int, dict[str, Any]]]:
        """Tamanho ausente numa escada que já existe.

        Só aponta o vão do meio ou o degrau seguinte de uma escada com pelo
        menos dois tamanhos: inventar um tamanho onde não há escada seria
        palpite, não leitura de dados.
        """

        lacunas: list[tuple[str, int, dict[str, Any]]] = []
        escada = (300, 500, 700, 1000)
        categorias = {p.get("category") for p in products if p.get("category")}
        for categoria in categorias:
            tamanhos = self._sizes(products, categoria)
            if len(tamanhos) < 2:
                continue
            menor, maior = min(tamanhos), max(tamanhos)
            # Vizinho proximo nao e vao. A escada de referencia e redonda (300,
            # 500, 700) e o cardapio real quase nunca e: esta loja vende 330 e
            # 770, e sem esta folga o motor pedia "falta o 700 ml" para quem ja
            # tem 770. Sugestao obvia como essa ensina a ignorar a tela.
            faltando = [
                degrau for degrau in escada
                if menor < degrau < maior
                and not any(abs(tamanho - degrau) <= degrau * PROXIMIDADE_DE_TAMANHO for tamanho in tamanhos)
            ]
            if not faltando:
                continue
            referencia = max(
                (p for p in products if p.get("category") == categoria and self._sizes([p], categoria)),
                key=lambda p: p["price"], default=None,
            )
            if referencia:
                lacunas.append((categoria, faltando[0], referencia))
        return lacunas[:1]

    def _scaled_price(self, reference: dict[str, Any], target_size: int, inventory) -> tuple[float | None, float]:
        """Preço e custo do tamanho novo, proporcionais ao tamanho de referência.

        Embalagem não escala com o volume — um copo é um copo — então só os
        ingredientes são multiplicados pela proporção.
        """

        match = re.search(r"(\d{2,4})\s*ml", _fold(reference["name"]))
        if not match:
            return None, 0.0
        base = int(match.group(1))
        if not base:
            return None, 0.0
        fator = target_size / base
        ingredientes = embalagem = 0.0
        for line in reference.get("recipe", []):
            item = inventory.get(line["inventory_id"])
            if not item:
                continue
            custo = item["unit_cost"] * line["quantity"]
            if is_packaging(item):
                embalagem += custo
            else:
                ingredientes += custo * fator
        custo = round(ingredientes + embalagem, 2)
        margem = self._margin(reference["price"], self._recipe_cost(reference, inventory))
        preco = round(custo / (1 - margem / 100), 2) if margem < 100 else None
        return preco, custo

    def _idle_inventory(self, products, inventory) -> list[tuple[dict[str, Any], float]]:
        usados = {
            line["inventory_id"]
            for product in products.values()
            if product.get("active", True)
            for line in product.get("recipe", [])
        }
        parados = [
            (item, round(item["quantity"] * item["unit_cost"], 2))
            for item in inventory.values()
            if item["id"] not in usados and item["quantity"] > 0 and item["unit_cost"] > 0
        ]
        parados.sort(key=lambda row: row[1], reverse=True)
        return parados[:2]
