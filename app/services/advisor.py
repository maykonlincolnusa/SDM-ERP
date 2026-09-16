"""Briefing semanal para o gestor levar ao dono.

Crescimento, promoção, estoque e DRE já calculam. O que falta é a reunião:
três itens, duas vozes, nenhuma meta inventada. Este módulo não cria conta
nova — escolhe, ordena e traduz o que os outros serviços já sustentam.

Tres regras, as mesmas do restante do sistema:

1. Sem evidencia numerica nao ha prioridade. O gestor precisa poder discordar.
2. Impacto estimado viaja com a premissa. Sem premissa o numero nao aparece.
3. Amostra pequena rebaixa a confianca; nao some com o conselho e nao o
   promove a certeza.
"""

from __future__ import annotations

from typing import Any

from app.repositories.store import Store
from app.services.erp import ERPService, needs_restock
from app.services.growth import GrowthService
from app.services.planning import PlanningService
from app.services.promotions import PromotionService

MAXIMO_PRIORIDADES = 3
ORDEM_CONFIANCA = {"alta": 0, "media": 1, "baixa": 2}
PESO_CONFIANCA = {"alta": 1.0, "media": 0.65, "baixa": 0.35}

# O conselheiro nao tenta prever a vontade de compra nem atribui uma
# probabilidade inventada a uma sugestao. Ele resolve outra pergunta: entre
# acoes que os modulos ja sustentam, qual deve ocupar os poucos minutos da
# reuniao desta semana? Este e um modelo de decisao multicriterio,
# deterministico e auditavel; por isso cabe no Cloud Run junto com o ERP e nao
# exige enviar vendas ou clientes para um modelo externo.
MODELO_DECISAO = {
    "id": "mcda-evidencia-v1",
    "name": "Priorizacao multicriterio com evidencia",
    "description": (
        "Ordena riscos e acoes pela urgencia, confianca da evidencia e impacto "
        "financeiro ajustado pela confiança. O resultado é uma ordem de conversa, "
        "não uma previsão nem uma meta."
    ),
    "criteria": [
        {"id": "risk", "label": "Protecao da operacao", "max_points": 55},
        {"id": "urgency", "label": "Urgencia", "max_points": 20},
        {"id": "evidence", "label": "Confianca da evidencia", "max_points": 15},
        {"id": "impact", "label": "Impacto mensal ajustado", "max_points": 10},
    ],
}


def _money(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class AdvisorService:
    """Monta o roteiro da semana a partir do que a loja ja mediu."""

    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)
        self.growth = GrowthService(store)
        self.planning = PlanningService(store)
        self.promotions = PromotionService(store)

    def brief(self, days: int = 30) -> dict[str, Any]:
        days = max(7, min(days, 90))
        resultado = self.erp.operating_result(days)
        crescimento = self.growth.opportunities(max(days, 30))
        planejamento = self.planning.plan(max(days, 14), horizon=7, service_level=0.9)
        # Promoção exige janela mínima de 14 dias: weekday sem observação
        # suficiente não vira vale, e vale inventado é desconto no dia errado.
        promocao = self.promotions.week(max(days, 14), horizon=7)

        riscos = self._riscos(resultado, planejamento)
        decisoes = self._ordenar(self._decisoes(crescimento, promocao))
        execucoes = self._ordenar(self._execucoes(crescimento))
        prioridades = self._escolher(riscos, decisoes, execucoes)

        return {
            "period_days": days,
            "decision_model": MODELO_DECISAO,
            "situation": self._situacao(resultado),
            "priorities": prioridades,
            "dont": self._nao_fazer(resultado, promocao, crescimento),
            "blockers": crescimento.get("blockers") or [],
            "sources": {
                "growth": crescimento["totals"]["count"],
                "promotions": promocao["totals"]["count"],
                "reorder_now": sum(1 for row in planejamento["purchase"] if row["reorder_now"]),
                "quantified_growth": crescimento["totals"]["quantified"],
                "growth_impact_monthly": crescimento["totals"]["impact_monthly"],
            },
        }

    # ------------------------------------------------------------------
    # Situação: o que o mês está dizendo, nas duas vozes
    # ------------------------------------------------------------------

    def _situacao(self, resultado: dict[str, Any]) -> dict[str, Any]:
        totais = resultado["totals"]
        pedidos = totais["orders"]
        receita = totais["revenue"]
        cobertura = resultado["cost_coverage"]
        teto = totais["profit_upper_bound"]
        lucro = totais["operating_profit"]
        ticket = round(receita / pedidos, 2) if pedidos else 0.0

        if not pedidos:
            dono = (
                "Ainda não há faturamento neste período. A reunião desta semana é sobre "
                "o cadastro da loja, não sobre crescimento."
            )
            gestor = (
                "Antes de levar número ao dono, registre a primeira venda e a contagem "
                "inicial. Sem isso qualquer conselho de mix ou canal é palpite."
            )
        elif not resultado["complete"]:
            dono = (
                f"O faturamento foi {_money(receita)} em {pedidos} pedido(s). O lucro operacional "
                f"ainda não fecha: a cobertura de custo é {cobertura}%. O teto do resultado é "
                f"{_money(teto)} — qualquer custo que faltar só reduz esse número."
            )
            gestor = (
                "Não apresente o teto como lucro fechado. O que falta é o custo dos pedidos "
                "sem itens: conferir a importação muda o resultado, inventar custo não."
            )
        else:
            dono = (
                f"O faturamento foi {_money(receita)} em {pedidos} pedido(s), ticket médio "
                f"{_money(ticket)}. O lucro operacional ficou em {_money(lucro)}, já descontando "
                f"insumo, taxa de canal ({_money(totais['channel_fees'])}), perda, retirada e "
                "despesa lançada no período."
            )
            gestor = (
                "Este é o número que pode ir à reunião: a cobertura de custo está completa. "
                "Se o dono pedir 'potencial de crescimento', a soma das oportunidades não é meta."
            )

        return {
            "for_owner": dono,
            "for_manager": gestor,
            "revenue": receita,
            "orders": pedidos,
            "average_ticket": ticket,
            "operating_profit": lucro,
            "profit_upper_bound": teto,
            "cost_coverage": cobertura,
            "complete": resultado["complete"],
            "channel_fees": totais["channel_fees"],
        }

    # ------------------------------------------------------------------
    # Prioridades: no máximo três, na ordem que a reunião deve seguir
    # ------------------------------------------------------------------

    def _riscos(self, resultado: dict[str, Any], planejamento: dict[str, Any]) -> list[dict[str, Any]]:
        itens: list[dict[str, Any]] = []
        estoque = self.store.list("inventory")
        negativos = [item for item in estoque if item["quantity"] < 0]
        if negativos:
            nomes = ", ".join(item["name"] for item in negativos[:3])
            itens.append(self._item(
                "risco-saldo-negativo", "risco", "Estoque",
                title="Saldo negativo no estoque",
                for_owner=(
                    f"O estoque está negativo: {nomes} saiu mais do que o cadastro tinha. "
                    "itens não bate até a contagem ser refeita."
                ),
                for_manager=(
                    "Refaça a contagem desses insumos hoje. Enquanto o saldo estiver negativo, "
                    "margem e reposição mentem."
                ),
                evidence=f"{len(negativos)} insumo(s) com quantidade abaixo de zero.",
                premise="O saldo negativo é o registro da saída, não um lucro extra.",
                view="inventory",
                confidence="alta",
                confidence_note="lido do saldo atual, não de uma estimativa",
                urgency=0,
            ))

        criticos = [item for item in estoque if needs_restock(item)]
        comprar = [row for row in planejamento["purchase"] if row["reorder_now"]]
        if criticos or comprar:
            nomes_minimo = ", ".join(item["name"] for item in criticos[:2])
            nomes_plano = ", ".join(row["name"] for row in comprar[:2])
            nomes = nomes_minimo or nomes_plano
            custo = round(sum(row["cost"] for row in comprar), 2)
            itens.append(self._item(
                "risco-reposicao", "risco", "Estoque",
                title="Reposição antes do próximo pico",
                for_owner=(
                    f"{nomes} chegou no ponto de pedir de novo. Sem compra, a ruptura "
                    "corta venda que o cardápio já promete."
                ),
                for_manager=(
                    "Abra Planejamento, confirme o ponto de pedido e lance a compra. "
                    "Não espere o mínimo digitado no olho se o histórico já pediu reposição."
                ),
                evidence=(
                    (f"{len(criticos)} insumo(s) no mínimo cadastrado. " if criticos else "")
                    + (f"{len(comprar)} insumo(s) no ponto de pedido do planejamento"
                       + (f", cerca de {_money(custo)} para repor o horizonte." if custo else ".")
                       if comprar else "")
                ).strip(),
                premise="O ponto de pedido vem do consumo medido no prazo do fornecedor, não de chute de venda.",
                view="planning",
                impact_monthly=None,
                confidence="alta" if criticos else "media",
                confidence_note="mínimo cadastrado é evidência dura; o ponto de pedido depende da série de consumo",
            ))

        if resultado["totals"]["orders"] and resultado["cost_coverage"] < 80:
            itens.append(self._item(
                "risco-cobertura", "risco", "Resultado",
                title="Lucro ainda aberto",
                for_owner=(
                    f"Só {resultado['cost_coverage']}% do faturamento tem custo conhecido. "
                    "Fechar o mês com o teto como se fosse lucro empurra uma decisão no escuro."
                ),
                for_manager=(
                    "Priorize os pedidos importados sem itens. Cada um que ganha ficha técnica "
                    "sobe a cobertura e o número da reunião deixa de ser teto."
                ),
                evidence=resultado["blockers"][0] if resultado.get("blockers") else (
                    f"Cobertura de custo em {resultado['cost_coverage']}%."
                ),
                premise="Custo desconhecido nunca é zero: o que falta só pode reduzir o resultado.",
                view="finance",
                confidence="alta",
                confidence_note="cobertura medida sobre o faturamento do período",
            ))

        if not resultado["totals"]["orders"]:
            estoque_zerado = estoque and not any(item["quantity"] for item in estoque)
            itens.append(self._item(
                "risco-cadastro", "risco", "Cadastro",
                title="Faça a contagem inicial" if estoque_zerado else "Registre a primeira venda",
                for_owner=(
                    "Sem movimento no período o conselheiro não tem o que defender. "
                    "A decisão desta semana é colocar a loja no sistema."
                ),
                for_manager=(
                    "Use Contagem para lançar o que existe na prateleira e Nova venda para o "
                    "primeiro pedido. Faturamento, ticket e margem nascem daí."
                ),
                evidence=(
                    f"{len(estoque)} insumo(s) cadastrado(s) sem saldo." if estoque_zerado
                    else "Nenhum pedido ativo no período analisado."
                ),
                premise="Não há impacto mensal a estimar sem venda medida.",
                view="inventory" if estoque_zerado else "orders",
                confidence="alta",
                confidence_note="ausência de movimento, não amostra pequena",
            ))
        return itens

    def _decisoes(self, crescimento: dict[str, Any], promocao: dict[str, Any]) -> list[dict[str, Any]]:
        itens: list[dict[str, Any]] = []
        preco = next((row for row in crescimento["suggestions"] if row["type"] == "preco"), None)
        if preco:
            itens.append(self._de_sugestao(
                preco, "decisao", "Preços",
                for_owner=preco["title"],
                for_manager=preco["action"],
                view="pricing",
                urgency=0 if "impossível" in preco["id"] or "ficha" in preco["id"] or "prejuizo" in preco["id"] or "prejuízo" in preco["title"] else 1,
            ))

        canal = next((row for row in crescimento["suggestions"] if row["type"] == "canal"), None)
        if canal:
            itens.append(self._de_sugestao(
                canal, "decisao", "Canais",
                for_owner=canal["title"],
                for_manager=canal["action"],
                view="channels",
                urgency=0 if "taxa-zero" in canal["id"] else 1,
            ))

        oferta = self._melhor_oferta(promocao)
        if oferta:
            virada = oferta.get("breakeven") or {}
            unidades = virada.get("units")
            premissa = oferta.get("upside_note") or ""
            if unidades is not None:
                premissa = (
                    f"A promoção só empata com +{unidades:g} unidade(s) neste bloco. "
                    + (virada.get("note") or "")
                ).strip()
            itens.append(self._item(
                f"decisao-{oferta['id']}", "decisao", "Promoções",
                title=oferta["title"],
                for_owner=(
                    f"{oferta.get('goal') or 'Oferta da semana'}: {oferta['title']}. "
                    "É margem cedida; só vale se o volume extra existir de verdade."
                ),
                for_manager=(
                    f"Conferir piso ({_money(oferta['floor']['cost_with_shrinkage'])} de custo com quebra) "
                    f"e publicar só em {oferta.get('block_name') or oferta.get('weekday')}. "
                    "Dia cheio não entra."
                ),
                evidence=oferta["evidence"],
                premise=premissa or "Sem ponto de virada o desconto não tem como ser julgado.",
                view="promotions",
                impact_monthly=oferta.get("upside_monthly"),
                confidence=oferta.get("confidence", "media"),
                confidence_note=oferta.get("confidence_note", ""),
                urgency=2 if (oferta.get("breakeven") or {}).get("units") is not None else 4,
            ))
        return itens

    def _execucoes(self, crescimento: dict[str, Any]) -> list[dict[str, Any]]:
        itens: list[dict[str, Any]] = []
        for row in crescimento["suggestions"]:
            if row["type"] in {"preco", "canal"}:
                continue
            itens.append(self._de_sugestao(
                row, "execucao", "Crescimento",
                for_owner=row["title"],
                for_manager=row["action"],
                view="growth",
            ))
        return itens

    @staticmethod
    def _melhor_oferta(promocao: dict[str, Any]) -> dict[str, Any] | None:
        """A oferta da reunião é a que tem ponto de virada, não a primeira da lista.

        Sem unidades de empate o desconto não dá para julgar. Preferir essa à
        de maior upside evita vender promoção que não se paga.
        """

        ofertas = list(promocao.get("promotions") or [])
        com_virada = [row for row in ofertas if (row.get("breakeven") or {}).get("units") is not None]
        pool = com_virada or ofertas
        if not pool:
            return None
        return min(
            pool,
            key=lambda row: (
                ORDEM_CONFIANCA.get(row.get("confidence"), 1),
                -(row.get("upside_monthly") or 0),
            ),
        )

    @staticmethod
    def _ordenar(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in itens:
            AdvisorService._pontuar(item)
        itens.sort(key=lambda row: (
            -row["decision_score"],
            row["urgency"],
            ORDEM_CONFIANCA.get(row["confidence"], 1),
            row["id"],
        ))
        return itens

    def _escolher(
        self,
        riscos: list[dict[str, Any]],
        decisoes: list[dict[str, Any]],
        execucoes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Um de cada faixa, nesta ordem. Lista longa não é direção."""

        # Risco, decisão e execução continuam sendo faixas distintas: sem essa
        # diversidade, um grande risco de cadastro ou três ideias parecidas de
        # marketing ocupariam todo o briefing. Dentro de cada faixa, porém, a
        # escolha é feita pela pontuação multicritério abaixo.
        riscos = self._ordenar(riscos)
        escolhidas: list[dict[str, Any]] = []
        for faixa in (riscos, decisoes, execucoes):
            if faixa:
                escolhidas.append(faixa[0])
            if len(escolhidas) >= MAXIMO_PRIORIDADES:
                break
        for indice, row in enumerate(escolhidas, start=1):
            row["rank"] = indice
        return escolhidas

    @staticmethod
    def _pontuar(item: dict[str, Any]) -> None:
        """Anexa uma pontuação auditável sem disfarçá-la de probabilidade.

        Risco observado recebe prioridade alta mesmo sem valor monetário: uma
        ruptura ou um saldo negativo pode invalidar o lucro inteiro. Para ações
        de crescimento, impacto só rende pontos depois de ser reduzido pela
        confiança da amostra. A raiz evita que um único palpite de valor muito
        alto domine a reunião.
        """

        confianca = PESO_CONFIANCA.get(item.get("confidence"), PESO_CONFIANCA["media"])
        urgencia = max(0, min(int(item.get("urgency", 3)), 5))
        pontos_urgencia = round((5 - urgencia) * 4, 1)
        pontos_evidencia = round(15 * confianca, 1)
        impacto = max(0.0, float(item.get("impact_monthly") or 0.0))
        # R$ 2.500/mês já atinge o teto desta dimensão. É uma escala de
        # ordenação, não uma promessa de retorno.
        pontos_impacto = round(min(10.0, (impacto / 2500) ** 0.5 * 10) * confianca, 1) if impacto else 0.0
        pontos_risco = 55.0 if item.get("kind") == "risco" else 0.0
        total = round(min(100.0, pontos_risco + pontos_urgencia + pontos_evidencia + pontos_impacto), 1)
        item["decision_score"] = total
        item["score_breakdown"] = {
            "risk": pontos_risco,
            "urgency": pontos_urgencia,
            "evidence": pontos_evidencia,
            "impact": pontos_impacto,
        }
        item["score_note"] = (
            "Pontuação de prioridade para a reunião; não é chance de sucesso nem meta financeira."
        )

    def _nao_fazer(
        self,
        resultado: dict[str, Any],
        promocao: dict[str, Any],
        crescimento: dict[str, Any],
    ) -> list[dict[str, str]]:
        recusas: list[dict[str, str]] = [
            {
                "title": "Não some as oportunidades e chame de meta",
                "reason": (
                    f"As {crescimento['totals']['quantified']} sugestões quantificadas somam "
                    f"{_money(crescimento['totals']['impact_monthly'])} por mês nas premissas de cada uma. "
                    "O número ordena o que fazer primeiro; virar meta sem conferir a conta é chute."
                ),
            }
        ]
        if resultado["totals"]["orders"] and not resultado["complete"]:
            recusas.append({
                "title": "Não apresente o teto do lucro como resultado fechado",
                "reason": (
                    f"A cobertura de custo está em {resultado['cost_coverage']}%. "
                    "O que falta só reduz o número."
                ),
            })
        dias_cheios = [
            dia for dia in promocao.get("week") or []
            if dia.get("factor") is not None and not dia.get("is_valley") and not dia.get("mostly_closed")
        ]
        if dias_cheios:
            nomes = ", ".join(dia["name"] for dia in dias_cheios[:3])
            recusas.append({
                "title": "Não desconte o dia que já está cheio",
                "reason": (
                    f"{nomes} já vende acima da média dos dias abertos. "
                    "Desconto ali compra a venda que viria pelo preço cheio."
                ),
            })
        else:
            recusas.append({
                "title": "Não desconte um dia forte para 'aquecer' o movimento",
                "reason": (
                    "Promoção em dia cheio troca margem por volume que a loja já tinha. "
                    "O desconto só se paga no vale, e só acima do piso."
                ),
            })
        return recusas[:3]

    def _de_sugestao(
        self,
        row: dict[str, Any],
        kind: str,
        source: str,
        *,
        for_owner: str,
        for_manager: str,
        view: str,
        urgency: int = 3,
    ) -> dict[str, Any]:
        return self._item(
            f"{kind}-{row['id']}", kind, source,
            title=row["title"],
            for_owner=for_owner,
            for_manager=for_manager,
            evidence=row["evidence"],
            premise=row.get("impact_note") or row.get("confidence_note") or "",
            view=view,
            impact_monthly=row.get("impact_monthly"),
            confidence=row.get("confidence", "media"),
            confidence_note=row.get("confidence_note", ""),
            urgency=urgency,
        )

    @staticmethod
    def _item(
        identifier: str, kind: str, source: str, *,
        title: str, for_owner: str, for_manager: str, evidence: str, premise: str,
        view: str, impact_monthly: float | None = None,
        confidence: str = "media", confidence_note: str = "",
        urgency: int = 3,
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "kind": kind,
            "source": source,
            "title": title,
            "for_owner": for_owner,
            "for_manager": for_manager,
            "evidence": evidence,
            "premise": premise,
            "view": view,
            "impact_monthly": impact_monthly,
            "confidence": confidence,
            "confidence_note": confidence_note,
            "urgency": urgency,
        }
