"""Entregadores, entregas e o que a entrega custa.

A pergunta que este modulo responde nao e "quantas entregas o Joao fez". E
**a entrega se paga?** — o frete que o cliente paga cobre o que o entregador
recebe, ou a loja esta bancando a diferenca com a margem do produto?

Sem isso, delivery vira um canal que parece crescer bem no faturamento e drena
resultado por baixo. O frete aparece somado a receita, o pagamento do
entregador sai como despesa avulsa, e ninguem cruza os dois.

Duas decisoes de modelagem
--------------------------

**A remuneracao soma componentes em vez de escolher um modelo.** Loja de bairro
combina diaria com valor por entrega, ou comissao com bonus de chuva. Forcar a
escolha de um unico modelo faria o resto ser lancado "no bonus", que e
exatamente onde o controle se perde.

**O lancamento e por turno, nao por entrega.** E assim que o acerto acontece no
balcao no fim do dia. Pedir uma linha por entrega produziria um cadastro que a
loja abandona na primeira semana movimentada — e cadastro abandonado e pior que
cadastro que nao existe, porque parece dado.

O sistema tambem cruza o que foi lancado com os pedidos de entrega que existem
no ERP naquele dia. Divergencia entre os dois nao e acusacao de nada: quase
sempre e pedido que entrou sem entregador marcado. Mas ela precisa ficar
visivel, senao o acerto vira palavra contra palavra.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.repositories.store import Store
from app.schemas import CourierCreate, CourierUpdate, DeliveryEntryCreate, DeliveryEntryUpdate
from app.services.erp import FUSO_LOJA, ERPService, is_active_order, is_delivery_order


class DeliveryService:
    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------
    # Entregadores
    # ------------------------------------------------------------------

    def couriers(self) -> list[dict[str, Any]]:
        registrados = self.store.list("couriers")
        return sorted(registrados, key=lambda row: (not row.get("active", True), row["name"]))

    def create_courier(self, payload: CourierCreate) -> dict[str, Any]:
        courier = payload.model_dump()
        courier["id"] = self._identifier(payload.name)
        if self.store.get("couriers", courier["id"]):
            courier["id"] = f"{courier['id']}-{str(uuid4())[:5]}"
        courier["created_at"] = datetime.now(FUSO_LOJA).isoformat()
        self.store.put("couriers", courier["id"], courier)
        self.erp._audit(self.store, "courier", courier["id"], "criado", f"Entregador {payload.name} cadastrado")
        return courier

    def update_courier(self, courier_id: str, payload: CourierUpdate) -> dict[str, Any]:
        courier = self.store.get("couriers", courier_id)
        if not courier:
            raise ValueError("Entregador não encontrado.")
        mudancas = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
        antes = {k: courier.get(k) for k in mudancas}
        courier.update(mudancas)
        courier["updated_at"] = datetime.now(FUSO_LOJA).isoformat()
        self.store.put("couriers", courier_id, courier)
        self.erp._audit(
            self.store, "courier", courier_id, "editado",
            f"Entregador {courier['name']} atualizado",
            self.erp._diff(antes, mudancas),
        )
        return courier

    def delete_courier(self, courier_id: str) -> dict[str, str]:
        """Entregador com lancamento vira inativo, nunca some.

        Apagar deixaria turnos pagos apontando para ninguem e o historico de
        custo da entrega ficaria sem dono.
        """

        courier = self.store.get("couriers", courier_id)
        if not courier:
            raise ValueError("Entregador não encontrado.")
        tem_lancamento = any(row["courier_id"] == courier_id for row in self.store.list("delivery_entries"))
        tem_pedido = any(row.get("courier_id") == courier_id for row in self.store.list("orders"))
        if tem_lancamento or tem_pedido:
            courier["active"] = False
            self.store.put("couriers", courier_id, courier)
            self.erp._audit(self.store, "courier", courier_id, "inativado", f"{courier['name']} desativado")
            return {"id": courier_id, "status": "inativado", "detail": "O entregador tem histórico, então foi desativado em vez de excluído."}
        self.store.delete("couriers", courier_id)
        self.erp._audit(self.store, "courier", courier_id, "excluido", f"Entregador {courier['name']} removido")
        return {"id": courier_id, "status": "excluido", "detail": "Entregador removido."}

    # ------------------------------------------------------------------
    # Lancamentos de turno
    # ------------------------------------------------------------------

    def entries(self, days: int = 30, courier_id: str | None = None) -> list[dict[str, Any]]:
        inicio = datetime.now(FUSO_LOJA).date() - timedelta(days=max(1, min(days, 365)) - 1)
        entregadores = {row["id"]: row for row in self.store.list("couriers")}
        linhas = []
        for entry in self.store.list("delivery_entries"):
            dia = self._dia(entry["date"])
            if not dia or dia < inicio:
                continue
            if courier_id and entry["courier_id"] != courier_id:
                continue
            linhas.append(self._decorar(entry, entregadores.get(entry["courier_id"])))
        linhas.sort(key=lambda row: (row["date"], row["courier_name"]), reverse=True)
        return linhas

    def create_entry(self, payload: DeliveryEntryCreate) -> dict[str, Any]:
        courier = self.store.get("couriers", payload.courier_id)
        if not courier:
            raise ValueError("Entregador não encontrado.")
        if not self._dia(payload.date):
            raise ValueError("Data inválida. Use o formato AAAA-MM-DD.")
        if self._dia(payload.date) > datetime.now(FUSO_LOJA).date():
            raise ValueError("Não dá para lançar turno no futuro.")

        entry = payload.model_dump()
        entry["id"] = str(uuid4())
        entry["created_at"] = datetime.now(FUSO_LOJA).isoformat()
        self.store.put("delivery_entries", entry["id"], entry)
        decorado = self._decorar(entry, courier)
        self.erp._audit(
            self.store, "delivery", entry["id"], "criado",
            f"{courier['name']} · {payload.date} · {payload.deliveries} entrega(s) · {self._money(decorado['payout'])}",
        )
        return decorado

    def update_entry(self, entry_id: str, payload: DeliveryEntryUpdate) -> dict[str, Any]:
        entry = self.store.get("delivery_entries", entry_id)
        if not entry:
            raise ValueError("Lançamento não encontrado.")
        mudancas = payload.model_dump(exclude_unset=True)
        # payout_override precisa poder voltar a ser nulo, entao ele nao pode
        # cair no filtro que descarta None como "campo nao enviado".
        limpo = {k: v for k, v in mudancas.items() if v is not None or k in ("payout_override", "note")}
        if "courier_id" in limpo and not self.store.get("couriers", limpo["courier_id"]):
            raise ValueError("Entregador não encontrado.")
        if limpo.get("date") and not self._dia(limpo["date"]):
            raise ValueError("Data inválida. Use o formato AAAA-MM-DD.")

        if limpo.get("date") and self._dia(limpo["date"]) > datetime.now(FUSO_LOJA).date():
            raise ValueError("Não dá para lançar turno no futuro.")

        antes = self._resumo(entry)
        entry.update(limpo)
        if entry.get("payout_override") is not None and not (entry.get("note") or "").strip():
            raise ValueError("Ao substituir o valor calculado, escreva o motivo no campo de observação.")
        entry["updated_at"] = datetime.now(FUSO_LOJA).isoformat()
        self.store.put("delivery_entries", entry_id, entry)
        courier = self.store.get("couriers", entry["courier_id"])
        decorado = self._decorar(entry, courier)
        self.erp._audit(
            self.store, "delivery", entry_id, "editado",
            f"Turno de {decorado['courier_name']} em {entry['date']} corrigido",
            self.erp._diff(antes, self._resumo(entry)),
        )
        return decorado

    def delete_entry(self, entry_id: str) -> dict[str, str]:
        entry = self.store.get("delivery_entries", entry_id)
        if not entry:
            raise ValueError("Lançamento não encontrado.")
        courier = self.store.get("couriers", entry["courier_id"])
        self.store.delete("delivery_entries", entry_id)
        self.erp._audit(
            self.store, "delivery", entry_id, "excluido",
            f"Turno de {(courier or {}).get('name', 'entregador')} em {entry['date']} removido",
        )
        return {"id": entry_id, "status": "excluido"}

    def suggest(self, courier_id: str, day: str) -> dict[str, Any]:
        """O que os pedidos daquele dia dizem, para o lancamento partir do fato.

        Digitar de memoria no fim do expediente e onde o numero se perde. Se o
        pedido ja registra frete e entregador, o turno vem preenchido e a loja
        so confere.
        """

        dia = self._dia(day)
        if not dia:
            raise ValueError("Data inválida. Use o formato AAAA-MM-DD.")
        entregas = 0
        frete = 0.0
        sem_dono = 0
        for order in self.store.list("orders"):
            if not is_active_order(order) or self.erp._local_day(order["created_at"]) != dia:
                continue
            if not is_delivery_order(order):
                continue
            if order.get("courier_id") == courier_id:
                entregas += 1
                frete += order.get("delivery_fee") or 0
            elif not order.get("courier_id"):
                sem_dono += 1
        return {
            "courier_id": courier_id,
            "date": dia.isoformat(),
            "deliveries": entregas,
            "freight_total": round(frete, 2),
            "unassigned_orders": sem_dono,
            "note": (
                f"{sem_dono} pedido(s) de entrega neste dia estão sem entregador marcado."
                if sem_dono else ""
            ),
        }

    # ------------------------------------------------------------------
    # Acompanhamento
    # ------------------------------------------------------------------

    def performance(self, days: int = 30) -> dict[str, Any]:
        """Quanto cada entregador entregou, arrecadou e custou."""

        days = max(1, min(days, 365))
        hoje = datetime.now(FUSO_LOJA).date()
        inicio = hoje - timedelta(days=days - 1)
        entregadores = {row["id"]: row for row in self.store.list("couriers")}

        linhas: dict[str, dict[str, Any]] = {}
        por_dia: dict[str, float] = defaultdict(float)
        for entry in self.store.list("delivery_entries"):
            dia = self._dia(entry["date"])
            if not dia or dia < inicio or dia > hoje:
                continue
            courier = entregadores.get(entry["courier_id"])
            decorado = self._decorar(entry, courier)
            linha = linhas.setdefault(entry["courier_id"], {
                "courier_id": entry["courier_id"],
                "name": decorado["courier_name"],
                "active": (courier or {}).get("active", True),
                "vehicle": (courier or {}).get("vehicle"),
                "deliveries": 0, "freight": 0.0, "payout": 0.0,
                "shifts": 0, "worked_days": 0, "bonus": 0.0, "deduction": 0.0,
                "overrides": 0,
            })
            linha["deliveries"] += entry.get("deliveries", 0)
            linha["freight"] += entry.get("freight_total", 0)
            linha["payout"] += decorado["payout"]
            linha["shifts"] += 1
            linha["worked_days"] += 1 if entry.get("worked", True) else 0
            linha["bonus"] += entry.get("bonus", 0)
            linha["deduction"] += entry.get("deduction", 0)
            linha["overrides"] += 1 if entry.get("payout_override") is not None else 0
            por_dia[entry["date"]] += decorado["payout"]

        # Salario fixo nao aparece em turno nenhum, mas e custo do periodo.
        # Deixar de fora faria a entrega parecer mais barata do que e.
        for courier_id, courier in entregadores.items():
            salario = courier.get("monthly_salary", 0)
            if not salario:
                continue
            linha = linhas.setdefault(courier_id, {
                "courier_id": courier_id, "name": courier["name"],
                "active": courier.get("active", True), "vehicle": courier.get("vehicle"),
                "deliveries": 0, "freight": 0.0, "payout": 0.0,
                "shifts": 0, "worked_days": 0, "bonus": 0.0, "deduction": 0.0, "overrides": 0,
            })
            linha["salary"] = round(salario * days / 30, 2)
            linha["payout"] += linha["salary"]

        resultado = []
        for linha in linhas.values():
            entregas = linha["deliveries"]
            resultado.append({
                **linha,
                "salary": linha.get("salary", 0.0),
                "freight": round(linha["freight"], 2),
                "payout": round(linha["payout"], 2),
                "bonus": round(linha["bonus"], 2),
                "deduction": round(linha["deduction"], 2),
                # As tres leituras que decidem: quanto custa cada entrega, o que
                # sobra do frete e quantas entregas rendem por turno.
                "cost_per_delivery": round(linha["payout"] / entregas, 2) if entregas else None,
                "result": round(linha["freight"] - linha["payout"], 2),
                "deliveries_per_shift": round(entregas / linha["shifts"], 1) if linha["shifts"] else 0.0,
                "coverage": round(linha["freight"] / linha["payout"] * 100, 1) if linha["payout"] else None,
            })
        resultado.sort(key=lambda row: row["deliveries"], reverse=True)

        pedidos = self._conferencia(inicio, hoje)
        frete_total = round(sum(row["freight"] for row in resultado), 2)
        custo_total = round(sum(row["payout"] for row in resultado), 2)
        entregas_total = sum(row["deliveries"] for row in resultado)

        return {
            "period_days": days,
            "couriers": resultado,
            "totals": {
                "deliveries": entregas_total,
                "freight": frete_total,
                "payout": custo_total,
                "result": round(frete_total - custo_total, 2),
                "cost_per_delivery": round(custo_total / entregas_total, 2) if entregas_total else None,
                "freight_per_delivery": round(frete_total / entregas_total, 2) if entregas_total else None,
                # Quanto do custo da entrega o frete cobre. Abaixo de 100%, a
                # diferenca sai da margem do produto — e essa e a conta que
                # decide se o delivery vale a pena como esta.
                "coverage": round(frete_total / custo_total * 100, 1) if custo_total else None,
            },
            "series": [
                {"date": (hoje - timedelta(days=offset)).isoformat(), "payout": round(por_dia.get((hoje - timedelta(days=offset)).isoformat(), 0.0), 2)}
                for offset in range(min(days, 30) - 1, -1, -1)
            ],
            "reconciliation": pedidos,
            "readings": self._leituras(resultado, frete_total, custo_total, entregas_total, pedidos),
        }

    def _conferencia(self, inicio: date, fim: date) -> dict[str, Any]:
        """Cruza o turno lancado com os pedidos de entrega do ERP.

        Divergencia aqui quase nunca e problema de pessoa: e pedido que entrou
        sem entregador marcado. Mas precisa ficar visivel, senao o acerto do
        fim do mes vira palavra contra palavra.
        """

        pedidos = 0
        frete = 0.0
        sem_dono = 0
        for order in self.store.list("orders"):
            if not is_active_order(order):
                continue
            dia = self.erp._local_day(order["created_at"])
            if dia < inicio or dia > fim:
                continue
            if not is_delivery_order(order):
                continue
            pedidos += 1
            frete += order.get("delivery_fee") or 0
            if not order.get("courier_id"):
                sem_dono += 1
        lancadas = sum(row.get("deliveries", 0) for row in self.store.list("delivery_entries") if self._no_periodo(row, inicio, fim))
        return {
            "delivery_orders": pedidos,
            "order_freight": round(frete, 2),
            "orders_without_courier": sem_dono,
            "entries_deliveries": lancadas,
            "difference": lancadas - pedidos,
        }

    def _no_periodo(self, entry: dict[str, Any], inicio: date, fim: date) -> bool:
        dia = self._dia(entry["date"])
        return bool(dia and inicio <= dia <= fim)

    def _leituras(self, linhas, frete, custo, entregas, conferencia) -> list[dict[str, str]]:
        leituras: list[dict[str, str]] = []
        if not entregas:
            leituras.append({
                "type": "info",
                "title": "Nenhuma entrega lançada no período",
                "description": "Cadastre os entregadores e lance o fechamento de cada turno para o custo da entrega aparecer.",
            })
            return leituras

        if custo and frete < custo:
            falta = custo - frete
            leituras.append({
                "type": "attention",
                "title": "O frete não cobre a entrega",
                "description": (
                    f"Os clientes pagaram {self._money(frete)} de frete e a entrega custou {self._money(custo)}. "
                    f"A diferença de {self._money(falta)} sai da margem do produto."
                ),
            })
        elif custo:
            leituras.append({
                "type": "opportunity",
                "title": "A entrega se paga",
                "description": f"O frete cobriu {self._money(frete)} de {self._money(custo)} de custo e ainda sobraram {self._money(frete - custo)}.",
            })

        caros = [row for row in linhas if row["cost_per_delivery"] is not None]
        if len(caros) > 1:
            caro = max(caros, key=lambda row: row["cost_per_delivery"])
            barato = min(caros, key=lambda row: row["cost_per_delivery"])
            if caro["cost_per_delivery"] > barato["cost_per_delivery"] * 1.3:
                leituras.append({
                    "type": "info",
                    "title": f"{caro['name']} custa mais por entrega",
                    "description": (
                        f"{self._money(caro['cost_per_delivery'])} por entrega contra {self._money(barato['cost_per_delivery'])} "
                        f"de {barato['name']}. Costuma ser diária com poucas entregas no turno, não preço combinado diferente — "
                        f"confira o volume antes de mexer no acerto."
                    ),
                })

        if conferencia["orders_without_courier"]:
            leituras.append({
                "type": "attention",
                "title": f"{conferencia['orders_without_courier']} pedido(s) de entrega sem entregador",
                "description": "Sem o entregador marcado no pedido, o cruzamento entre o que foi lançado e o que saiu não fecha.",
            })

        overrides = sum(row["overrides"] for row in linhas)
        if overrides:
            leituras.append({
                "type": "info",
                "title": f"{overrides} turno(s) com valor substituído à mão",
                "description": "O valor pago foi diferente do combinado nesses turnos. Cada um tem o motivo registrado no lançamento.",
            })
        return leituras

    # ------------------------------------------------------------------

    @staticmethod
    def _calcular_pagamento(entry: dict[str, Any], courier: dict[str, Any] | None) -> tuple[float, list[dict[str, Any]]]:
        """Soma os componentes combinados e devolve a conta aberta.

        A memoria de calculo volta junto de proposito: acerto de entregador e
        conversa, e conversa sem a conta na tela vira desconfianca.
        """

        courier = courier or {}
        partes: list[dict[str, Any]] = []
        entregas = entry.get("deliveries", 0)
        frete = entry.get("freight_total", 0)

        por_entrega = courier.get("fee_per_delivery", 0) * entregas
        if por_entrega:
            partes.append({"label": f"{entregas} entrega(s) × {courier.get('fee_per_delivery', 0)}", "value": round(por_entrega, 2)})
        comissao = frete * courier.get("commission_percent", 0) / 100
        if comissao:
            partes.append({"label": f"{courier.get('commission_percent', 0)}% do frete", "value": round(comissao, 2)})
        diaria = courier.get("daily_rate", 0) if entry.get("worked", True) else 0
        if diaria:
            partes.append({"label": "Diária", "value": round(diaria, 2)})
        bonus = entry.get("bonus", 0)
        if bonus:
            partes.append({"label": "Bônus", "value": round(bonus, 2)})
        desconto = entry.get("deduction", 0)
        if desconto:
            partes.append({"label": "Desconto", "value": -round(desconto, 2)})

        calculado = round(por_entrega + comissao + diaria + bonus - desconto, 2)
        return calculado, partes

    def _decorar(self, entry: dict[str, Any], courier: dict[str, Any] | None) -> dict[str, Any]:
        calculado, partes = self._calcular_pagamento(entry, courier)
        override = entry.get("payout_override")
        return {
            **entry,
            "courier_name": (courier or {}).get("name", "Entregador removido"),
            "calculated_payout": calculado,
            "payout": round(override, 2) if override is not None else calculado,
            "breakdown": partes,
            "overridden": override is not None,
            "freight_result": round(entry.get("freight_total", 0) - (override if override is not None else calculado), 2),
        }

    @staticmethod
    def _resumo(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "date": entry.get("date"),
            "deliveries": entry.get("deliveries"),
            "freight_total": entry.get("freight_total"),
            "worked": entry.get("worked"),
            "bonus": entry.get("bonus"),
            "deduction": entry.get("deduction"),
            "payout_override": entry.get("payout_override"),
        }

    @staticmethod
    def _dia(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _identifier(value: str) -> str:
        normalizado = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", normalizado.lower()).strip("-")
        return f"courier-{slug or 'entregador'}"

    @staticmethod
    def _money(value: float) -> str:
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
