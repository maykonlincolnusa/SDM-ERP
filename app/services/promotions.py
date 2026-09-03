"""Promoções da semana, tiradas do movimento da própria loja.

Uma promoção é uma decisão de dar margem embora. Ela só se paga se trouxer
volume que não viria — e a pergunta que quase nenhuma ferramenta responde é
quanto volume é esse. Por isso toda sugestão daqui carrega o **ponto de
virada**: quantas unidades a mais aquele dia precisa vender para a promoção
não ser prejuízo. Sem esse número, "20% de desconto na segunda" é palpite com
cara de estratégia.

Três regras estruturam o módulo:

1. **Nunca no dia forte.** Desconto no dia cheio compra venda que já
   aconteceria. A promoção existe para o vale da semana.
2. **Nunca abaixo do piso.** O piso é o custo com quebra do produto, já
   descontada a taxa do canal onde a promoção vai rodar. Desconto que fura o
   piso é dinheiro entregue ao cliente por cada unidade a mais vendida.
3. **A origem sempre declarada.** Cada sugestão diz se nasceu de venda medida,
   do cadastro do cardápio ou de uma âncora que o gestor declarou. As três
   valem coisas diferentes, e confundi-las é o erro que faz a loja confiar num
   número que não existe.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from app.repositories.store import Store
from app.services.costing import CostingService
from app.services.erp import FUSO_LOJA, ERPService, is_active_order
from app.services.forecast import ForecastService
from app.services.pricing import PricingService

DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

# A semana da loja se parte em dois pedaços que se comportam de forma
# diferente, e o corte importa mais do que o dia isolado: no bloco a amostra
# soma três ou quatro observações por semana em vez de uma, e um feriado ou uma
# chuva isolada deixa de virar regra. Também é como o gestor decide na prática —
# ele monta a oferta da semana, não a oferta de terça.
BLOCOS = (
    {"id": "inicio", "name": "segunda a quarta", "weekdays": (0, 1, 2)},
    {"id": "fim", "name": "quinta a domingo", "weekdays": (3, 4, 5, 6)},
)

# Quanto o bloco precisa ficar abaixo do outro para o corte valer a pena. Abaixo
# disso os dois pedaços são a mesma semana e separar a oferta só dá trabalho.
DIFERENCA_MINIMA_ENTRE_BLOCOS = 0.08

# Quantos clientes novos precisam subir de tamanho para compensar um cliente que
# já compraria o tamanho maior e levou a oferta junto. Acima disso a oferta é
# frágil demais: basta o tamanho maior já ser popular para ela dar prejuízo.
RAZAO_MAXIMA_DE_CANIBALIZACAO = 3.0

# Espaço mínimo entre o que o tamanho maior custa a mais na tabela e o que ele
# custa a mais de insumo. Sem essa folga não há oferta possível — e a falta dela
# é um problema de preço do cardápio, não de promoção.
FOLGA_MINIMA_DO_UPGRADE = 0.25

# Dia que vende menos que isto da média da semana é vale. Acima disso a
# diferença cabe no ruído e a promoção estaria comprando venda que já viria.
FATOR_DE_VALE = 0.92

# Teto do desconto sugerido. Acima disso deixa de ser promoção e vira
# liquidação: reposiciona o preço na cabeça do cliente e é difícil de desfazer.
DESCONTO_MAXIMO = 0.25

# Quanto da contribuição por unidade a loja aceita abrir mão. Metade mantém a
# venda lucrativa mesmo se o volume extra não vier — que é o cenário provável.
FATIA_DA_CONTRIBUICAO = 0.5

# Abaixo disso o dia da semana não tem peso próprio e a sugestão diria mais
# sobre o acaso do que sobre a loja.
AMOSTRAS_MINIMAS_POR_DIA = 3


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _preco_redondo(valor: float) -> float:
    """Arredonda para baixo no meio real: preço de balcão não tem centavo quebrado."""

    return max(0.5, int(valor * 2) / 2)


class PromotionService:
    """Cruza demanda por dia, margem real, estoque e linha de marca."""

    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)
        self.costing = CostingService(store)
        self.forecast = ForecastService(store)

    # ------------------------------------------------------------------

    def week(self, days: int = 90, horizon: int = 7) -> dict[str, Any]:
        days = max(14, min(days, 365))
        horizon = max(1, min(horizon, 14))

        inicio = self.erp._window_start(days)
        pedidos = [
            pedido for pedido in self.store.list("orders")
            if is_active_order(pedido) and self.erp._timestamp(pedido["created_at"]) >= inicio
        ]
        detalhados = [pedido for pedido in pedidos if pedido.get("items")]
        cobertura = round(len(detalhados) / len(pedidos) * 100, 1) if pedidos else 0.0

        produtos = {item["id"]: item for item in self.store.list("products")}
        insumos = {item["id"]: item for item in self.store.list("inventory")}
        canais = [canal for canal in self.store.list("channels") if canal.get("active", True)]
        ancoras = [linha for linha in self.store.list("sport_anchors") if linha.get("active", True)]

        semana = self._semana(pedidos, horizon)
        canal_alvo = self._canal_de_publicacao(canais, pedidos)
        economia = self._economia_dos_produtos(produtos, insumos, canal_alvo, days)
        vendas = self._unidades_por_produto_e_dia(detalhados)

        promocoes: list[dict[str, Any]] = []
        promocoes += self._promocao_de_vale(semana, economia, vendas, cobertura, canal_alvo)
        promocoes += self._promocao_de_estoque(semana, economia, produtos, insumos, days)
        promocoes += self._promocao_sport(semana, economia, produtos, ancoras, vendas, cobertura)

        promocoes.sort(key=lambda linha: (linha["day"], -(linha.get("upside_monthly") or 0)))
        for indice, linha in enumerate(promocoes, start=1):
            linha["rank"] = indice

        return {
            "period_days": days,
            "horizon_days": horizon,
            "generated_for": [linha["date"] for linha in semana],
            "week": semana,
            "channel": canal_alvo,
            "anchors": [self._resumo_da_ancora(linha) for linha in ancoras],
            "promotions": promocoes,
            "coverage": {
                "orders": len(pedidos),
                "detailed_orders": len(detalhados),
                "detail_coverage": cobertura,
                "source": "venda medida" if cobertura >= 50 else "cadastro do cardápio",
            },
            "totals": {
                "count": len(promocoes),
                "upside_monthly": round(sum(linha.get("upside_monthly") or 0 for linha in promocoes), 2),
                "margin_at_risk_monthly": round(sum(linha.get("margin_given_monthly") or 0 for linha in promocoes), 2),
            },
            "blockers": self._blockers(pedidos, detalhados, cobertura, semana, produtos, ancoras),
        }

    # ------------------------------------------------------------------
    # O mês: quatro semanas, cada uma com sua oferta
    # ------------------------------------------------------------------

    def month(self, days: int = 90, weeks: int = 4) -> dict[str, Any]:
        """Calendário de promoções do mês, partido em começo e fim de semana.

        A loja monta uma oferta por semana, não uma por dia. E as duas metades
        da semana pedem mecânicas opostas:

        - **Começo de semana** é o pedaço vazio. Ali falta gente, e o que traz
          gente é preço. A oferta é desconto no produto âncora.
        - **Fim de semana** já está cheio. Descontar ali compra a venda que já
          aconteceria — é margem entregue de graça. O que rende no pedaço cheio
          é subir o ticket de quem já vem: upgrade de tamanho, complemento,
          segunda unidade. O preço do item principal não se toca.

        As quatro semanas não repetem a mesma oferta de propósito. Desconto que
        se repete toda semana deixa de ser promoção e vira o preço novo — o
        cliente aprende a esperar, e a loja perde a margem sem ganhar o volume.
        """

        days = max(14, min(days, 365))
        weeks = max(1, min(weeks, 8))

        inicio = self.erp._window_start(days)
        pedidos = [
            pedido for pedido in self.store.list("orders")
            if is_active_order(pedido) and self.erp._timestamp(pedido["created_at"]) >= inicio
        ]
        detalhados = [pedido for pedido in pedidos if pedido.get("items")]
        cobertura = round(len(detalhados) / len(pedidos) * 100, 1) if pedidos else 0.0

        produtos = {item["id"]: item for item in self.store.list("products")}
        insumos = {item["id"]: item for item in self.store.list("inventory")}
        addons = [linha for linha in self.store.list("addons") if linha.get("active", True)]
        canais = [canal for canal in self.store.list("channels") if canal.get("active", True)]
        ancoras = [linha for linha in self.store.list("sport_anchors") if linha.get("active", True)]

        canal = self._canal_de_publicacao(canais, pedidos)
        economia = self._economia_dos_produtos(produtos, insumos, canal, days)
        vendas = self._unidades_por_produto_e_dia(detalhados)
        blocos = self._blocos(pedidos)

        # O veredito de preço de cada produto, para viajar junto da oferta.
        orientacao = PricingService(self.store).guidance(days)
        precos = {
            linha["product_id"]: {
                "price": linha["price"],
                "cost_with_shrinkage": linha["cost"]["with_shrinkage"],
                "margin": linha["margin"],
                "verdict": linha["verdict"]["id"],
                "action": linha["verdict"]["action"],
                "headline": linha["verdict"]["headline"],
                "reason": linha["verdict"]["reason"],
                "suggested_price": linha["verdict"].get("suggested_price"),
                "market_position": linha["market"]["position"],
                "market_median": linha["market"]["median"],
                "market_samples": linha["market"]["samples"],
            }
            for linha in orientacao["products"]
        }

        fraco = min(blocos, key=lambda linha: linha["average_per_open_day"] or 0) if blocos else None
        forte = max(blocos, key=lambda linha: linha["average_per_open_day"] or 0) if blocos else None
        separavel = bool(
            fraco and forte and fraco["id"] != forte["id"]
            and forte["average_per_open_day"]
            and (forte["average_per_open_day"] - fraco["average_per_open_day"]) / forte["average_per_open_day"]
            >= DIFERENCA_MINIMA_ENTRE_BLOCOS
        )
        if fraco and forte and fraco["average_per_open_day"] is not None and forte["average_per_open_day"] is not None:
            fraco["deficit_per_day"] = round(forte["average_per_open_day"] - fraco["average_per_open_day"], 2)

        # As alavancas de cada bloco, na ordem em que giram pelas semanas. Cada
        # semana pega a proxima que estiver disponivel, e nunca a mesma ancora
        # duas semanas seguidas.
        alavancas_fraco = ("preco", "estoque", "margem-parada", "sport")
        alavancas_forte = ("upgrade", "complemento", "sport", "upgrade")

        hoje = datetime.now(FUSO_LOJA).date()
        # A primeira semana comeca na proxima segunda; a semana ja em curso
        # nao da tempo de preparar material nem avisar o cliente.
        proxima_segunda = hoje + timedelta(days=(7 - hoje.weekday()) % 7 or 7)

        calendario: list[dict[str, Any]] = []
        usados_fraco: list[str] = []
        usados_forte: list[str] = []
        for indice in range(weeks):
            comeco = proxima_segunda + timedelta(days=7 * indice)
            ofertas: list[dict[str, Any]] = []

            oferta_fraco = self._oferta_de_trafego(
                fraco, comeco, indice, alavancas_fraco, economia, vendas, insumos,
                produtos, ancoras, cobertura, canal, usados_fraco,
            )
            if oferta_fraco:
                ofertas.append(oferta_fraco)
                usados_fraco.append(oferta_fraco["product"]["id"])

            oferta_forte = self._oferta_de_ticket(
                forte, comeco, indice, alavancas_forte, economia, produtos,
                insumos, addons, ancoras, canal, usados_forte,
            )
            if oferta_forte:
                ofertas.append(oferta_forte)
                usados_forte.append(oferta_forte["product"]["id"])

            # Se o gestor so tiver folego para uma oferta na semana, esta e a
            # que ele deve rodar: a de maior ganho com o numero sustentado.
            # Promoção não conserta preço errado. Cada oferta carrega o veredito
            # de preço do produto que ela desconta, para o gestor não passar um
            # mês descontando um item que já estava barato demais — ou caro
            # demais, e por isso parado.
            for oferta in ofertas:
                oferta["pricing"] = precos.get(oferta["product"]["id"])

            titular = max(
                ofertas, key=lambda linha: (linha.get("upside_monthly") or 0),
            ) if ofertas else None
            calendario.append({
                "week": indice + 1,
                "starts_on": comeco.isoformat(),
                "ends_on": (comeco + timedelta(days=6)).isoformat(),
                "label": f"Semana {indice + 1}",
                "promotions": ofertas,
                "headline": titular["id"] if titular else None,
            })

        total = sum(len(semana["promotions"]) for semana in calendario)
        return {
            "period_days": days,
            "weeks": weeks,
            "split_worth_it": separavel,
            "split_note": (
                f"{forte['name'].capitalize()} rende "
                f"{round((forte['average_per_open_day'] / fraco['average_per_open_day'] - 1) * 100)}% mais por dia aberto "
                f"que {fraco['name']}. Separar a oferta em dois blocos vale a pena: o pedaço vazio pede preço, "
                "o pedaço cheio pede ticket."
                if separavel and fraco["average_per_open_day"]
                else "Os dois blocos da semana rendem quase o mesmo por dia aberto. Uma oferta única para a semana "
                     "inteira dá menos trabalho e o mesmo resultado."
            ) if fraco and forte else "Ainda não há movimento suficiente para comparar os dois blocos da semana.",
            "blocks": blocos,
            "weak_block": fraco["id"] if fraco else None,
            "strong_block": forte["id"] if forte else None,
            "channel": canal,
            "calendar": calendario,
            "coverage": {
                "orders": len(pedidos),
                "detailed_orders": len(detalhados),
                "detail_coverage": cobertura,
                "source": "venda medida" if cobertura >= 50 else "cadastro do cardápio",
            },
            "totals": {
                "count": total,
                "per_week": round(total / weeks, 1) if weeks else 0,
                "upside_monthly": round(sum(
                    oferta.get("upside_monthly") or 0
                    for semana in calendario for oferta in semana["promotions"]
                ), 2),
            },
            "blockers": self._blockers_do_mes(pedidos, cobertura, blocos, produtos, ancoras, calendario),
        }

    def _blocos(self, pedidos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Economia de cada metade da semana, contando só os dias em que abriu."""

        por_data: dict[date, float] = defaultdict(float)
        pedidos_por_data: dict[date, int] = defaultdict(int)
        for pedido in pedidos:
            dia = self.erp._local_day(pedido["created_at"])
            por_data[dia] += pedido["total"]
            pedidos_por_data[dia] += 1

        geral = [valor for valor in por_data.values()]
        media_geral = sum(geral) / len(geral) if geral else 0.0

        resultado = []
        for bloco in BLOCOS:
            dias = [dia for dia in por_data if dia.weekday() in bloco["weekdays"]]
            receita = sum(por_data[dia] for dia in dias)
            pedidos_do_bloco = sum(pedidos_por_data[dia] for dia in dias)
            media = receita / len(dias) if dias else None
            # Quantas vezes o bloco pode ter aberto: uma por dia da semana, por
            # semana observada. E o denominador que separa "vende pouco" de
            # "abre pouco".
            semanas = len({(dia.isocalendar().year, dia.isocalendar().week) for dia in por_data})
            possiveis = semanas * len(bloco["weekdays"])
            resultado.append({
                **bloco,
                "weekday_names": [DIAS[indice] for indice in bloco["weekdays"]],
                "revenue": round(receita, 2),
                "orders": pedidos_do_bloco,
                "open_days": len(dias),
                "possible_days": possiveis,
                "closed_days": max(0, possiveis - len(dias)),
                "average_per_open_day": round(media, 2) if media is not None else None,
                "factor": round(media / media_geral, 3) if (media is not None and media_geral) else None,
                "average_ticket": round(receita / pedidos_do_bloco, 2) if pedidos_do_bloco else None,
                "share": round(receita / sum(geral) * 100, 1) if geral and sum(geral) else 0.0,
            })
        return resultado

    # ------------------------------------------------------------------
    # A semana: onde está o vale
    # ------------------------------------------------------------------

    def _semana(self, pedidos: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
        """Fatura médio de cada dia da semana, e o quanto ele fica abaixo da mediana.

        A mediana entra no lugar da média porque um único domingo de evento
        puxaria a média para cima e faria a semana inteira parecer um vale.
        """

        por_data: dict[date, float] = defaultdict(float)
        for pedido in pedidos:
            por_data[self.erp._local_day(pedido["created_at"])] += pedido["total"]

        por_dia_da_semana: dict[int, list[float]] = defaultdict(list)
        for dia, valor in por_data.items():
            por_dia_da_semana[dia.weekday()].append(valor)

        medias = {
            indice: sum(valores) / len(valores)
            for indice, valores in por_dia_da_semana.items()
            if len(valores) >= AMOSTRAS_MINIMAS_POR_DIA
        }
        referencia = median(medias.values()) if medias else 0.0

        # Quantas vezes cada dia da semana teve movimento. Dia que aparece menos
        # que os outros nao e dia fraco: e dia em que a loja nao abriu. A
        # distincao decide tudo aqui, porque promocao nao conserta porta
        # fechada — e uma media calculada so sobre os dias abertos e a unica que
        # responde "quando abro, vendo pouco?".
        aberturas = {indice: len(valores) for indice, valores in por_dia_da_semana.items()}
        maximo_de_aberturas = max(aberturas.values()) if aberturas else 0

        hoje = datetime.now(FUSO_LOJA).date()
        semana = []
        for adiante in range(horizon):
            dia = hoje + timedelta(days=adiante)
            indice = dia.weekday()
            amostras = aberturas.get(indice, 0)
            media = medias.get(indice)
            fator = round(media / referencia, 3) if (media is not None and referencia) else None
            faltando = max(0, maximo_de_aberturas - amostras)
            semana.append({
                "date": dia.isoformat(),
                "weekday": indice,
                "name": DIAS[indice],
                "samples": amostras,
                "reliable": amostras >= AMOSTRAS_MINIMAS_POR_DIA,
                "average_revenue": round(media, 2) if media is not None else None,
                "median_revenue": round(referencia, 2),
                "factor": fator,
                "deficit": round(referencia - media, 2) if media is not None else None,
                "is_valley": bool(fator is not None and fator < FATOR_DE_VALE),
                # O dia existiu no calendario e nao teve venda nenhuma.
                "closed_days": faltando,
                "open_days": amostras,
                "mostly_closed": bool(maximo_de_aberturas and faltando / maximo_de_aberturas >= 0.25),
                "closed_upside": round(faltando * media, 2) if (media is not None and faltando) else None,
            })
        return semana

    # ------------------------------------------------------------------
    # Economia do produto: o piso que nenhum desconto pode furar
    # ------------------------------------------------------------------

    def _canal_de_publicacao(self, canais: list[dict[str, Any]], pedidos: list[dict[str, Any]]) -> dict[str, Any]:
        """Onde a promoção deve rodar: o canal que fatura sem entregar comissão.

        Promover no marketplace é dar desconto ao cliente e comissão à
        plataforma sobre a mesma venda. O canal próprio guarda as duas pontas.
        """

        receita: dict[str, float] = defaultdict(float)
        for pedido in pedidos:
            receita[pedido.get("channel") or "Balcao"] += pedido["total"]

        candidatos = [
            {
                "name": canal["name"],
                "fee_percent": float(canal.get("fee_percent", 0) or 0),
                "fee_fixed": float(canal.get("fee_fixed", 0) or 0),
                "revenue": round(receita.get(canal["name"], 0.0), 2),
            }
            for canal in canais
        ]
        if not candidatos:
            return {"name": "Balcao", "fee_percent": 0.0, "fee_fixed": 0.0, "revenue": 0.0, "reason": "sem canal cadastrado"}

        # Menor taxa primeiro; entre os de taxa igual, o que mais fatura.
        melhor = min(candidatos, key=lambda linha: (linha["fee_percent"], -linha["revenue"]))
        pior = max(candidatos, key=lambda linha: linha["fee_percent"])
        melhor = dict(melhor)
        melhor["reason"] = (
            f"{melhor['name']} não cobra comissão e já responde por {_money(melhor['revenue'])} no período."
            if melhor["fee_percent"] == 0
            else f"{melhor['name']} tem a menor taxa entre os canais cadastrados ({melhor['fee_percent']:g}%)."
        )
        if pior["fee_percent"] > melhor["fee_percent"]:
            melhor["avoid"] = (
                f"Fora de {pior['name']}: lá a taxa de {pior['fee_percent']:g}% cai sobre o preço já descontado, "
                "e a loja paga o desconto duas vezes."
            )
        return melhor

    def _economia_dos_produtos(
        self, produtos: dict[str, dict[str, Any]], insumos: dict[str, dict[str, Any]],
        canal: dict[str, Any], days: int,
    ) -> dict[str, dict[str, Any]]:
        """Preço, custo com quebra e o desconto máximo que ainda deixa lucro."""

        taxa = canal.get("fee_percent", 0) / 100
        fixa = canal.get("fee_fixed", 0)
        # As taxas de quebra saem uma vez para a loja inteira. Pedir o dossiê
        # completo de cada produto recalcularia rateio de custo fixo, canais e
        # ponto de equilíbrio — contas que esta decisão não usa — varrendo os
        # movimentos de novo a cada produto.
        quebra = self.costing._shrinkage_rates(days)
        economia: dict[str, dict[str, Any]] = {}
        for produto in produtos.values():
            if not produto.get("active", True) or not produto.get("recipe"):
                continue
            custo = round(sum(
                insumos.get(linha["inventory_id"], {}).get("unit_cost", 0) * linha["quantity"]
                / (1 - quebra.get(linha["inventory_id"], 0.0))
                for linha in produto["recipe"]
            ), 4)
            preco = float(produto["price"])
            # O que a loja recebe hoje por unidade, depois da taxa do canal.
            liquido = preco * (1 - taxa) - fixa
            contribuicao = liquido - custo
            if contribuicao <= 0:
                economia[produto["id"]] = {
                    "product_id": produto["id"], "name": produto["name"], "line": produto.get("line", "Classico"),
                    "claims": produto.get("claims", []), "nutrition": produto.get("nutrition", {}),
                    "price": round(preco, 2), "cost": round(custo, 2), "contribution": round(contribuicao, 2),
                    "max_discount": 0.0, "promotable": False,
                    "reason": "Este produto já não cobre o próprio custo neste canal; desconto só aprofundaria o prejuízo.",
                }
                continue
            # Desconto máximo antes de zerar a contribuição, e o sugerido, que
            # abre mão de metade dela — a loja continua ganhando se o volume
            # extra não vier, que é o cenário mais provável.
            desconto_maximo = contribuicao / preco
            desconto = min(desconto_maximo * FATIA_DA_CONTRIBUICAO, DESCONTO_MAXIMO)
            preco_promocional = _preco_redondo(preco * (1 - desconto))
            liquido_promocional = preco_promocional * (1 - taxa) - fixa
            contribuicao_promocional = liquido_promocional - custo
            economia[produto["id"]] = {
                "product_id": produto["id"],
                "name": produto["name"],
                "line": produto.get("line", "Classico"),
                "claims": produto.get("claims", []),
                "nutrition": produto.get("nutrition", {}),
                "price": round(preco, 2),
                "cost": round(custo, 2),
                "contribution": round(contribuicao, 2),
                "max_discount": round(desconto_maximo * 100, 1),
                "promo_price": preco_promocional,
                "promo_discount": round((1 - preco_promocional / preco) * 100, 1),
                "promo_contribution": round(contribuicao_promocional, 2),
                "floor_price": round((custo + fixa) / (1 - taxa), 2) if taxa < 1 else None,
                "promotable": contribuicao_promocional > 0 and preco_promocional < preco,
            }
        return economia

    @staticmethod
    def _ponto_de_virada(base_units: float, economia: dict[str, Any]) -> dict[str, Any]:
        """Quantas unidades a mais a promoção precisa trazer para empatar.

        É a conta que decide se vale: com o desconto, cada unidade rende menos,
        então o mesmo volume rende menos dinheiro. O ponto de virada é o volume
        extra que devolve a contribuição de antes. Se ele for maior do que o dia
        comporta, a promoção é prejuízo com aparência de movimento.
        """

        atual = base_units * economia["contribution"]
        nova = economia["promo_contribution"]
        if nova <= 0:
            return {"units": None, "note": "Sem contribuição no preço promocional: não existe volume que compense."}
        necessarias = atual / nova
        extras = max(0.0, necessarias - base_units)
        return {
            "units": round(extras, 1),
            "total_units": round(necessarias, 1),
            "base_units": round(base_units, 1),
            "growth_percent": round(extras / base_units * 100, 1) if base_units else None,
            "note": (
                f"Hoje o dia rende {_money(round(atual, 2))} de sobra com {round(base_units, 1)} unidades. "
                f"A {economia['promo_discount']:g}% cada unidade sobra {_money(economia['promo_contribution'])}, "
                f"então são {round(necessarias, 1)} unidades para empatar — {round(extras, 1)} a mais do que hoje."
            ),
        }

    # ------------------------------------------------------------------
    # Bloco vazio: a oferta que traz gente
    # ------------------------------------------------------------------

    def _oferta_de_trafego(
        self, bloco, comeco, indice, alavancas, economia, vendas, insumos,
        produtos, ancoras, cobertura, canal, usados,
    ) -> dict[str, Any] | None:
        """No pedaço vazio da semana, o que falta é gente — e o que traz gente é preço."""

        if not bloco or not economia:
            return None
        promovveis = [linha for linha in economia.values() if linha["promotable"]]
        if not promovveis:
            return None

        alavanca = alavancas[indice % len(alavancas)]
        escolhido, motivo, extra = self._alavanca_de_trafego(
            alavanca, promovveis, economia, produtos, insumos, ancoras, vendas, cobertura, usados,
        )
        if not escolhido:
            # Alavanca indisponivel nesta semana cai para o ancora de sempre, em
            # vez de deixar a semana sem oferta.
            escolhido = self._sem_repetir(promovveis, usados, key=lambda linha: linha["contribution"])
            motivo, extra = "Produto de maior sobra por unidade do cardápio.", {}
        if not escolhido:
            return None

        # O ponto de virada é do bloco inteiro, não de um dia: a oferta roda os
        # três dias, e é o total deles que precisa empatar.
        base = sum(vendas.get(escolhido["product_id"], {}).get(dia, 0.0) for dia in bloco["weekdays"])
        virada = self._ponto_de_virada(base, escolhido) if base else None
        aberto = bloco["open_days"] or 1
        # Quanto o bloco fraco rende a menos que o cheio, por dia aberto. É o
        # tamanho do buraco que a oferta tenta fechar.
        deficit = bloco.get("deficit_per_day")

        return {
            "id": f"trafego-s{indice + 1}-{escolhido['product_id']}",
            "week": indice + 1,
            "block": bloco["id"],
            "block_name": bloco["name"],
            "day": comeco.isoformat(),
            "weekday": bloco["name"],
            "runs_from": comeco.isoformat(),
            "runs_to": (comeco + timedelta(days=max(bloco["weekdays"]))).isoformat(),
            "kind": "trafego",
            "goal": "Trazer gente ao pedaço vazio da semana",
            "title": f"{escolhido['name']} a {_money(escolhido['promo_price'])} de {bloco['name']}",
            "product": {"id": escolhido["product_id"], "name": escolhido["name"], "line": escolhido["line"]},
            "mechanic": "Preço promocional válido só neste bloco da semana",
            "price": {
                "from": escolhido["price"], "to": escolhido["promo_price"],
                "discount_percent": escolhido["promo_discount"],
            },
            "floor": {
                "cost_with_shrinkage": escolhido["cost"],
                "min_price": escolhido["floor_price"],
                "max_discount_percent": escolhido["max_discount"],
                "channel": canal["name"],
            },
            "evidence": (
                f"{bloco['name'].capitalize()} rende {_money(bloco['average_per_open_day'])} por dia aberto"
                + (f", {round(deficit / bloco['average_per_open_day'] * 100)}% abaixo do outro bloco da semana"
                   if deficit and bloco["average_per_open_day"] else "")
                + f", medido em {bloco['open_days']} dias abertos. {motivo}"
            ),
            "why_this_day": (
                "O custo fixo corre igual nos sete dias. No pedaço vazio o desconto compra movimento que não "
                "existe; no pedaço cheio compraria venda que já viria."
            ),
            "breakeven": virada,
            "margin_given": round(escolhido["contribution"] - escolhido["promo_contribution"], 2),
            "margin_given_monthly": round((escolhido["contribution"] - escolhido["promo_contribution"]) * base * 4.3, 2) if base else None,
            "upside_monthly": round(deficit * aberto * 0.4, 2) if deficit else None,
            "upside_note": (
                "Premissa: a oferta recupera 40% da distância entre os dois blocos, nos dias abertos do período. "
                "É premissa, não previsão."
            ) if deficit else "Sem diferença medida entre os blocos, o ganho não tem base para ser estimado.",
            "channel": canal["name"],
            "channel_note": canal.get("reason", ""),
            "source": "venda medida" if (cobertura >= 50 and base) else "cadastro do cardápio",
            "confidence": "alta" if bloco["open_days"] >= 12 else "media",
            "confidence_note": f"{bloco['open_days']} dias abertos de {bloco['name']} no período",
        }

    def _alavanca_de_trafego(
        self, alavanca, promovveis, economia, produtos, insumos, ancoras, vendas, cobertura, usados,
    ):
        """Escolhe o produto da semana pela alavanca da vez, sem repetir a anterior."""

        if alavanca == "estoque":
            consumo = self.erp.consumption(60)
            giro = {linha["inventory_id"]: linha["daily_average"] for linha in consumo["items"]}
            for insumo in sorted(insumos.values(), key=lambda item: -(item.get("quantity", 0) * item.get("unit_cost", 0))):
                validade, saldo = insumo.get("shelf_life_days"), insumo.get("quantity", 0)
                diario = giro.get(insumo["id"], 0.0)
                if not validade or saldo <= 0 or diario <= 0 or saldo / diario <= validade:
                    continue
                dono = self._produto_que_mais_consome(insumo["id"], produtos, economia)
                if dono and dono["product_id"] not in usados:
                    excedente = round(saldo - diario * validade, 3)
                    return dono, (
                        f"{insumo['name']} tem {round(saldo / diario)} dias de saldo para uma validade de "
                        f"{validade} dias: {excedente:g} {insumo['unit']} vencem antes de vender, e este é o "
                        "produto que mais consome esse insumo."
                    ), {}
            return None, "", {}

        if alavanca == "margem-parada" and cobertura >= 50:
            # Margem boa e giro fraco e problema de exposicao, nao de preco — e
            # a semana de promocao e a exposicao mais barata que existe.
            com_giro = [
                linha for linha in promovveis
                if linha["product_id"] not in usados and sum(vendas.get(linha["product_id"], {}).values())
            ]
            if com_giro:
                giro_mediano = median([sum(vendas.get(l["product_id"], {}).values()) for l in com_giro])
                candidatos = [
                    linha for linha in com_giro
                    if sum(vendas.get(linha["product_id"], {}).values()) < giro_mediano
                ]
                if candidatos:
                    melhor = max(candidatos, key=lambda linha: linha["contribution"])
                    return melhor, (
                        f"{melhor['name']} deixa {_money(melhor['contribution'])} por unidade e gira abaixo da "
                        "mediana do cardápio: o problema dele é exposição, não preço."
                    ), {}
            return None, "", {}

        if alavanca == "sport":
            sport = [l for l in promovveis if l["line"] == "Sport" and l["product_id"] not in usados]
            if sport and ancoras:
                melhor = max(sport, key=lambda linha: linha["contribution"])
                nomes = ", ".join(a["name"] for a in ancoras[:2])
                return melhor, f"{nomes} está cadastrado como âncora e este é o item da linha Sport com maior sobra.", {}
            return None, "", {}

        # "preco": o ancora de sempre, o que mais sobra por unidade.
        escolhido = self._sem_repetir(promovveis, usados, key=lambda linha: linha["contribution"])
        return escolhido, "Produto de maior sobra por unidade entre os que ainda cobrem o próprio custo.", {}

    @staticmethod
    def _sem_repetir(candidatos, usados, key):
        """Evita a mesma âncora duas semanas seguidas.

        Desconto que se repete deixa de ser promocao e vira o preco novo: o
        cliente aprende a esperar a semana da oferta e a loja perde a margem sem
        ganhar o volume.
        """

        inéditos = [linha for linha in candidatos if linha["product_id"] not in usados]
        # Esgotado o cardápio, o mês repete — mas repete o mais distante, nunca
        # o da semana passada.
        recentes = [linha for linha in candidatos if linha["product_id"] not in usados[-1:]]
        pool = inéditos or recentes or candidatos
        return max(pool, key=key) if pool else None

    # ------------------------------------------------------------------
    # Bloco cheio: a oferta que sobe o ticket sem descontar
    # ------------------------------------------------------------------

    def _oferta_de_ticket(
        self, bloco, comeco, indice, alavancas, economia, produtos, insumos, addons, ancoras, canal, usados,
    ) -> dict[str, Any] | None:
        """No pedaço cheio a gente já vem. Descontar aqui é margem entregue de graça."""

        if not bloco or not economia:
            return None

        alavanca = alavancas[indice % len(alavancas)]
        oferta = None
        if alavanca == "upgrade":
            oferta = self._upgrade_de_tamanho(produtos, economia, usados)
        if oferta is None and alavanca in ("complemento", "upgrade"):
            oferta = self._oferta_de_complemento(addons, insumos, economia, usados)
        if oferta is None and alavanca == "sport":
            oferta = self._destaque_sport(economia, ancoras, usados)
        if oferta is None:
            oferta = self._upgrade_de_tamanho(produtos, economia, usados) or self._oferta_de_complemento(addons, insumos, economia, usados)
        if oferta is None:
            return None

        return {
            **oferta,
            "id": f"ticket-s{indice + 1}-{oferta['product']['id']}",
            "week": indice + 1,
            "block": bloco["id"],
            "block_name": bloco["name"],
            "day": (comeco + timedelta(days=min(bloco["weekdays"]))).isoformat(),
            "weekday": bloco["name"],
            "runs_from": (comeco + timedelta(days=min(bloco["weekdays"]))).isoformat(),
            "runs_to": (comeco + timedelta(days=max(bloco["weekdays"]))).isoformat(),
            "kind": "ticket",
            "goal": "Subir o ticket de quem já vem, sem descontar o item principal",
            "why_this_day": (
                f"{bloco['name'].capitalize()} já rende {_money(bloco['average_per_open_day'])} por dia aberto — "
                f"{round((bloco['factor'] - 1) * 100)}% acima da média. Baixar preço aqui compraria a venda que já "
                "aconteceria; o que rende no pedaço cheio é cada cliente gastar um pouco mais."
            ) if bloco.get("factor") else "No pedaço cheio da semana o que rende é ticket, não desconto.",
            "channel": canal["name"],
            "channel_note": canal.get("reason", ""),
            "confidence_note": f"{bloco['open_days']} dias abertos de {bloco['name']} no período",
        }

    def _upgrade_de_tamanho(self, produtos, economia, usados) -> dict[str, Any] | None:
        """Levar o tamanho maior por uma diferença menor que a da tabela.

        O insumo a mais de um copo maior custa pouco perto do que ele custa a
        mais na tabela. Isso abre espaço para uma oferta que sobe o ticket e a
        sobra por pedido ao mesmo tempo — sem tocar no preço de nada.
        """

        escadas: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for produto in produtos.values():
            linha = economia.get(produto["id"])
            if not linha or not produto.get("active", True):
                continue
            medida = re.search(r"(\d{2,4})\s*ml", produto["name"].lower())
            if medida:
                escadas[produto.get("category") or "—"].append((int(medida.group(1)), linha))

        melhor = None
        self._escadas_apertadas = []
        for degraus in escadas.values():
            degraus.sort()
            for (tamanho_menor, menor), (tamanho_maior, maior) in zip(degraus, degraus[1:]):
                diferenca_tabela = maior["price"] - menor["price"]
                custo_extra = maior["cost"] - menor["cost"]
                if diferenca_tabela <= 0 or custo_extra < 0:
                    continue
                # Sem folga entre o que o tamanho maior cobra a mais e o que ele
                # custa a mais, não existe oferta possível — e a falta de folga é
                # um problema de preço do cardápio, que fica registrado para
                # virar aviso em vez de sumir.
                folga = diferenca_tabela - custo_extra
                if folga < max(1.0, diferenca_tabela * FOLGA_MINIMA_DO_UPGRADE):
                    self._escadas_apertadas.append({
                        "menor": menor, "maior": maior, "diferenca": round(diferenca_tabela, 2),
                        "custo_extra": round(custo_extra, 2), "folga": round(folga, 2),
                    })
                    continue
                if maior["product_id"] in usados:
                    continue
                # A oferta fica no meio do caminho entre o custo do insumo extra
                # e a diferença de tabela: o cliente ganha de verdade e a loja
                # ainda sobe a sobra por pedido.
                acrescimo = _preco_redondo(custo_extra + folga * 0.45)
                ganho = round(acrescimo - custo_extra, 2)
                perda = round(diferenca_tabela - acrescimo, 2)
                if ganho <= 0 or acrescimo >= diferenca_tabela:
                    continue
                # Uma oferta que precisa de muita conversão para compensar cada
                # canibalização é frágil: basta o tamanho maior já ser popular
                # para ela dar prejuízo.
                if perda / ganho > RAZAO_MAXIMA_DE_CANIBALIZACAO:
                    continue
                candidato = {
                    "product": {"id": maior["product_id"], "name": maior["name"], "line": maior["line"]},
                    "title": f"Leve o {maior['name']} por mais {_money(acrescimo)} no lugar do {menor['name']}",
                    "mechanic": f"Upgrade de tamanho: {tamanho_menor} ml para {tamanho_maior} ml",
                    "price": {"from": maior["price"], "to": round(menor["price"] + acrescimo, 2), "discount_percent": round(perda / maior["price"] * 100, 1)},
                    "floor": {
                        "cost_with_shrinkage": maior["cost"],
                        "min_price": maior["floor_price"],
                        "max_discount_percent": maior["max_discount"],
                        "channel": None,
                    },
                    "evidence": (
                        f"Na tabela o {maior['name']} custa {_money(diferenca_tabela)} a mais que o {menor['name']}, "
                        f"mas o insumo a mais custa só {_money(custo_extra)}. Cobrando {_money(acrescimo)} pelo "
                        f"upgrade, cada conversão sobe o ticket em {_money(acrescimo)} e a sobra em {_money(ganho)}."
                    ),
                    # Este e o risco real da oferta, e ele nao e o desconto: e
                    # quem ja compraria o tamanho maior levar a promocao junto.
                    "breakeven": {
                        "units": round(perda / ganho, 1) if ganho else None,
                        "total_units": None,
                        "base_units": None,
                        "growth_percent": None,
                        "note": (
                            f"Quem já compraria o {maior['name']} e aceitar a oferta deixa de pagar {_money(perda)}. "
                            f"Cada um desses precisa ser compensado por {round(perda / ganho, 1)} cliente(s) que "
                            f"vieram pelo {menor['name']} e subiram. Se o {maior['name']} já é a maioria das vendas, "
                            "não rode esta oferta."
                        ),
                    },
                    "margin_given": perda,
                    "margin_given_monthly": None,
                    "upside_monthly": None,
                    "upside_note": (
                        f"Cada conversão rende {_money(ganho)} a mais de sobra. Sem o volume por produto medido, "
                        "o total do mês não pode ser projetado — só o ganho por conversão."
                    ),
                    "source": "cadastro do cardápio",
                    "confidence": "media",
                    "_razao": perda / ganho,
                }
                # Vence a de menor risco, não a de maior ganho: o ganho por
                # conversão é pequeno em qualquer uma, e o que separa uma oferta
                # boa de uma ruim aqui é quanta canibalização ela aguenta.
                if melhor is None or candidato["_razao"] < melhor["_razao"]:
                    melhor = candidato
        if melhor:
            melhor.pop("_razao", None)
        return melhor

    def _oferta_de_complemento(self, addons, insumos, economia, usados) -> dict[str, Any] | None:
        """Complemento por um preço de convite: sobe o ticket sem mexer no produto."""

        candidatos = []
        for addon in addons:
            custo = insumos.get(addon["inventory_id"], {}).get("unit_cost", 0) * addon["quantity"]
            preco = float(addon.get("price", 0) or 0)
            if preco <= custo:
                continue
            convite = _preco_redondo(custo + (preco - custo) * 0.5)
            if convite >= preco or convite - custo <= 0:
                continue
            candidatos.append((round(convite - custo, 2), addon, custo, preco, convite))
        if not candidatos:
            return None

        ganho, addon, custo, preco, convite = max(candidatos, key=lambda linha: linha[0])
        ancora = max(
            (linha for linha in economia.values() if linha["promotable"] and linha["product_id"] not in usados[-1:]),
            key=lambda linha: linha["contribution"], default=None,
        )
        if not ancora:
            return None
        return {
            "product": {"id": ancora["product_id"], "name": ancora["name"], "line": ancora["line"]},
            "title": f"{addon['name']} por {_money(convite)} em qualquer {ancora['name']}",
            "mechanic": "Complemento a preço de convite, com o produto no preço cheio",
            "price": {"from": ancora["price"], "to": round(ancora["price"] + convite, 2), "discount_percent": 0.0},
            "floor": {
                "cost_with_shrinkage": ancora["cost"],
                "min_price": ancora["floor_price"],
                "max_discount_percent": ancora["max_discount"],
                "channel": None,
            },
            "evidence": (
                f"{addon['name']} custa {_money(round(custo, 2))} à loja e é vendido a {_money(preco)}. "
                f"A {_money(convite)} cada adesão ainda deixa {_money(ganho)} e sobe o ticket do pedido — "
                "sem desconto nenhum no produto."
            ),
            "breakeven": {
                "units": 0.0,
                "total_units": None,
                "base_units": None,
                "growth_percent": None,
                "note": (
                    f"Não há ponto de virada: o produto não é descontado e cada adesão soma {_money(ganho)}. "
                    f"O único cuidado é o cliente que já pagava {_money(preco)} pelo complemento — para ele, "
                    f"a oferta custa {_money(round(preco - convite, 2))}."
                ),
            },
            "margin_given": round(preco - convite, 2),
            "margin_given_monthly": None,
            "upside_monthly": None,
            "upside_note": f"Cada adesão rende {_money(ganho)}. O total depende da taxa de adesão, que ainda não foi medida.",
            "source": "cadastro do cardápio",
            "confidence": "media",
        }

    def _destaque_sport(self, economia, ancoras, usados) -> dict[str, Any] | None:
        sport = [
            linha for linha in economia.values()
            if linha["line"] == "Sport" and linha["promotable"] and linha["product_id"] not in usados[-1:]
        ]
        if not sport or not ancoras:
            return None
        escolhido = max(sport, key=lambda linha: linha["contribution"])
        nomes = ", ".join(a["name"] for a in ancoras[:2])
        return {
            "product": {"id": escolhido["product_id"], "name": escolhido["name"], "line": "Sport"},
            "title": f"{escolhido['name']} em destaque no fim de semana",
            "mechanic": "Destaque da linha Sport, com o preço cheio",
            "price": {"from": escolhido["price"], "to": escolhido["price"], "discount_percent": 0.0},
            "floor": {
                "cost_with_shrinkage": escolhido["cost"],
                "min_price": escolhido["floor_price"],
                "max_discount_percent": escolhido["max_discount"],
                "channel": None,
            },
            "evidence": f"{nomes} cadastrado como âncora, e {escolhido['name']} é o item Sport de maior sobra ({_money(escolhido['contribution'])} por unidade).",
            "breakeven": {
                "units": 0.0, "total_units": None, "base_units": None, "growth_percent": None,
                "note": "Sem desconto não há ponto de virada: o risco desta semana é de exposição, não de margem.",
            },
            "margin_given": 0.0,
            "margin_given_monthly": None,
            "upside_monthly": None,
            "upside_note": self._promessa(escolhido),
            "source": "âncora declarada",
            "confidence": "media",
        }

    def _blockers_do_mes(self, pedidos, cobertura, blocos, produtos, ancoras, calendario) -> list[dict[str, str]]:
        bloqueios: list[dict[str, str]] = []
        for bloco in blocos:
            if bloco["closed_days"] >= max(1, bloco["possible_days"] * 0.2):
                bloqueios.append({
                    "title": f"{bloco['name'].capitalize()}: {bloco['closed_days']} dias sem abrir",
                    "description": (
                        f"De {bloco['possible_days']} dias possíveis, a loja abriu {bloco['open_days']}. "
                        f"Quando abre, o bloco rende {_money(bloco['average_per_open_day'] or 0)} por dia. "
                        "Promoção não recupera dia fechado — decidir a escala vem antes de decidir a oferta."
                    ),
                })
        if pedidos and cobertura < 50:
            bloqueios.append({
                "title": f"{round(100 - cobertura, 1)}% dos pedidos não têm itens",
                "description": (
                    "Sem os itens vendidos, o ponto de virada e a taxa de adesão não podem ser calculados, e o "
                    "produto de cada semana é escolhido pelo cardápio em vez de pela venda. Importe o relatório "
                    "de itens da Anota Aí."
                ),
            })
        # A escada de preço apertada não é ausência de oferta: é um achado de
        # precificação. O copo maior cobra pouco a mais e leva muito a mais de
        # insumo, então ele é pior negócio que o menor — e nenhuma promoção
        # conserta isso, só o preço.
        for escada in getattr(self, "_escadas_apertadas", [])[:1]:
            menor, maior = escada["menor"], escada["maior"]
            bloqueios.append({
                "title": f"{maior['name']} cobra pouco a mais que {menor['name']} para o insumo que leva",
                "description": (
                    f"Na tabela a diferença é de {_money(escada['diferenca'])}, mas o insumo a mais custa "
                    f"{_money(escada['custo_extra'])} — sobram {_money(escada['folga'])} de folga. "
                    f"Isso torna o {maior['name']} um negócio pior que o {menor['name']} a cada venda, e fecha a "
                    "porta para oferta de upgrade, que é a forma mais barata de subir ticket sem dar desconto. "
                    "É preço de cardápio para rever, não promoção para criar."
                ),
            })

        if not any(semana["promotions"] for semana in calendario):
            bloqueios.append({
                "title": "Nenhuma oferta pôde ser montada",
                "description": (
                    "Nenhum produto ativo do cardápio cobre o próprio custo com folga suficiente para uma oferta, "
                    "ou falta ficha técnica. Confira o dossiê de custo em Cardápio antes de promover qualquer coisa."
                ),
            })
        return bloqueios

    # ------------------------------------------------------------------
    # Regra 1: o vale da semana
    # ------------------------------------------------------------------

    def _promocao_de_vale(self, semana, economia, vendas, cobertura, canal) -> list[dict[str, Any]]:
        candidatos = [dia for dia in semana if dia["is_valley"] and dia["reliable"]]
        if not candidatos or not economia:
            return []

        promovveis = [linha for linha in economia.values() if linha["promotable"]]
        if not promovveis:
            return []

        sugestoes = []
        for dia in candidatos[:2]:
            escolhido = self._ancora_do_dia(promovveis, vendas, dia["weekday"], cobertura)
            base = vendas.get(escolhido["product_id"], {}).get(dia["weekday"])
            medido = base is not None
            if not medido:
                # Sem itens no histórico não há volume por produto. O ponto de
                # virada vira "por unidade", que ainda decide a promoção.
                base = 0.0
            virada = self._ponto_de_virada(base, escolhido) if base else None
            margem_aberta = (escolhido["contribution"] - escolhido["promo_contribution"])
            sugestoes.append({
                "id": f"vale-{dia['date']}-{escolhido['product_id']}",
                "day": dia["date"],
                "weekday": dia["name"],
                "kind": "vale-da-semana",
                "title": f"{escolhido['name']} a {_money(escolhido['promo_price'])} na {dia['name']}",
                "product": {"id": escolhido["product_id"], "name": escolhido["name"], "line": escolhido["line"]},
                "mechanic": "Preço promocional do dia",
                "price": {
                    "from": escolhido["price"], "to": escolhido["promo_price"],
                    "discount_percent": escolhido["promo_discount"],
                },
                "floor": {
                    "cost_with_shrinkage": escolhido["cost"],
                    "min_price": escolhido["floor_price"],
                    "max_discount_percent": escolhido["max_discount"],
                    "channel": canal["name"],
                },
                "evidence": (
                    f"{dia['name'].capitalize()} fatura {_money(dia['average_revenue'])} contra "
                    f"{_money(dia['median_revenue'])} de um dia mediano — {round((1 - dia['factor']) * 100)}% abaixo, "
                    f"medido em {dia['samples']} {dia['name']}s."
                ),
                "why_this_day": (
                    "O custo fixo corre igual em todo dia da semana. Descontar no dia cheio compra venda que "
                    "já aconteceria; descontar no vale compra movimento que não existe."
                ),
                "breakeven": virada,
                "margin_given": round(margem_aberta, 2),
                "margin_given_monthly": round(margem_aberta * (base or 0) * 4.3, 2) if base else None,
                "upside_monthly": round((dia["deficit"] or 0) * 4.3 * 0.4, 2),
                "upside_note": (
                    f"Premissa: a ação recupera 40% da distância para a mediana, em ~4,3 {dia['name']}s por mês. "
                    "É premissa, não previsão."
                ),
                "channel": canal["name"],
                "channel_note": canal.get("reason", ""),
                "source": "venda medida" if medido else "cadastro do cardápio",
                "confidence": "alta" if (medido and dia["samples"] >= 6) else "media",
                "confidence_note": (
                    f"{dia['samples']} observações da {dia['name']}"
                    + ("" if medido else f"; produto escolhido pelo cadastro, porque só {cobertura}% dos pedidos têm itens")
                ),
            })
        return sugestoes

    def _ancora_do_dia(self, promovveis, vendas, weekday, cobertura) -> dict[str, Any]:
        """O produto que a promoção deve puxar.

        Com itens no histórico, é o que mais sobra no dia — o que já tem público
        e ainda tem margem. Sem itens, a escolha só pode vir do cadastro, e a
        sugestão diz isso em vez de fingir que mediu.
        """

        if cobertura >= 50:
            com_venda = [
                linha for linha in promovveis
                if vendas.get(linha["product_id"], {}).get(weekday)
            ]
            if com_venda:
                return max(
                    com_venda,
                    key=lambda linha: vendas[linha["product_id"]][weekday] * linha["contribution"],
                )
        return max(promovveis, key=lambda linha: linha["contribution"])

    def _unidades_por_produto_e_dia(self, detalhados) -> dict[str, dict[int, float]]:
        """Média de unidades de cada produto em cada dia da semana."""

        contagem: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        por_dia: dict[tuple[str, date], float] = defaultdict(float)
        dias_vistos: dict[int, set[date]] = defaultdict(set)
        for pedido in detalhados:
            dia = self.erp._local_day(pedido["created_at"])
            dias_vistos[dia.weekday()].add(dia)
            for item in pedido["items"]:
                por_dia[(item["product_id"], dia)] += item["quantity"]
        for (produto_id, dia), unidades in por_dia.items():
            contagem[produto_id][dia.weekday()].append(unidades)

        media: dict[str, dict[int, float]] = {}
        for produto_id, dias in contagem.items():
            media[produto_id] = {}
            for indice, valores in dias.items():
                # Divide pelo total de dias daquele weekday, e não pelos dias em
                # que o produto saiu: um item que vendeu em 2 de 12 segundas tem
                # média baixa mesmo, e arredondar isso para cima inventaria giro.
                total_dias = len(dias_vistos.get(indice, set())) or len(valores)
                media[produto_id][indice] = round(sum(valores) / total_dias, 2)
        return media

    # ------------------------------------------------------------------
    # Regra 2: o que vai vencer antes de vender
    # ------------------------------------------------------------------

    def _promocao_de_estoque(self, semana, economia, produtos, insumos, days) -> list[dict[str, Any]]:
        """Promoção que economiza em vez de custar.

        Insumo que vence antes de girar já está perdido; o desconto não abre
        mão de margem, ele recupera parte de um custo que a loja teria de
        qualquer jeito. É a única promoção aqui cujo piso pode ser o custo.
        """

        consumo = self.erp.consumption(min(days, 90))
        giro = {linha["inventory_id"]: linha["daily_average"] for linha in consumo["items"]}

        sugestoes = []
        for insumo in insumos.values():
            validade = insumo.get("shelf_life_days")
            saldo = insumo.get("quantity", 0)
            if not validade or saldo <= 0:
                continue
            diario = giro.get(insumo["id"], 0.0)
            if diario <= 0:
                continue
            dias_de_saldo = saldo / diario
            if dias_de_saldo <= validade:
                continue

            dono = self._produto_que_mais_consome(insumo["id"], produtos, economia)
            if not dono:
                continue
            excedente = round(saldo - diario * validade, 3)
            risco = round(excedente * insumo.get("unit_cost", 0), 2)
            if risco < 5:
                continue
            dia = semana[0]
            sugestoes.append({
                "id": f"estoque-{insumo['id']}",
                "day": dia["date"],
                "weekday": dia["name"],
                "kind": "estoque-em-risco",
                "title": f"Girar {insumo['name']} antes de virar perda",
                "product": {"id": dono["product_id"], "name": dono["name"], "line": dono["line"]},
                "mechanic": "Destaque do dia no produto que mais usa esse insumo",
                "price": {
                    "from": dono["price"], "to": dono["promo_price"],
                    "discount_percent": dono["promo_discount"],
                },
                "floor": {
                    "cost_with_shrinkage": dono["cost"],
                    "min_price": dono["floor_price"],
                    "max_discount_percent": dono["max_discount"],
                    "channel": None,
                },
                "evidence": (
                    f"{insumo['name']}: {saldo:g} {insumo['unit']} em estoque para um consumo de "
                    f"{diario:g} {insumo['unit']} por dia — {round(dias_de_saldo)} dias de saldo para uma validade "
                    f"de {validade} dias. Sobram {excedente:g} {insumo['unit']} que vencem antes de vender."
                ),
                "why_this_day": (
                    "Aqui a promoção não abre mão de margem: sem ela esse insumo vira perda inteira. "
                    "Qualquer preço acima do custo recupera dinheiro que já estava no lixo."
                ),
                "breakeven": None,
                "margin_given": round(dono["contribution"] - dono["promo_contribution"], 2),
                "margin_given_monthly": None,
                "upside_monthly": risco,
                "upside_note": f"{_money(risco)} é o custo do insumo que vence sem essa ação — o ganho é não perder.",
                "channel": None,
                "channel_note": "",
                "source": "venda medida",
                "confidence": "alta",
                "confidence_note": "saldo e consumo vêm dos movimentos de estoque, não de estimativa",
            })
        return sorted(sugestoes, key=lambda linha: -linha["upside_monthly"])[:3]

    @staticmethod
    def _produto_que_mais_consome(inventory_id, produtos, economia) -> dict[str, Any] | None:
        melhor, maior = None, 0.0
        for produto in produtos.values():
            linha = economia.get(produto["id"])
            if not linha or not linha["promotable"]:
                continue
            quantidade = sum(
                item["quantity"] for item in produto.get("recipe", [])
                if item["inventory_id"] == inventory_id
            )
            if quantidade > maior:
                melhor, maior = linha, quantidade
        return melhor

    # ------------------------------------------------------------------
    # Regra 3: a linha Sport
    # ------------------------------------------------------------------

    def _promocao_sport(self, semana, economia, produtos, ancoras, vendas, cobertura) -> list[dict[str, Any]]:
        """A entrada no escopo esportivo, sem inventar hábito de treino.

        Com âncora cadastrada, o dia vem do que o gestor declarou. Sem âncora,
        o dia vem de onde a linha Sport já vende melhor que a Clássica. Sem
        nenhum dos dois, este módulo não sugere nada — a falta vira bloqueio, e
        não um palpite sobre quando o bairro treina.
        """

        sport = [linha for linha in economia.values() if linha["line"] == "Sport" and linha["promotable"]]
        if not sport:
            return []

        escolhido = max(sport, key=lambda linha: linha["contribution"])
        promessa = self._promessa(escolhido)

        sugestoes = []
        if ancoras:
            dias_da_ancora = {
                indice: linha
                for linha in ancoras
                for indice in (linha.get("weekdays") or list(range(7)))
            }
            for dia in semana:
                ancora = dias_da_ancora.get(dia["weekday"])
                if not ancora:
                    continue
                pessoas = ancora.get("people")
                sugestoes.append({
                    "id": f"sport-ancora-{ancora['id']}-{dia['date']}",
                    "day": dia["date"],
                    "weekday": dia["name"],
                    "kind": "linha-sport",
                    "title": f"{escolhido['name']} para {ancora['name']} na {dia['name']}",
                    "product": {"id": escolhido["product_id"], "name": escolhido["name"], "line": "Sport"},
                    "mechanic": f"Oferta de {ancora.get('time_of_day', 'Manhã').lower()} para quem treina",
                    "price": {
                        "from": escolhido["price"], "to": escolhido["promo_price"],
                        "discount_percent": escolhido["promo_discount"],
                    },
                    "floor": {
                        "cost_with_shrinkage": escolhido["cost"],
                        "min_price": escolhido["floor_price"],
                        "max_discount_percent": escolhido["max_discount"],
                        "channel": None,
                    },
                    "evidence": (
                        f"{ancora['name']} ({ancora.get('kind', 'Academia').lower()}) treina "
                        f"{DIAS[dia['weekday']]} de {ancora.get('time_of_day', 'manhã').lower()}"
                        + (f", com {pessoas} pessoas declaradas." if pessoas else ".")
                        + " Âncora cadastrada por você — o sistema não mede treino, ele usa o que você declarou."
                    ),
                    "why_this_day": promessa,
                    "breakeven": self._ponto_de_virada(
                        vendas.get(escolhido["product_id"], {}).get(dia["weekday"], 0.0), escolhido
                    ) if vendas.get(escolhido["product_id"], {}).get(dia["weekday"]) else None,
                    "margin_given": round(escolhido["contribution"] - escolhido["promo_contribution"], 2),
                    "margin_given_monthly": None,
                    "upside_monthly": round(pessoas * 0.05 * escolhido["promo_contribution"] * 4.3, 2) if pessoas else None,
                    "upside_note": (
                        f"Premissa: 5% das {pessoas} pessoas declaradas compram uma vez por semana. "
                        "É premissa sobre um público declarado, não medição."
                    ) if pessoas else "Sem público declarado na âncora, o impacto não tem base para ser estimado.",
                    "channel": None,
                    "channel_note": "",
                    "source": "âncora declarada",
                    "confidence": "media",
                    "confidence_note": "o dia vem do seu cadastro; o preço e o piso vêm da ficha técnica e do custo com quebra",
                })
            return sugestoes[:3]

        # Sem âncora: só o que a venda mostrar.
        if cobertura < 50:
            return []
        por_dia = vendas.get(escolhido["product_id"], {})
        if not por_dia:
            return []
        melhor_dia = max(por_dia, key=lambda indice: por_dia[indice])
        alvo = next((dia for dia in semana if dia["weekday"] == melhor_dia), semana[0])
        return [{
            "id": f"sport-medido-{escolhido['product_id']}-{alvo['date']}",
            "day": alvo["date"],
            "weekday": alvo["name"],
            "kind": "linha-sport",
            "title": f"Reforçar {escolhido['name']} na {alvo['name']}",
            "product": {"id": escolhido["product_id"], "name": escolhido["name"], "line": "Sport"},
            "mechanic": "Destaque da linha Sport no dia em que ela já responde",
            "price": {
                "from": escolhido["price"], "to": escolhido["promo_price"],
                "discount_percent": escolhido["promo_discount"],
            },
            "floor": {
                "cost_with_shrinkage": escolhido["cost"],
                "min_price": escolhido["floor_price"],
                "max_discount_percent": escolhido["max_discount"],
                "channel": None,
            },
            "evidence": (
                f"{escolhido['name']} vende em média {por_dia[melhor_dia]:g} unidades na {DIAS[melhor_dia]}, "
                "o melhor dia dele no período medido."
            ),
            "why_this_day": promessa,
            "breakeven": self._ponto_de_virada(por_dia[melhor_dia], escolhido),
            "margin_given": round(escolhido["contribution"] - escolhido["promo_contribution"], 2),
            "margin_given_monthly": None,
            "upside_monthly": None,
            "upside_note": "Consolidar o dia forte da linha custa menos que abrir um dia novo.",
            "channel": None,
            "channel_note": "",
            "source": "venda medida",
            "confidence": "media",
            "confidence_note": "linha nova tem amostra pequena por definição",
        }]

    @staticmethod
    def _promessa(economia: dict[str, Any]) -> str:
        """A comunicação da oferta sai da ficha, não da imaginação."""

        nutricao = economia.get("nutrition") or {}
        proteina = nutricao.get("protein_g")
        kcal = nutricao.get("kcal")
        claims = economia.get("claims") or []
        partes = []
        if proteina:
            partes.append(f"{proteina:g} g de proteína por porção")
        if kcal:
            partes.append(f"{kcal:g} kcal")
        if claims:
            partes.append(", ".join(claims[:2]))
        if not partes:
            return (
                "Cadastre proteína, calorias e as promessas do rótulo neste produto: sem isso a oferta "
                "esportiva não tem o que comunicar, e vira desconto genérico."
            )
        return "A oferta se comunica com o que a ficha sustenta: " + " · ".join(partes) + "."

    @staticmethod
    def _resumo_da_ancora(linha: dict[str, Any]) -> dict[str, Any]:
        dias = linha.get("weekdays") or []
        return {
            **linha,
            "weekday_names": [DIAS[indice] for indice in dias] or list(DIAS),
        }

    # ------------------------------------------------------------------

    def _blockers(self, pedidos, detalhados, cobertura, semana, produtos, ancoras) -> list[dict[str, str]]:
        bloqueios: list[dict[str, str]] = []

        # Vem primeiro de proposito: enquanto o dia estiver fechado, nenhuma
        # promocao nele significa coisa alguma.
        fechados = sorted(
            {dia["name"]: dia for dia in semana if dia["mostly_closed"] and dia["closed_upside"]}.values(),
            key=lambda dia: -(dia["closed_upside"] or 0),
        )
        for dia in fechados[:2]:
            bloqueios.append({
                "title": f"A loja abriu {dia['open_days']} {dia['name']}s e deixou de abrir {dia['closed_days']}",
                "description": (
                    f"Quando abre, {dia['name']} fatura {_money(dia['average_revenue'])} — praticamente a média da "
                    f"semana ({_money(dia['median_revenue'])}). Não é dia fraco, é dia fechado. As "
                    f"{dia['closed_days']} aberturas que faltaram valem cerca de {_money(dia['closed_upside'])} no "
                    "período, e nenhum desconto recupera isso: decidir abrir vem antes de decidir promover."
                ),
            })

        if pedidos and cobertura < 50:
            bloqueios.append({
                "title": f"{round(100 - cobertura, 1)}% dos pedidos não têm itens",
                "description": (
                    "Sem os itens vendidos não dá para saber qual produto puxa cada dia, e o produto da "
                    "promoção passa a ser escolhido pelo cadápio em vez de pela venda. Importe o relatório "
                    "de itens da Anota Aí para a sugestão passar a medir em vez de supor."
                ),
            })
        if not any(dia["reliable"] for dia in semana):
            bloqueios.append({
                "title": "Ainda não há semanas suficientes para separar dia forte de dia fraco",
                "description": (
                    f"Cada dia da semana precisa de pelo menos {AMOSTRAS_MINIMAS_POR_DIA} observações. "
                    "Antes disso, promover um dia seria apostar no acaso."
                ),
            })
        if not any((produto.get("line") == "Sport" and produto.get("active", True)) for produto in produtos.values()):
            bloqueios.append({
                "title": "A linha Sport não tem nenhum produto ativo",
                "description": (
                    "É a linha para onde a loja está indo, e ela não existe no cardápio. Use o simulador "
                    "em Cardápio para testar um item Sport: ele aceita insumo que a loja ainda não compra e "
                    "diz o preço para a margem que você quer, antes de qualquer compra."
                ),
            })
        elif not ancoras:
            bloqueios.append({
                "title": "Nenhuma âncora esportiva cadastrada",
                "description": (
                    "Academia, grupo de corrida ou time do bairro, com os dias em que treinam. O sistema não "
                    "tem como medir hábito de treino; com a âncora declarada, o calendário da semana passa a "
                    "se alinhar a ele em vez de a um palpite."
                ),
            })
        return bloqueios
