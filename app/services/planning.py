"""Planejamento de producao e compra.

A previsao existente responde "quanto vou faturar". Esta camada responde as
duas perguntas que a loja faz de manha e na hora de ligar para o fornecedor:
**quanto bater hoje** e **quando pedir mais**. Sao perguntas diferentes, e a
resposta certa nao e a media.

Bater a media significa faltar em metade dos dias. Para um produto perecivel, o
custo de faltar (a margem que nao aconteceu) quase nunca e igual ao custo de
sobrar (o insumo que venceu). Quem decide o ponto e a razao entre os dois — o
modelo do jornaleiro — e nao a intuicao de "prepara um pouco a mais".

Por que nada aqui ajusta parametro
----------------------------------

Com ~8 pedidos por dia e alguns meses de historico, sobram poucas observacoes
por dia da semana. Ajustar uma distribuicao normal, ou qualquer curva com
parametro, produz numero com quatro casas decimais e nenhuma sustentacao: o
modelo decora o ruido da amostra e erra feio no primeiro dia atipico.

As tres escolhas que evitam isso:

1. **Quantil empirico**, nao distribuicao ajustada. O percentil sai das proprias
   observacoes ordenadas. Se o historico nunca viu um dia acima de X, o plano
   nao inventa um.
2. **Encolhimento do fator do dia da semana.** O peso de cada dia e puxado de
   volta para 1 conforme a amostra e pequena — `n / (n + k)`. Uma terca vista
   tres vezes quase nao move o plano; vista vinte, move quase tudo. Sem isso,
   um sabado atipico vira regra permanente.
3. **Convolucao empirica para o prazo de entrega**, nao aproximacao normal. A
   demanda de tres dias sai das somas moveis observadas de tres dias. Nao ha
   suposicao de normalidade para um prazo curto, onde ela e justamente mais
   fraca.

Dia sem nenhum movimento e tratado como loja fechada e sai da amostra. Contar
domingo fechado como "demanda zero" rebaixaria todo quantil e faria o plano
sugerir menos do que a loja precisa.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean, pstdev
from typing import Any

from app.repositories.store import Store
from app.services.erp import (
    FUSO_LOJA,
    PRODUCTION_KIND,
    REVERSAL_PREFIX,
    SALE_REASON,
    WITHDRAWAL_KIND,
    ERPService,
)

DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

# Constante de encolhimento do fator do dia da semana. Com k=4, uma amostra de
# 4 observacoes vale metade do caminho ate o fator bruto; com 16, vale 80%.
# Valor escolhido para ser conservador no comeco da serie sem travar o fator
# quando o historico cresce.
K_ENCOLHIMENTO = 4.0

# Abaixo disso o insumo nao tem serie propria para planejar sem chutar.
MINIMO_OBSERVACOES = 8

# Curva ABC: fatia acumulada do custo consumido.
CORTE_A = 80.0
CORTE_B = 95.0

# XYZ pelo coeficiente de variacao da demanda diaria.
CORTE_X = 0.5
CORTE_Y = 1.0


class PlanningService:
    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------

    def plan(self, days: int = 90, horizon: int = 7, service_level: float = 0.9) -> dict[str, Any]:
        days = max(14, min(days, 365))
        horizon = max(1, min(horizon, 30))
        service_level = min(max(service_level, 0.5), 0.99)

        estoque = {item["id"]: item for item in self.store.list("inventory")}
        series, dias_abertos = self._series(days, estoque)
        amanha = datetime.now(FUSO_LOJA).date() + timedelta(days=1)

        producao = []
        compra = []
        for item_id, serie in series.items():
            item = estoque[item_id]
            valores = [quantidade for _, quantidade in serie]
            if len(valores) < MINIMO_OBSERVACOES:
                continue
            fator, amostras = self._fator_do_dia(serie, amanha.weekday())

            if item.get("shelf_life_days"):
                producao.append(self._producao(item, valores, fator, amostras, amanha))
            compra.append(self._compra(item, valores, horizon, service_level))

        producao.sort(key=lambda row: row["cost"], reverse=True)
        compra.sort(key=lambda row: (not row["reorder_now"], -row["cost"]))

        return {
            "period_days": days,
            "horizon": horizon,
            "service_level": round(service_level * 100, 1),
            "open_days": len(dias_abertos),
            "target_day": amanha.isoformat(),
            "target_weekday": DIAS[amanha.weekday()],
            "production": producao,
            "production_total": round(sum(row["cost"] for row in producao), 2),
            "purchase": compra,
            "purchase_total": round(sum(row["cost"] for row in compra if row["reorder_now"]), 2),
            "abc": self._abc(series, estoque, days),
            "method": self._method(days, len(dias_abertos), service_level),
            "blockers": self._blockers(series, estoque, dias_abertos),
        }

    # ------------------------------------------------------------------
    # Producao do dia: modelo do jornaleiro
    # ------------------------------------------------------------------

    def _producao(self, item: dict[str, Any], valores: list[float], fator: float, amostras: int, dia: date) -> dict[str, Any]:
        """Quanto preparar, pela razao entre o custo de faltar e o de sobrar.

        Custo de faltar (Cu) e a margem de contribuicao que a venda perdida
        deixaria; custo de sobrar (Co) e o insumo que vence. A razao critica
        Cu/(Cu+Co) e o percentil da demanda em que vale a pena parar — e nao
        uma escolha de gosto sobre "quanto a mais preparar".
        """

        custo_unitario = item["unit_cost"]
        margem = self._margem_por_unidade(item["id"], custo_unitario)
        # Sobrar nao custa o insumo inteiro quando ele dura mais de um dia: o
        # que sobra hoje ainda vende amanha. O custo de sobra e a parcela que
        # de fato corre risco de vencer, aproximada pelo inverso da validade.
        # Com validade de 1 dia (prepara e descarta no fechamento) ela e o custo
        # cheio; com 3 dias, um terco. E aproximacao, e o texto diz isso.
        validade = max(int(item.get("shelf_life_days") or 1), 1)
        custo_sobra = custo_unitario / validade
        if margem is None or margem <= 0:
            razao = 0.5
            base = "sem margem medida, o plano usa a mediana"
        else:
            razao = margem / (margem + custo_sobra) if (margem + custo_sobra) else 0.5
            base = (
                f"faltar custa {self._money(margem)} por {item['unit']} de margem perdida; "
                f"sobrar custa {self._money(custo_sobra)}, que é o {self._money(custo_unitario)} "
                f"do insumo diluído nos {validade} dia(s) de validade"
            )

        quantidade = self._quantil(valores, razao) * fator
        mediana = self._quantil(valores, 0.5) * fator
        return {
            "inventory_id": item["id"],
            "name": item["name"],
            "unit": item["unit"],
            "prepare": round(quantidade, 3),
            "median": round(mediana, 3),
            "in_stock": item["quantity"],
            # O que falta preparar depois de contar o que ja esta pronto.
            "to_prepare": round(max(quantidade - item["quantity"], 0), 3),
            "cost": round(max(quantidade - item["quantity"], 0) * custo_unitario, 2),
            "critical_ratio": round(razao * 100, 1),
            "ratio_basis": base,
            "weekday_factor": round(fator, 3),
            "weekday_samples": amostras,
            "shelf_life_days": item.get("shelf_life_days"),
            "observations": len(valores),
            "reading": self._leitura_producao(razao, fator, amostras, DIAS[dia.weekday()]),
        }

    @staticmethod
    def _leitura_producao(razao: float, fator: float, amostras: int, dia: str) -> str:
        if amostras < 3:
            return f"Só {amostras} {dia}(s) no histórico: o peso do dia mal influencia este número ainda."
        if fator >= 1.15:
            return f"{dia.capitalize()} puxa {round((fator - 1) * 100)}% acima da média, e o plano já considera isso."
        if fator <= 0.85:
            return f"{dia.capitalize()} costuma vender {round((1 - fator) * 100)}% abaixo da média — preparar menos evita a perda."
        return f"{dia.capitalize()} fica na média da semana; o plano cobre {round(razao * 100)}% dos dias."

    def _margem_por_unidade(self, inventory_id: str, custo_unitario: float) -> float | None:
        """Margem de contribuicao por unidade deste insumo.

        Um quilo de acai vira varios copos de tamanhos diferentes. A margem
        relevante e a que aquele quilo gera, ponderada pelo que a loja de fato
        vende — nao a margem do produto mais caro que o usa.
        """

        inventario = {row["id"]: row for row in self.store.list("inventory")}
        giro = self._giro_por_produto()
        peso_total = 0.0
        margem_total = 0.0
        for produto in self.store.list("products"):
            if not produto.get("active", True):
                continue
            linha = next((l for l in produto.get("recipe", []) if l["inventory_id"] == inventory_id), None)
            if not linha or not linha["quantity"]:
                continue
            custo = sum(
                inventario.get(l["inventory_id"], {}).get("unit_cost", 0) * l["quantity"]
                for l in produto["recipe"]
            )
            contribuicao = produto["price"] - custo
            # Margem por unidade do insumo: quanto aquele quilo rende de sobra.
            por_unidade = contribuicao / linha["quantity"]
            peso = giro.get(produto["id"], 0) or 1
            margem_total += por_unidade * peso
            peso_total += peso
        return margem_total / peso_total if peso_total else None

    def _giro_por_produto(self) -> dict[str, float]:
        giro: dict[str, float] = defaultdict(float)
        for order in self.store.list("orders"):
            if order.get("status") == "Cancelado":
                continue
            for item in order.get("items", []):
                giro[item["product_id"]] += item["quantity"]
        return giro

    # ------------------------------------------------------------------
    # Compra: ponto de pedido por convolucao empirica
    # ------------------------------------------------------------------

    def _compra(self, item: dict[str, Any], valores: list[float], horizon: int, service_level: float) -> dict[str, Any]:
        """Quando pedir e quanto, sem supor normalidade.

        A demanda ate a mercadoria chegar sai das somas moveis observadas
        daquele numero de dias. Para prazo curto a aproximacao normal e
        justamente onde mais erra, e aqui nao e necessaria: as somas ja estao
        no historico.
        """

        prazo = int(item.get("lead_time_days") or 2)
        demanda_prazo = self._somas_moveis(valores, prazo)
        ponto = self._quantil(demanda_prazo, service_level) if demanda_prazo else fmean(valores) * prazo
        media_prazo = fmean(demanda_prazo) if demanda_prazo else fmean(valores) * prazo
        # Estoque de seguranca e o que separa o ponto de pedido da demanda media
        # no prazo: e o preco de nao faltar nos dias ruins.
        seguranca = max(ponto - media_prazo, 0)

        cobertura = self._somas_moveis(valores, horizon)
        necessidade = self._quantil(cobertura, service_level) if cobertura else fmean(valores) * horizon
        comprar = max(necessidade + seguranca - item["quantity"], 0)

        media_diaria = fmean(valores)
        return {
            "inventory_id": item["id"],
            "name": item["name"],
            "unit": item["unit"],
            "in_stock": item["quantity"],
            "daily_average": round(media_diaria, 3),
            "lead_time_days": prazo,
            "reorder_point": round(ponto, 3),
            "safety_stock": round(seguranca, 3),
            "need_for_horizon": round(necessidade, 3),
            "to_buy": round(comprar, 3),
            "cost": round(comprar * item["unit_cost"], 2),
            "reorder_now": item["quantity"] <= ponto,
            "days_left": round(item["quantity"] / media_diaria, 1) if media_diaria > 0 else None,
            "current_minimum": item["minimum_quantity"],
            # O minimo digitado no olho vira um numero com origem: o proprio
            # historico de consumo daquele insumo no prazo do fornecedor.
            "suggested_minimum": round(ponto, 3),
            "observations": len(valores),
        }

    # ------------------------------------------------------------------
    # Curva ABC/XYZ
    # ------------------------------------------------------------------

    def _abc(self, series: dict[str, list[tuple[date, float]]], estoque: dict[str, Any], days: int) -> list[dict[str, Any]]:
        """Onde vale contar todo dia e onde vale contar uma vez por mes.

        ABC ordena pelo custo que o insumo consome; XYZ separa o que e previsivel
        do que oscila. O item AZ — caro e imprevisivel — e o que merece atencao
        humana; o CX pode viver de rotina.
        """

        linhas = []
        for item_id, serie in series.items():
            item = estoque[item_id]
            valores = [quantidade for _, quantidade in serie]
            if not valores:
                continue
            consumo = sum(valores)
            media = fmean(valores)
            desvio = pstdev(valores) if len(valores) > 1 else 0.0
            variacao = desvio / media if media else 0.0
            linhas.append({
                "inventory_id": item_id,
                "name": item["name"],
                "unit": item["unit"],
                "consumed": round(consumo, 3),
                "cost": round(consumo * item["unit_cost"], 2),
                "daily_average": round(media, 3),
                "variation": round(variacao, 2),
                "xyz": "X" if variacao < CORTE_X else "Y" if variacao < CORTE_Y else "Z",
                "observations": len(valores),
            })

        linhas.sort(key=lambda row: row["cost"], reverse=True)
        total = sum(row["cost"] for row in linhas) or 1
        acumulado = 0.0
        for linha in linhas:
            acumulado += linha["cost"]
            fatia = acumulado / total * 100
            linha["share"] = round(linha["cost"] / total * 100, 1)
            linha["cumulative"] = round(fatia, 1)
            linha["abc"] = "A" if fatia <= CORTE_A else "B" if fatia <= CORTE_B else "C"
            linha["routine"] = self._rotina(linha["abc"], linha["xyz"])
        return linhas

    @staticmethod
    def _rotina(abc: str, xyz: str) -> str:
        if abc == "A" and xyz == "Z":
            return "Contagem diária e compra acompanhada de perto: pesa no custo e é imprevisível."
        if abc == "A":
            return "Contagem diária. Concentra o custo, então erro aqui aparece no resultado."
        if abc == "B" and xyz == "Z":
            return "Contagem semanal, com folga no estoque de segurança pela oscilação."
        if abc == "B":
            return "Contagem semanal resolve."
        if xyz == "Z":
            return "Contagem mensal, mas mantenha folga: oscila muito para o pouco que custa."
        return "Contagem mensal. Baixo custo e consumo previsível."

    # ------------------------------------------------------------------
    # Serie de consumo
    # ------------------------------------------------------------------

    def _series(self, days: int, estoque: dict[str, Any]) -> tuple[dict[str, list[tuple[date, float]]], set[date]]:
        """Consumo diario por insumo: o que virou venda e o que a equipe retirou.

        A retirada entra porque tambem e consumo previsivel — a producao da
        manha sai todo dia, e um plano que a ignora manda comprar menos do que
        a loja gasta. Perda fica de fora de proposito: o plano existe para
        atender o cliente, nao para repor o que foi jogado fora. A perda entra
        no resultado por outro caminho, na tela de Consumo.
        """

        inicio = datetime.now(FUSO_LOJA).date() - timedelta(days=days - 1)
        vendas: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        movimento: set[date] = set()

        for movimentacao in self.store.list("inventory_movements"):
            dia = self.erp._local_day(movimentacao["created_at"])
            if dia < inicio:
                continue
            motivo = movimentacao["reason"]
            if motivo.startswith(REVERSAL_PREFIX):
                continue
            movimento.add(dia)
            is_sale = motivo == SALE_REASON
            is_withdrawal = movimentacao.get("kind") == WITHDRAWAL_KIND
            # A barra so sai do freezer pela fabricacao: e ela que diz quanta
            # barra comprar. Sem contar aqui, o plano mandaria repor so o que a
            # ficha tecnica consome, e a ficha nao consome barra — consome base.
            is_production = movimentacao.get("kind") == PRODUCTION_KIND
            if movimentacao["quantity_delta"] >= 0 or not (is_sale or is_withdrawal or is_production):
                continue
            vendas[movimentacao["inventory_id"]][dia] += abs(movimentacao["quantity_delta"])

        # Dia sem nenhuma movimentacao e loja fechada, nao demanda zero. Contar
        # esses dias rebaixaria todo quantil e o plano sugeriria menos do que a
        # loja precisa nos dias em que ela abre.
        dias_abertos = sorted(movimento)

        series: dict[str, list[tuple[date, float]]] = {}
        for item_id in estoque:
            por_dia = vendas.get(item_id, {})
            if not por_dia:
                continue
            series[item_id] = [(dia, por_dia.get(dia, 0.0)) for dia in dias_abertos]
        return series, set(dias_abertos)

    # ------------------------------------------------------------------
    # Estatistica
    # ------------------------------------------------------------------

    @staticmethod
    def _quantil(valores: list[float], p: float) -> float:
        """Quantil empirico com interpolacao entre ordens estatisticas.

        Sem distribuicao ajustada: o numero sai das proprias observacoes. Se o
        historico nunca viu um dia acima de X, o plano nao inventa um.
        """

        if not valores:
            return 0.0
        ordenados = sorted(valores)
        if len(ordenados) == 1:
            return ordenados[0]
        posicao = min(max(p, 0.0), 1.0) * (len(ordenados) - 1)
        baixo = int(posicao)
        alto = min(baixo + 1, len(ordenados) - 1)
        return ordenados[baixo] + (ordenados[alto] - ordenados[baixo]) * (posicao - baixo)

    @staticmethod
    def _somas_moveis(valores: list[float], janela: int) -> list[float]:
        """Demanda observada em janelas de N dias consecutivos.

        E a convolucao empirica: em vez de supor que a soma de N dias e normal,
        usa as somas que realmente aconteceram.
        """

        if janela <= 1:
            return list(valores)
        if len(valores) < janela:
            return []
        return [sum(valores[i:i + janela]) for i in range(len(valores) - janela + 1)]

    @staticmethod
    def _fator_do_dia(serie: list[tuple[date, float]], weekday: int) -> tuple[float, int]:
        """Peso do dia da semana, encolhido para 1 conforme a amostra e pequena.

        `n / (n + k)` e o encolhimento: com poucas observacoes o fator quase nao
        se afasta de 1, e ele so ganha forca conforme o historico cresce. Sem
        isso, tres sabados atipicos viram regra permanente do plano.
        """

        do_dia = [quantidade for dia, quantidade in serie if dia.weekday() == weekday]
        geral = [quantidade for _, quantidade in serie]
        if not do_dia or not geral:
            return 1.0, len(do_dia)
        media_geral = fmean(geral)
        if not media_geral:
            return 1.0, len(do_dia)
        bruto = fmean(do_dia) / media_geral
        peso = len(do_dia) / (len(do_dia) + K_ENCOLHIMENTO)
        return 1 + (bruto - 1) * peso, len(do_dia)

    # ------------------------------------------------------------------

    def _method(self, days: int, dias_abertos: int, service_level: float) -> dict[str, Any]:
        return {
            "summary": (
                f"Quantis calculados sobre {dias_abertos} dias de operação nos últimos {days}. "
                "Dias sem nenhum movimento foram tratados como loja fechada e ficaram de fora da amostra."
            ),
            "why_no_fit": (
                "Nenhuma distribuição é ajustada. O percentil sai das observações ordenadas, o peso do dia da "
                "semana é encolhido conforme a amostra é pequena, e a demanda no prazo do fornecedor vem das somas "
                "móveis observadas — não de aproximação normal. Com poucas observações por dia da semana, ajustar "
                "parâmetro produziria número preciso e sem sustentação."
            ),
            "service_level": (
                f"Compra dimensionada para cobrir {round(service_level * 100)}% dos períodos observados. "
                "A produção não usa esse número: ela usa a razão entre o custo de faltar e o de sobrar de cada insumo."
            ),
            "shrinkage_k": K_ENCOLHIMENTO,
            "minimum_observations": MINIMO_OBSERVACOES,
        }

    def _blockers(self, series: dict[str, Any], estoque: dict[str, Any], dias_abertos: set[date]) -> list[dict[str, str]]:
        pendencias: list[dict[str, str]] = []
        if len(dias_abertos) < MINIMO_OBSERVACOES:
            pendencias.append({
                "title": f"Só {len(dias_abertos)} dia(s) de consumo registrado",
                "description": "O plano precisa de histórico de baixa por venda. Enquanto os pedidos entrarem sem "
                               "itens, a ficha técnica não baixa o insumo e não há série para planejar.",
            })
        sem_serie = [item["name"] for item in estoque.values() if item["id"] not in series]
        if sem_serie:
            pendencias.append({
                "title": f"{len(sem_serie)} insumo(s) sem consumo registrado",
                "description": f"{', '.join(sorted(sem_serie)[:4])} nunca saíram por venda no período, então não "
                               "entram no plano. Ou não estão em nenhuma ficha técnica, ou as vendas que os usam "
                               "vieram importadas sem itens.",
            })
        sem_validade = [
            item["name"] for item in estoque.values()
            if item["id"] in series and not item.get("shelf_life_days")
        ]
        if sem_validade:
            pendencias.append({
                "title": "Validade não cadastrada",
                "description": f"{', '.join(sorted(sem_validade)[:4])} não têm prazo de validade no cadastro, então "
                               "ficam fora da produção do dia. Sem saber o que estraga, não dá para decidir quanto "
                               "preparar.",
            })
        return pendencias

    @staticmethod
    def _money(value: float) -> str:
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
