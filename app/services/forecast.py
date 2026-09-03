"""Previsao de demanda explicavel, feita para o gestor conferir e discordar.

Nada aqui e caixa-preta: a previsao de um dia e sempre o produto de tres
numeros que aparecem na tela — a media diaria, o peso daquele dia da semana e
a tendencia das ultimas semanas. O erro medido no proprio historico vai junto,
para o gestor saber de quanto e o chute.

Sem numpy nem sklearn de proposito: a conta e simples o bastante para caber em
Python puro, a imagem do container fica pequena, e cada passo pode ser
mostrado em portugues na interface.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any
import unicodedata

from app.services.delivery import DeliveryService
from app.services.erp import REVERSAL_PREFIX, SALE_REASON, WITHDRAWAL_KIND, ERPService

# O Brasil nao usa mais horario de verao desde 2019, entao o fuso da loja e um
# deslocamento fixo. Isso evita depender do pacote tzdata no Windows e mantem o
# resultado igual no container Linux.
FUSO_LOJA = timezone(timedelta(hours=-3))

DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

# Abaixo disso a conta ate roda, mas o numero engana mais do que ajuda.
DIAS_MINIMOS = 14
OBSERVACOES_MINIMAS_POR_DIA = 2


def _dia_local(momento: str) -> datetime:
    valor = datetime.fromisoformat(momento)
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(FUSO_LOJA)


class ForecastService:
    """Le os pedidos do ERP e devolve previsao com o raciocinio aberto."""

    def __init__(self, store: Any):
        self.store = store

    def forecast(self, horizon: int = 7) -> dict[str, Any]:
        horizon = max(1, min(horizon, 30))
        pedidos = [p for p in self.store.list("orders") if p.get("status") != "Cancelado"]

        receita_por_dia: dict[Any, float] = defaultdict(float)
        unidades_por_dia: dict[Any, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        nomes_produto: dict[str, str] = {}

        for pedido in pedidos:
            momento = _dia_local(pedido["created_at"])
            dia = momento.date()
            receita_por_dia[dia] += pedido["total"]
            for item in pedido.get("items", []):
                unidades_por_dia[dia][item["product_id"]] += item["quantity"]
                nomes_produto[item["product_id"]] = item["name"]

        if not receita_por_dia:
            return self._sem_dados("Nenhuma venda registrada ainda.", 0)

        # A serie precisa incluir os dias sem venda: domingo fechado tambem e
        # informacao, e ignora-lo inflaria a media diaria.
        primeiro, ultimo = min(receita_por_dia), max(receita_por_dia)
        serie: list[tuple[Any, float]] = []
        cursor = primeiro
        while cursor <= ultimo:
            serie.append((cursor, round(receita_por_dia.get(cursor, 0.0), 2)))
            cursor += timedelta(days=1)

        dias_cobertos = len(serie)
        if dias_cobertos < DIAS_MINIMOS:
            return self._sem_dados(
                f"O modelo precisa de pelo menos {DIAS_MINIMOS} dias de histórico para separar o efeito do dia da semana do acaso.",
                dias_cobertos,
            )

        media_diaria = fmean(valor for _, valor in serie)
        if media_diaria <= 0:
            return self._sem_dados("O histórico existe, mas não há faturamento nele.", dias_cobertos)

        # Decomposicao em duas passadas. O efeito do dia da semana e multiplicativo,
        # entao a oscilacao cresce junto com o faturamento: ajustar a reta direto na
        # serie crua faz os sabados grandes do fim puxarem a inclinacao para cima.
        # Passada 1: peso aproximado de cada dia, so para poder remove-lo.
        # Passada 2: tendencia sobre a serie ja sem sazonalidade, e entao o peso
        # definitivo sobre a serie ja sem tendencia.
        # Uma passada nao basta: cada estimativa depende da outra, e cada dia da
        # semana cai, em media, num ponto diferente da serie, o que enviesa quem
        # estiver mal ancorado. Tres rodadas ja estabilizam.
        centro = (dias_cobertos - 1) / 2
        fatores = self._pesos_brutos(serie, media_diaria)
        for _ in range(3):
            sem_sazonal = [(dia, valor / fatores[dia.weekday()]) for dia, valor in serie]
            tendencia = self._tendencia(sem_sazonal, media_diaria)
            pesos = self._pesos_por_dia_da_semana(serie, media_diaria, tendencia, centro)
            fatores = {p["weekday"]: (p["factor"] if p["factor"] > 0.05 else 1.0) for p in pesos}
        erro = self._erro(serie, media_diaria, pesos, tendencia, centro)

        return {
            "ready": True,
            "history_days": dias_cobertos,
            "first_day": primeiro.isoformat(),
            "last_day": ultimo.isoformat(),
            "orders": len(pedidos),
            "baseline": round(media_diaria, 2),
            "weekday_effect": pesos,
            "trend": tendencia,
            "accuracy": erro,
            "daily_forecast": self._projetar(ultimo, horizon, media_diaria, pesos, tendencia, centro, dias_cobertos - 1),
            "purchase_plan": self._plano_de_compra(unidades_por_dia, nomes_produto, serie, pesos, tendencia, horizon, pedidos),
            "product_trend": self._tendencia_por_produto(unidades_por_dia, nomes_produto, serie),
            "explanation": {
                "formula": "previsão do dia = média diária × peso do dia da semana × tendência",
                "baseline_note": f"A média diária de {self._reais(media_diaria)} considera também os dias sem venda dentro do período.",
                "trend_note": tendencia["descricao"],
                "accuracy_note": erro["descricao"],
            },
        }

    # ------------------------------------------------------------------ partes

    @staticmethod
    def _pesos_brutos(serie: list[tuple[Any, float]], media: float) -> dict[int, float]:
        """Peso aproximado de cada dia, usado so para remover a sazonalidade."""

        agrupado: dict[int, list[float]] = defaultdict(list)
        for dia, valor in serie:
            agrupado[dia.weekday()].append(valor)
        brutos = {}
        for indice in range(7):
            valores = agrupado.get(indice, [])
            fator = (fmean(valores) / media) if (valores and media) else 1.0
            # Dia sem movimento nenhum dividiria por zero na hora de remover.
            brutos[indice] = fator if fator > 0.05 else 1.0
        return brutos

    def _pesos_por_dia_da_semana(self, serie: list[tuple[Any, float]], media: float, tendencia: dict[str, Any], centro: float) -> list[dict[str, Any]]:
        agrupado: dict[int, list[float]] = defaultdict(list)
        for posicao, (dia, valor) in enumerate(serie):
            nivel = self._nivel(media, tendencia, posicao - centro)
            # Traz cada dia para o nivel medio da serie antes de comparar. Sem
            # isso, um sabado do fim de uma serie crescente pareceria mais forte
            # do que e, so por ter acontecido depois.
            agrupado[dia.weekday()].append(valor * media / nivel if nivel else valor)

        pesos = []
        for indice in range(7):
            valores = agrupado.get(indice, [])
            # Um dia visto uma vez so nao tem peso proprio: vale a media geral,
            # senao uma unica sexta atipica viraria regra.
            confiavel = len(valores) >= OBSERVACOES_MINIMAS_POR_DIA
            media_dia = fmean(valores) if valores else media
            fator = (media_dia / media) if (confiavel and media) else 1.0
            pesos.append({
                "weekday": indice,
                "name": DIAS[indice],
                "average": round(media_dia, 2),
                "factor": round(fator, 3),
                "samples": len(valores),
                "reliable": confiavel,
                "reading": self._leitura_do_peso(DIAS[indice], fator, confiavel, len(valores)),
            })
        return pesos

    @staticmethod
    def _leitura_do_peso(nome: str, fator: float, confiavel: bool, amostras: int) -> str:
        if not confiavel:
            return f"{nome.capitalize()}: só {amostras} registro(s) no período, ainda sem peso próprio."
        diferenca = (fator - 1) * 100
        if abs(diferenca) < 8:
            return f"{nome.capitalize()} fica na média da semana."
        lado = "acima" if diferenca > 0 else "abaixo"
        return f"{nome.capitalize()} vende {abs(diferenca):.0f}% {lado} da média."

    def _tendencia(self, serie: list[tuple[Any, float]], media: float) -> dict[str, Any]:
        """Regressao linear simples sobre o dia: inclinacao em reais por dia."""

        n = len(serie)
        xs = list(range(n))
        ys = [valor for _, valor in serie]
        media_x = fmean(xs)
        media_y = fmean(ys)
        denominador = sum((x - media_x) ** 2 for x in xs)
        inclinacao = (sum((x - media_x) * (y - media_y) for x, y in zip(xs, ys)) / denominador) if denominador else 0.0
        por_semana = inclinacao * 7
        percentual = (por_semana / media * 100) if media else 0.0

        if abs(percentual) < 3:
            descricao = "O faturamento está estável: a variação semanal é pequena demais para ser considerada tendência."
        else:
            direcao = "crescendo" if percentual > 0 else "caindo"
            descricao = f"O faturamento vem {direcao} cerca de {abs(percentual):.0f}% por semana ({self._reais(abs(por_semana))} por semana)."

        return {
            "slope_per_day": round(inclinacao, 2),
            "slope_per_week": round(por_semana, 2),
            "percent_per_week": round(percentual, 1),
            "descricao": descricao,
        }

    @staticmethod
    def _nivel(media: float, tendencia: dict[str, Any], desde_o_centro: float) -> float:
        """Nivel esperado do dia, sem o efeito do dia da semana.

        A reta foi ajustada em torno do centro da serie, entao o nivel de
        qualquer dia se mede a partir dali. Ancorar na media do periodo inteiro
        subestimaria o futuro sempre que houver crescimento.
        """

        nivel = media + tendencia["slope_per_day"] * desde_o_centro
        # Trava de sanidade: serie curta e inclinada projeta numero absurdo para
        # a frente, e numero absurdo destroi a confianca na tela inteira.
        return max(media * 0.3, min(nivel, media * 2.5))

    def _prever(self, media: float, pesos: list[dict[str, Any]], tendencia: dict[str, Any], dia: Any, desde_o_centro: float) -> float:
        peso = pesos[dia.weekday()]["factor"]
        return round(self._nivel(media, tendencia, desde_o_centro) * peso, 2)

    def _projetar(self, ultimo_dia: Any, horizonte: int, media: float, pesos: list[dict[str, Any]], tendencia: dict[str, Any], centro: float, ultimo_indice: int) -> list[dict[str, Any]]:
        projecao = []
        for passo in range(1, horizonte + 1):
            dia = ultimo_dia + timedelta(days=passo)
            peso = pesos[dia.weekday()]
            projecao.append({
                "date": dia.isoformat(),
                "weekday": peso["name"],
                "value": self._prever(media, pesos, tendencia, dia, ultimo_indice + passo - centro),
                "factor": peso["factor"],
                "reliable": peso["reliable"],
            })
        return projecao

    def _erro(self, serie: list[tuple[Any, float]], media: float, pesos: list[dict[str, Any]], tendencia: dict[str, Any], centro: float) -> dict[str, Any]:
        """Compara a previsao com o que de fato aconteceu, no proprio historico."""

        erros = []
        for posicao, (dia, real) in enumerate(serie):
            previsto = self._prever(media, pesos, tendencia, dia, posicao - centro)
            erros.append(abs(previsto - real))
        erro_medio = fmean(erros) if erros else 0.0
        percentual = (erro_medio / media * 100) if media else 0.0
        return {
            "mean_absolute_error": round(erro_medio, 2),
            "percent": round(percentual, 1),
            "descricao": (
                f"Testado contra o próprio histórico, o modelo erra em média {self._reais(erro_medio)} por dia "
                f"({percentual:.0f}% do movimento diário). Use a previsão como ordem de grandeza, não como promessa."
            ),
        }

    def _plano_de_compra(
        self,
        unidades: dict[Any, dict[str, float]],
        nomes: dict[str, str],
        serie: list[tuple[Any, float]],
        pesos: list[dict[str, Any]],
        tendencia: dict[str, Any],
        horizonte: int,
        pedidos: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Converte a previsao de venda em insumo, pela ficha tecnica ja cadastrada."""

        dias = len(serie)
        total_por_produto: dict[str, float] = defaultdict(float)
        for por_produto in unidades.values():
            for produto_id, quantidade in por_produto.items():
                total_por_produto[produto_id] += quantidade

        if not total_por_produto:
            return {"available": False, "reason": "Os pedidos importados não trazem os itens vendidos, então não dá para converter em insumo.", "items": [], "products": []}

        # Peso medio dos proximos dias: prever produto a produto por dia da semana
        # exigiria muito mais historico do que a loja tem.
        peso_horizonte = sum(pesos[(serie[-1][0] + timedelta(days=p)).weekday()]["factor"] for p in range(1, horizonte + 1))
        # Cresce o volume na mesma proporcao que a receita projetada no meio do
        # horizonte, medida a partir do centro da serie como na regressao.
        media_serie = fmean(v for _, v in serie)
        centro = (dias - 1) / 2
        meio = (dias - 1) + horizonte / 2 - centro
        crescimento = (self._nivel(media_serie, tendencia, meio) / media_serie) if media_serie else 1.0

        produtos = self.store.list("products")
        receitas = {p["id"]: p.get("recipe", []) for p in produtos}
        estoque = {i["id"]: i for i in self.store.list("inventory")}
        # Quantas porcoes de cada complemento saem por unidade vendida. Sem
        # isso a previsao pede polpa e copo e esquece banana e calda, que sao
        # justamente os insumos que somem primeiro num sabado cheio.
        complementos = self._complementos_por_produto(pedidos)
        cadastro_complementos = {addon["id"]: addon for addon in self.store.list("addons")}

        previsao_produto = []
        necessidade: dict[str, float] = defaultdict(float)
        for produto_id, unidades_totais in sorted(total_por_produto.items(), key=lambda kv: kv[1], reverse=True):
            por_dia = unidades_totais / dias
            previsto = por_dia * peso_horizonte * crescimento
            previsao_produto.append({
                "product_id": produto_id,
                "name": nomes.get(produto_id, produto_id),
                "sold": round(unidades_totais, 2),
                "daily_average": round(por_dia, 2),
                "forecast_units": round(previsto, 1),
            })
            for linha in receitas.get(produto_id, []):
                necessidade[linha["inventory_id"]] += linha["quantity"] * previsto
            for addon_id, taxa in complementos.get(produto_id, {}).items():
                addon = cadastro_complementos.get(addon_id)
                if addon:
                    necessidade[addon["inventory_id"]] += addon["quantity"] * taxa * previsto

        itens = []
        for inventory_id, quantidade in sorted(necessidade.items(), key=lambda kv: kv[1], reverse=True):
            item = estoque.get(inventory_id)
            if not item:
                continue
            saldo = item["quantity"]
            comprar = max(0.0, quantidade - saldo)
            itens.append({
                "inventory_id": inventory_id,
                "name": item["name"],
                "unit": item["unit"],
                "needed": round(quantidade, 3),
                "in_stock": round(saldo, 3),
                "to_buy": round(comprar, 3),
                "cost": round(comprar * item["unit_cost"], 2),
            })

        return {
            "available": True,
            "horizon_days": horizonte,
            "products": previsao_produto[:8],
            "items": itens,
            "total_cost": round(sum(i["cost"] for i in itens), 2),
            # A compra sugerida responde "quanto preciso repor". O resultado
            # operacional usa outra conta: custo de todo o insumo que sera
            # consumido, inclusive o que ja esta no estoque hoje.
            "operating_projection": self._projecao_operacional(
                previsao_produto, produtos, estoque, pedidos, horizonte, dias,
                complementos, cadastro_complementos,
            ),
            "note": f"Projeção para os próximos {horizonte} dias, já descontando o saldo atual do estoque.",
        }

    @staticmethod
    def _complementos_por_produto(pedidos: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        """Porcoes de cada complemento por unidade vendida do produto.

        E a taxa de adesao observada: se em 100 copos de 500 ml sairam 62
        porcoes de banana, cada copo previsto carrega 0,62 porcao. Projetar por
        taxa, e nao por "quantas bananas sairam por dia", faz o complemento
        crescer junto com o produto que o carrega, que e como ele se comporta.
        """

        unidades: dict[str, float] = defaultdict(float)
        porcoes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for pedido in pedidos:
            for item in pedido.get("items", []):
                unidades[item["product_id"]] += item["quantity"]
                for escolhido in item.get("addons", []):
                    porcoes[item["product_id"]][escolhido["addon_id"]] += escolhido["quantity"] * item["quantity"]
        return {
            produto: {addon: total / unidades[produto] for addon, total in mapa.items()}
            for produto, mapa in porcoes.items()
            if unidades.get(produto)
        }

    def _projecao_operacional(
        self,
        previsao_produto: list[dict[str, Any]],
        produtos: list[dict[str, Any]],
        estoque: dict[str, dict[str, Any]],
        pedidos: list[dict[str, Any]],
        horizonte: int,
        dias_historico: int,
        complementos: dict[str, dict[str, float]] | None = None,
        cadastro_complementos: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Traduz o mix previsto em uma DRE futura, sem chutar itens ausentes.

        A previsao de demanda ja sabe quantas unidades de cada produto devem
        sair. Aqui cada unidade percorre a ficha tecnica: 500 ml com banana e
        calda, por exemplo, baixa polpa, banana e calda nas quantidades da
        receita. Assim o custo nao e uma porcentagem generica do faturamento.
        """

        receita_historica = sum(float(pedido.get("total", 0)) for pedido in pedidos)
        pedidos_com_itens = [pedido for pedido in pedidos if pedido.get("items")]
        receita_com_itens = sum(float(pedido.get("total", 0)) for pedido in pedidos_com_itens)
        cobertura = round(receita_com_itens / receita_historica * 100, 1) if receita_historica else 100.0
        if receita_com_itens < receita_historica:
            faltante = receita_historica - receita_com_itens
            return {
                "available": False,
                "coverage": cobertura,
                "reason": (
                    f"{self._reais(faltante)} das vendas historicas nao tem os itens vendidos. "
                    "Importe ou registre os itens da Anota AI antes de projetar o lucro sem lacunas."
                ),
                "products": [],
            }

        produtos_por_id = {produto["id"]: produto for produto in produtos}
        complementos = complementos or {}
        cadastro_complementos = cadastro_complementos or {}
        linhas: list[dict[str, Any]] = []
        sem_receita: list[str] = []
        for previsto in previsao_produto:
            produto = produtos_por_id.get(previsto["product_id"])
            if not produto or not produto.get("recipe"):
                sem_receita.append(previsto["name"])
                continue
            custo_unitario = sum(
                estoque.get(item["inventory_id"], {}).get("unit_cost", 0) * item["quantity"]
                for item in produto["recipe"]
            )
            unidades = previsto["forecast_units"]
            # O complemento entra pelas duas pontas: cobra do cliente e come
            # insumo. Deixar so o custo subestimaria a margem; so a receita, o
            # contrario. Preco e custo sao os de hoje, como no resto da conta.
            receita_complementos = custo_complementos = 0.0
            for addon_id, taxa in complementos.get(produto["id"], {}).items():
                addon = cadastro_complementos.get(addon_id)
                if not addon:
                    continue
                porcoes = taxa * unidades
                receita_complementos += addon["price"] * porcoes
                custo_complementos += estoque.get(addon["inventory_id"], {}).get("unit_cost", 0) * addon["quantity"] * porcoes
            receita = produto["price"] * unidades + receita_complementos
            custo = custo_unitario * unidades + custo_complementos
            linhas.append({
                "product_id": produto["id"], "name": produto["name"],
                "units": round(unidades, 1), "revenue": round(receita, 2),
                "addons_revenue": round(receita_complementos, 2),
                "addons_cost": round(custo_complementos, 2),
                "cost": round(custo, 2), "contribution": round(receita - custo, 2),
            })

        if sem_receita:
            return {
                "available": False,
                "coverage": cobertura,
                "reason": (
                    "Cadastre a ficha tecnica de " + ", ".join(sem_receita[:3]) +
                    " para cruzar essas vendas com polpa, frutas, caldas e os demais insumos."
                ),
                "products": linhas,
            }

        # Vendas de produto nao incluem o frete. O frete e projetado pela media
        # dos pedidos de entrega e o pagamento do entregador pela media dos
        # fechamentos, para cada um entrar uma unica vez na equacao.
        frete_historico = sum(float(pedido.get("delivery_fee") or 0) for pedido in pedidos)
        entrega = DeliveryService(self.store).performance(dias_historico)["totals"]
        frete_previsto = frete_historico / dias_historico * horizonte
        custo_entrega = entrega["payout"] / dias_historico * horizonte
        tem_entrega_sem_fechamento = frete_historico > 0 and entrega["payout"] == 0

        canais = {canal["name"]: canal for canal in self.store.list("channels")}
        taxas_historicas = 0.0
        taxa_estimativa = False
        for pedido in pedidos:
            taxa = pedido.get("channel_fee")
            if taxa is None:
                canal = canais.get(pedido.get("channel"), {})
                taxa = pedido["total"] * (canal.get("fee_percent", 0) / 100) + canal.get("fee_fixed", 0)
                taxa_estimativa = True
            taxas_historicas += float(taxa)
        taxa_media = taxas_historicas / receita_historica if receita_historica else 0.0

        # Despesa fixa e a unica coisa desta conta que nao precisa ser
        # estimada: o aluguel do mes que vem esta escrito no contrato. Ela vem
        # da regra cadastrada, e so o que sobra — a despesa avulsa — segue a
        # media do historico.
        custo_fixo_mensal = ERPService(self.store).recurring_monthly_cost()["total"]
        despesa_fixa = custo_fixo_mensal / 30 * horizonte
        despesas_variaveis = self._despesas_variaveis(dias_historico)
        receita_produtos = sum(linha["revenue"] for linha in linhas)
        custo_produtos = sum(linha["cost"] for linha in linhas)
        receita_total = receita_produtos + frete_previsto
        taxas_canais = receita_total * taxa_media
        # O insumo que a equipe retira e o que se perde saem do estoque todo
        # dia e nao viram faturamento. Fora desta conta, o lucro previsto seria
        # otimista exatamente no valor do que a loja consome sem vender — que e
        # o custo que o registro de retirada existe para tornar visivel.
        saidas = self._saidas_sem_venda(dias_historico)
        retirada_prevista = saidas["withdrawal"] / dias_historico * horizonte
        perda_prevista = saidas["loss"] / dias_historico * horizonte
        despesas = despesa_fixa + despesas_variaveis / dias_historico * horizonte
        lucro = (
            receita_total - custo_produtos - taxas_canais - custo_entrega
            - retirada_prevista - perda_prevista - despesas
        )
        avisos: list[str] = []
        if taxa_estimativa:
            avisos.append("Parte da taxa de canal usa a tabela atual, pois alguns pedidos antigos nao guardaram a taxa do dia.")
        if tem_entrega_sem_fechamento:
            avisos.append("Ha frete em pedidos, mas nenhum fechamento de entregador no historico; o custo projetado de entrega ficou zerado.")
        sem_venda = retirada_prevista + perda_prevista
        if sem_venda and receita_total and sem_venda / receita_total >= 0.03:
            avisos.append(
                f"{self._reais(sem_venda)} do custo previsto e insumo que sai sem virar venda "
                f"({self._reais(retirada_prevista)} em retirada da equipe e {self._reais(perda_prevista)} em perda), "
                "projetado pela media do historico."
            )
        if not custo_fixo_mensal:
            avisos.append(
                "Nenhuma despesa fixa cadastrada como recorrente. Aluguel, energia e equipe entram na projecao "
                "pela media do historico, que muda de valor conforme a janela escolhida."
            )

        return {
            "available": True,
            "coverage": cobertura,
            "horizon_days": horizonte,
            "products": sorted(linhas, key=lambda linha: linha["revenue"], reverse=True),
            "warnings": avisos,
            "note": (
                "Preco e ficha tecnica atuais x unidades previstas, com os complementos na taxa de adesao observada; "
                "despesas, frete e custos de entrega seguem a media do historico usado."
            ),
            "totals": {
                "product_revenue": round(receita_produtos, 2),
                "addons_revenue": round(sum(linha["addons_revenue"] for linha in linhas), 2),
                "addons_cost": round(sum(linha["addons_cost"] for linha in linhas), 2),
                "freight_revenue": round(frete_previsto, 2),
                "revenue": round(receita_total, 2),
                "cost_of_goods": round(custo_produtos, 2),
                "channel_fees": round(taxas_canais, 2),
                "delivery_cost": round(custo_entrega, 2),
                # Separadas uma da outra pelo mesmo motivo do resultado
                # realizado: retirada e consumo da operacao e tende a se
                # repetir; perda e quebra e deveria cair.
                "withdrawal_cost": round(retirada_prevista, 2),
                "loss_cost": round(perda_prevista, 2),
                "operating_expenses": round(despesas, 2),
                # Separadas porque tem confiabilidade diferente: a fixa vem do
                # contrato, a variavel vem de media.
                "fixed_expenses": round(despesa_fixa, 2),
                "variable_expenses": round(despesas - despesa_fixa, 2),
                "fixed_monthly_cost": round(custo_fixo_mensal, 2),
                "operating_profit": round(lucro, 2),
                "operating_margin": round(lucro / receita_total * 100, 1) if receita_total else 0.0,
            },
        }

    def _saidas_sem_venda(self, dias: int) -> dict[str, float]:
        """Custo do insumo que saiu do estoque sem virar venda, no periodo.

        Retirada e perda vem separadas porque significam coisas diferentes: a
        retirada e consumo da operacao e tende a se repetir no mesmo tamanho; a
        perda e quebra, e o dono precisa ve-la caindo. Somar as duas num
        "outros custos" apagaria justamente essa leitura.
        """

        inicio = datetime.now(FUSO_LOJA).date() - timedelta(days=dias - 1)
        estoque = {item["id"]: item for item in self.store.list("inventory")}
        total = {"withdrawal": 0.0, "loss": 0.0}
        for movimentacao in self.store.list("inventory_movements"):
            if movimentacao["quantity_delta"] >= 0:
                continue
            motivo = movimentacao.get("reason", "")
            if motivo == SALE_REASON or motivo.startswith(REVERSAL_PREFIX):
                continue
            if _dia_local(movimentacao["created_at"]).date() < inicio:
                continue
            item = estoque.get(movimentacao["inventory_id"])
            if not item:
                continue
            # O custo gravado no movimento manda: ele guarda o preco do dia da
            # saida, e o custo medio de hoje ja mudou com a compra seguinte.
            custo = movimentacao.get(
                "total_cost", abs(movimentacao["quantity_delta"]) * item.get("unit_cost", 0)
            )
            chave = "withdrawal" if movimentacao.get("kind") == WITHDRAWAL_KIND else "loss"
            total[chave] += custo
        return total

    def _despesas_variaveis(self, dias: int) -> float:
        """Despesa que nao se repete todo mes, e por isso so pode vir da media.

        A fixa sai fora daqui porque ja entra na projecao pelo valor do
        contrato. Somar as duas contaria o aluguel duas vezes; usar so a media
        faria o custo fixo mudar de tamanho conforme a janela escolhida.
        Despesa de insumo tambem fica fora: ela ja esta no custo da receita.
        """

        inicio = datetime.now(FUSO_LOJA).date() - timedelta(days=dias - 1)
        fim = datetime.now(FUSO_LOJA).date()
        total = 0.0
        for despesa in self.store.list("expenses"):
            if despesa.get("series_id"):
                continue
            try:
                vencimento = datetime.fromisoformat(str(despesa["due_date"])[:10]).date()
            except (KeyError, TypeError, ValueError):
                continue
            if not inicio <= vencimento <= fim:
                continue
            categoria = self._texto_normalizado(despesa.get("category", ""))
            if "insumo" in categoria or "compra" in categoria:
                continue
            total += float(despesa.get("amount", 0))
        return total

    @staticmethod
    def _texto_normalizado(valor: Any) -> str:
        return unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode("ascii").lower()

    def _tendencia_por_produto(self, unidades: dict[Any, dict[str, float]], nomes: dict[str, str], serie: list[tuple[Any, float]]) -> list[dict[str, Any]]:
        """Compara a primeira metade do periodo com a segunda, produto a produto."""

        if len(serie) < DIAS_MINIMOS or not unidades:
            return []
        corte = serie[len(serie) // 2][0]
        antes: dict[str, float] = defaultdict(float)
        depois: dict[str, float] = defaultdict(float)
        for dia, por_produto in unidades.items():
            destino = depois if dia >= corte else antes
            for produto_id, quantidade in por_produto.items():
                destino[produto_id] += quantidade

        linhas = []
        for produto_id in set(antes) | set(depois):
            inicio, fim = antes.get(produto_id, 0.0), depois.get(produto_id, 0.0)
            if inicio + fim < 4:  # volume baixo demais para leitura honesta
                continue
            variacao = ((fim - inicio) / inicio * 100) if inicio else 100.0
            if abs(variacao) < 15:
                leitura = "estável"
            else:
                leitura = "crescendo" if variacao > 0 else "caindo"
            linhas.append({
                "product_id": produto_id,
                "name": nomes.get(produto_id, produto_id),
                "before": round(inicio, 1),
                "after": round(fim, 1),
                "change_percent": round(variacao, 1),
                "reading": leitura,
            })
        linhas.sort(key=lambda linha: linha["change_percent"], reverse=True)
        return linhas

    @staticmethod
    def _sem_dados(motivo: str, dias: int) -> dict[str, Any]:
        return {
            "ready": False,
            "reason": motivo,
            "history_days": dias,
            "required_days": DIAS_MINIMOS,
            "weekday_effect": [],
            "daily_forecast": [],
            "product_trend": [],
            "purchase_plan": {"available": False, "items": [], "products": []},
        }

    @staticmethod
    def _reais(valor: float) -> str:
        return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
