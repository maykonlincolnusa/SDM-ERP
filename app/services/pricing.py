"""Como precificar cada item do cardápio.

Preço tem três âncoras, e elas respondem perguntas diferentes:

1. **Custo** responde *quanto eu preciso cobrar*. É o piso, e ele é duro: o
   custo com quebra, líquido da comissão do canal onde a venda acontece.
2. **Margem-alvo** responde *quanto eu quero cobrar*. É decisão do dono, e o
   sistema só faz a conta.
3. **Mercado** responde *quanto eu consigo cobrar*. Essa é a única que o
   sistema não tem como responder sozinho — não existe fonte dentro da loja
   que diga por quanto o vizinho vende.

O módulo separa a terceira em duas camadas, e nunca as confunde:

- **Mercado declarado**: o que o gestor cadastrou olhando o concorrente. É a
  referência externa de verdade, e vem com a idade da observação ao lado,
  porque preço de concorrente envelhece.
- **Mercado da própria loja**: o que os clientes *desta* loja de fato pagam —
  ticket médio por canal e a faixa de preço onde o volume se concentra. Não é
  o mercado inteiro, é a fatia dele que já compra aqui; mas é medido, e vale
  mais do que palpite sobre a rua.

Sem referência declarada, o veredito diz isso em vez de inventar posição.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from statistics import median
from typing import Any

from app.repositories.store import Store
from app.services.costing import CostingService
from app.services.erp import FUSO_LOJA, ERPService, is_active_order

# Distância do preço da loja para a mediana das referências a partir da qual o
# módulo se manifesta. Dentro disso a loja está na faixa e mexer é ruído.
FAIXA_DE_MERCADO = 0.10

# Referência mais velha que isto entra na conta, mas com aviso: preço de
# concorrente de meio ano atrás diz pouco sobre a rua de hoje.
DIAS_PARA_REFERENCIA_ENVELHECER = 120


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class PricingService:
    """Junta piso de custo, margem-alvo e mercado numa recomendação por produto."""

    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)
        self.costing = CostingService(store)

    # ------------------------------------------------------------------

    def guidance(self, days: int = 90, product_id: str | None = None) -> dict[str, Any]:
        days = max(7, min(days, 365))
        produtos = [
            produto for produto in self.store.list("products")
            if produto.get("active", True) and (product_id is None or produto["id"] == product_id)
        ]
        insumos = {item["id"]: item for item in self.store.list("inventory")}
        canais = [canal for canal in self.store.list("channels") if canal.get("active", True)]
        referencias = self.store.list("market_prices")
        quebra = self.costing._shrinkage_rates(days)

        mercado_da_loja = self._mercado_da_loja(days)
        linhas = [
            self._produto(produto, insumos, canais, referencias, quebra, mercado_da_loja, days)
            for produto in produtos
        ]
        linhas = [linha for linha in linhas if linha]
        linhas.sort(key=lambda linha: (linha["verdict"]["rank"], -linha["price"]))

        com_referencia = sum(1 for linha in linhas if linha["market"]["samples"])
        return {
            "period_days": days,
            "products": linhas,
            "store_market": mercado_da_loja,
            "coverage": {
                "products": len(linhas),
                "with_reference": com_referencia,
                "reference_count": len(referencias),
                "competitors": sorted({str(r.get("competitor")) for r in referencias if r.get("competitor")}),
            },
            "totals": {
                "below_floor": sum(1 for linha in linhas if linha["verdict"]["id"] == "abaixo-do-custo"),
                "room_to_raise": sum(1 for linha in linhas if linha["verdict"]["id"] == "espaco-para-subir"),
                "monthly_upside": round(sum(linha["verdict"].get("monthly_upside") or 0 for linha in linhas), 2),
            },
            "blockers": self._blockers(linhas, referencias, mercado_da_loja),
        }

    # ------------------------------------------------------------------

    def _produto(self, produto, insumos, canais, referencias, quebra, mercado_da_loja, days) -> dict[str, Any] | None:
        if not produto.get("recipe"):
            return None
        preco = float(produto["price"])
        ficha = sum(
            insumos.get(linha["inventory_id"], {}).get("unit_cost", 0) * linha["quantity"]
            for linha in produto["recipe"]
        )
        custo = sum(
            insumos.get(linha["inventory_id"], {}).get("unit_cost", 0) * linha["quantity"]
            / (1 - quebra.get(linha["inventory_id"], 0.0))
            for linha in produto["recipe"]
        )

        por_canal = []
        for canal in canais:
            taxa = float(canal.get("fee_percent", 0) or 0) / 100
            fixa = float(canal.get("fee_fixed", 0) or 0)
            liquido = preco * (1 - taxa) - fixa
            piso = (custo + fixa) / (1 - taxa) if taxa < 1 else None
            por_canal.append({
                "name": canal["name"],
                "fee_percent": round(taxa * 100, 1),
                "net_price": round(liquido, 2),
                "contribution": round(liquido - custo, 2),
                "margin": round((liquido - custo) / preco * 100, 1) if preco else 0.0,
                # Preço que devolve, neste canal, a mesma sobra do canal sem taxa.
                "floor_price": round(piso, 2) if piso else None,
                "price_to_match": round((preco - custo + custo + fixa) / (1 - taxa), 2) if taxa < 1 else None,
            })

        vendas = self._vendas_do_produto(produto["id"], days)
        mercado = self._mercado_declarado(produto, referencias)
        escada = [
            {"margin": alvo, "price": round(custo / (1 - alvo / 100), 2) if alvo < 100 else None}
            for alvo in (40, 50, 60, 70)
        ]
        veredito = self._veredito(produto, preco, custo, mercado, por_canal, vendas, mercado_da_loja)

        return {
            "product_id": produto["id"],
            "name": produto["name"],
            "category": produto.get("category"),
            "line": produto.get("line", "Classico"),
            "price": round(preco, 2),
            "cost": {
                "recipe": round(ficha, 2),
                "with_shrinkage": round(custo, 2),
                "shrinkage": round(custo - ficha, 2),
            },
            "margin": round((preco - custo) / preco * 100, 1) if preco else 0.0,
            "contribution": round(preco - custo, 2),
            "channels": por_canal,
            "ladder": escada,
            "market": mercado,
            "sales": vendas,
            "verdict": veredito,
        }

    # ------------------------------------------------------------------
    # Mercado declarado: o que o gestor viu na rua
    # ------------------------------------------------------------------

    def _mercado_declarado(self, produto, referencias) -> dict[str, Any]:
        hoje = datetime.now(FUSO_LOJA).date()
        candidatas = []
        for referencia in referencias:
            # Referência ligada ao produto vale mais que a de categoria: a de
            # categoria compara coisas parecidas, não iguais.
            if referencia.get("product_id") == produto["id"]:
                precisao = "produto"
            elif referencia.get("category") and referencia["category"] == produto.get("category"):
                precisao = "categoria"
            else:
                continue
            idade = None
            if referencia.get("observed_on"):
                try:
                    idade = (hoje - date.fromisoformat(str(referencia["observed_on"])[:10])).days
                except ValueError:
                    idade = None
            candidatas.append({
                "competitor": referencia.get("competitor"),
                "description": referencia.get("description"),
                "price": round(float(referencia.get("price", 0)), 2),
                "channel": referencia.get("channel"),
                "observed_on": referencia.get("observed_on"),
                "age_days": idade,
                "stale": bool(idade is not None and idade > DIAS_PARA_REFERENCIA_ENVELHECER),
                "match": precisao,
            })

        # Se existe referência do próprio produto, a de categoria sai: misturar
        # as duas diluiria a mais precisa na menos precisa.
        exatas = [linha for linha in candidatas if linha["match"] == "produto"]
        usadas = exatas or candidatas
        if not usadas:
            return {"samples": 0, "references": [], "median": None, "min": None, "max": None,
                    "position": None, "match": None, "stale_share": 0.0}

        precos = [linha["price"] for linha in usadas]
        mediana = median(precos)
        velhas = sum(1 for linha in usadas if linha["stale"])
        preco = float(produto["price"])
        distancia = (preco - mediana) / mediana if mediana else 0.0
        return {
            "samples": len(usadas),
            "references": sorted(usadas, key=lambda linha: linha["price"]),
            "median": round(mediana, 2),
            "min": round(min(precos), 2),
            "max": round(max(precos), 2),
            "distance_percent": round(distancia * 100, 1),
            "position": (
                "abaixo" if distancia < -FAIXA_DE_MERCADO
                else "acima" if distancia > FAIXA_DE_MERCADO
                else "na faixa"
            ),
            "match": usadas[0]["match"],
            "stale_share": round(velhas / len(usadas) * 100, 1),
        }

    # ------------------------------------------------------------------
    # Mercado da própria loja: o que o cliente daqui aceita pagar
    # ------------------------------------------------------------------

    def _mercado_da_loja(self, days: int) -> dict[str, Any]:
        """Ticket por canal e a faixa de preço onde o volume se concentra.

        Não é o mercado da rua — é a fatia dele que já compra aqui. Mas é
        medido, e responde uma coisa que nenhuma tabela de concorrente responde:
        até onde *este* cliente vai.
        """

        inicio = self.erp._window_start(days)
        pedidos = [
            pedido for pedido in self.store.list("orders")
            if is_active_order(pedido) and self.erp._timestamp(pedido["created_at"]) >= inicio
        ]
        if not pedidos:
            return {"orders": 0, "average_ticket": None, "channels": [], "price_bands": [], "detail_coverage": 0.0}

        por_canal: dict[str, list[float]] = defaultdict(list)
        for pedido in pedidos:
            por_canal[pedido.get("channel") or "Balcao"].append(float(pedido["total"]))

        detalhados = [pedido for pedido in pedidos if pedido.get("items")]
        faixas: Counter[str] = Counter()
        for pedido in detalhados:
            for item in pedido["items"]:
                preco = float(item.get("unit_price", 0) or 0)
                rotulo = (
                    "até R$ 15" if preco < 15 else
                    "R$ 15 a R$ 25" if preco < 25 else
                    "R$ 25 a R$ 35" if preco < 35 else "acima de R$ 35"
                )
                faixas[rotulo] += item["quantity"]

        totais = sum(faixas.values())
        return {
            "orders": len(pedidos),
            "average_ticket": round(sum(sum(v) for v in por_canal.values()) / len(pedidos), 2),
            "channels": sorted(
                [
                    {"name": nome, "orders": len(valores), "average_ticket": round(sum(valores) / len(valores), 2)}
                    for nome, valores in por_canal.items()
                ],
                key=lambda linha: -linha["average_ticket"],
            ),
            "price_bands": [
                {"band": rotulo, "units": round(unidades, 1), "share": round(unidades / totais * 100, 1)}
                for rotulo, unidades in faixas.most_common()
            ] if totais else [],
            "detail_coverage": round(len(detalhados) / len(pedidos) * 100, 1),
        }

    def _vendas_do_produto(self, product_id: str, days: int) -> dict[str, Any]:
        inicio = self.erp._window_start(days)
        unidades = receita = 0.0
        for pedido in self.store.list("orders"):
            if not is_active_order(pedido) or self.erp._timestamp(pedido["created_at"]) < inicio:
                continue
            for item in pedido.get("items", []):
                if item["product_id"] == product_id:
                    unidades += item["quantity"]
                    receita += item["line_total"]
        return {
            "units": round(unidades, 1),
            "revenue": round(receita, 2),
            "monthly_units": round(unidades / days * 30, 1) if days else 0.0,
        }

    # ------------------------------------------------------------------
    # O veredito
    # ------------------------------------------------------------------

    def _veredito(self, produto, preco, custo, mercado, canais, vendas, mercado_da_loja) -> dict[str, Any]:
        """Subir, manter ou rever — sempre com o número que sustenta a decisão."""

        pior_canal = max(canais, key=lambda linha: linha["fee_percent"]) if canais else None
        if preco <= custo:
            return {
                "id": "abaixo-do-custo", "rank": 0, "action": "Corrigir agora",
                "headline": f"{produto['name']} é vendido a {_money(preco)} e custa {_money(custo)}",
                "reason": (
                    "Cada unidade vendida aumenta o prejuízo. Aqui não há decisão de posicionamento a tomar: "
                    "o preço precisa subir ou a ficha precisa mudar."
                ),
                "suggested_price": round(custo / 0.5, 2),
                "monthly_upside": round((custo / 0.5 - preco) * vendas["monthly_units"], 2) if vendas["monthly_units"] else None,
            }

        if pior_canal and pior_canal["contribution"] <= 0:
            return {
                "id": "canal-no-prejuizo", "rank": 1, "action": "Rever preço neste canal",
                "headline": f"Em {pior_canal['name']} o preço de {_money(preco)} não cobre o custo",
                "reason": (
                    f"A taxa de {pior_canal['fee_percent']:g}% deixa {_money(pior_canal['net_price'])} para a loja, "
                    f"contra {_money(custo)} de custo. Ou o preço sobe neste canal, ou ele sai do ar."
                ),
                "suggested_price": pior_canal["floor_price"],
                "monthly_upside": None,
            }

        if mercado["samples"]:
            if mercado["position"] == "abaixo":
                alvo = round(mercado["median"], 2)
                ganho = round((alvo - preco) * vendas["monthly_units"], 2) if vendas["monthly_units"] else None
                envelhecida = " As referências são antigas, então confirme na rua antes de mexer." if mercado["stale_share"] >= 50 else ""
                return {
                    "id": "espaco-para-subir", "rank": 2, "action": "Há espaço para subir",
                    "headline": f"{_money(preco)} contra {_money(mercado['median'])} de mediana do mercado",
                    "reason": (
                        f"{abs(mercado['distance_percent']):g}% abaixo da mediana de {mercado['samples']} "
                        f"referência(s) cadastrada(s), comparadas por {mercado['match']}. "
                        f"Subir até a mediana mantém você dentro da faixa que o cliente já vê na rua." + envelhecida
                    ),
                    "suggested_price": alvo,
                    "monthly_upside": ganho,
                }
            if mercado["position"] == "acima":
                return {
                    "id": "acima-do-mercado", "rank": 3, "action": "Sustente a diferença",
                    "headline": f"{_money(preco)} contra {_money(mercado['median'])} de mediana do mercado",
                    "reason": (
                        f"{mercado['distance_percent']:g}% acima da mediana das referências. Preço acima da rua "
                        "se sustenta com o que o produto entrega — ficha, porção, promessa de rótulo. Se não houver "
                        "diferença que o cliente perceba, é volume perdido para o vizinho."
                    ),
                    "suggested_price": None,
                    "monthly_upside": None,
                }
            return {
                "id": "na-faixa", "rank": 4, "action": "Manter",
                "headline": f"{_money(preco)} está dentro da faixa do mercado",
                "reason": (
                    f"Mediana de {_money(mercado['median'])} entre {_money(mercado['min'])} e {_money(mercado['max'])}, "
                    f"com margem de {round((preco - custo) / preco * 100, 1)}% no canal sem taxa. Não há motivo de "
                    "preço para mexer; se falta volume, o problema é outro."
                ),
                "suggested_price": None,
                "monthly_upside": None,
            }

        # Sem referência externa: o que sobra é o mercado da própria loja.
        ticket = mercado_da_loja.get("average_ticket")
        contexto = ""
        if ticket:
            relacao = "acima" if preco > ticket else "abaixo"
            contexto = (
                f" O ticket médio da loja é {_money(ticket)}, então este produto está {relacao} do que o cliente "
                "daqui costuma gastar por pedido."
            )
        return {
            "id": "sem-referencia", "rank": 5, "action": "Cadastre uma referência",
            "headline": f"{_money(preco)} com margem de {round((preco - custo) / preco * 100, 1)}%, sem referência de mercado",
            "reason": (
                "Custo e margem dizem quanto você precisa cobrar; só o preço do concorrente diz quanto dá para "
                "cobrar. Cadastre em Preços o que dois ou três vizinhos cobram por um item equivalente e este "
                "veredito passa a ter a terceira âncora." + contexto
            ),
            "suggested_price": None,
            "monthly_upside": None,
        }

    # ------------------------------------------------------------------

    def _blockers(self, linhas, referencias, mercado_da_loja) -> list[dict[str, str]]:
        bloqueios: list[dict[str, str]] = []
        sem_referencia = [linha for linha in linhas if not linha["market"]["samples"]]
        if linhas and len(sem_referencia) == len(linhas):
            bloqueios.append({
                "title": "Nenhum produto tem referência de mercado",
                "description": (
                    "Sem isso o preço só pode ser julgado por custo e margem-alvo — o que responde quanto você "
                    "precisa cobrar, nunca quanto dá para cobrar. Duas ou três observações por produto, tiradas "
                    "do iFood do vizinho ou da vitrine da rua, já mudam a conversa."
                ),
            })
        elif sem_referencia:
            nomes = ", ".join(linha["name"] for linha in sem_referencia[:3])
            bloqueios.append({
                "title": f"{len(sem_referencia)} produto(s) sem referência de mercado",
                "description": f"{nomes} ainda não têm preço de concorrente cadastrado, então só aparecem com a leitura de custo.",
            })
        velhas = [r for r in referencias if r.get("observed_on")]
        if velhas:
            hoje = datetime.now(FUSO_LOJA).date()
            antigas = 0
            for referencia in velhas:
                try:
                    if (hoje - date.fromisoformat(str(referencia["observed_on"])[:10])).days > DIAS_PARA_REFERENCIA_ENVELHECER:
                        antigas += 1
                except ValueError:
                    continue
            if antigas:
                bloqueios.append({
                    "title": f"{antigas} referência(s) com mais de {DIAS_PARA_REFERENCIA_ENVELHECER} dias",
                    "description": (
                        "Preço de concorrente envelhece. Elas continuam entrando na conta, mas confirme na rua "
                        "antes de mexer no seu preço por causa delas."
                    ),
                })
        if mercado_da_loja.get("orders") and mercado_da_loja.get("detail_coverage", 0) < 50:
            bloqueios.append({
                "title": "A faixa de preço que mais vende não pode ser medida",
                "description": (
                    f"Só {mercado_da_loja['detail_coverage']}% dos pedidos têm itens, então dá para ver o ticket "
                    "médio mas não em que faixa de preço o volume se concentra. Importe o relatório de itens da "
                    "Anota Aí."
                ),
            })
        return bloqueios
