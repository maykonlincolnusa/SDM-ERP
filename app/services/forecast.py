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
            "purchase_plan": self._plano_de_compra(unidades_por_dia, nomes_produto, serie, pesos, tendencia, horizon),
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
            "note": f"Projeção para os próximos {horizonte} dias, já descontando o saldo atual do estoque.",
        }

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
