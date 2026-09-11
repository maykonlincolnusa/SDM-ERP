"""Quanto uma barra de açaí rende, deduzido do que a loja já registra.

O rendimento parece impossível de saber sem balança e sem alguém medindo a cuba
a cada batida. Mas ele não precisa ser medido: ele pode ser **deduzido**, porque
os dois lados da conta já passam pelo sistema por outros motivos.

De um lado, o quilo que entrou — a loja registra a compra porque precisa saber
quanto gastou. Do outro, o mililitro que saiu — a loja registra a venda porque
precisa faturar. O rendimento é a razão entre os dois, e ninguém precisou parar
para medir nada:

    rendimento = mililitros de base vendidos ÷ quilos de barra consumidos

É uma medida melhor que a batida declarada, e por uma razão que importa: ela já
tem dentro de si tudo o que a batida esconde. O que ficou na cuba, o que
derreteu, a mão pesada de quem serve, o copo que voltou. A batida declarada diz
o que deveria ter saído; esta conta diz o que de fato saiu pela porta.

Duas decisões de desenho
------------------------

**O consumo vem do saldo, não da ficha técnica.** Poderia sair do movimento que
a venda gera — mas isso tornaria a conta circular: a ficha diria quanto saiu, e
o resultado confirmaria a ficha. Vindo de `saldo inicial + compras − saldo
final`, o número é independente da ficha e serve justamente para corrigi-la.

**Sem base suficiente, nenhum número.** Um rendimento tirado de três dias de
venda e uma compra vira decisão de compra para o mês inteiro. Quando a amostra
não sustenta, o módulo diz o que falta em vez de devolver um número frágil com
cara de medida.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import Any

from app.repositories.store import Store
from app.services.erp import FUSO_LOJA, ERPService, is_active_order

# Abaixo disso a razao existe mas nao se sustenta: uma compra e alguns dias de
# venda produzem um numero que muda de metade quando a proxima compra entra.
DIAS_MINIMOS = 14
KG_MINIMOS = 5.0
COPOS_MINIMOS = 30

# O que identifica a barra e o que identifica a base pronta, pelo nome. A loja
# escreve "barra 12%", "Barra de 12", "Barra 8% saude" — o que os une e a
# palavra, nao um cadastro que ninguem manteria.
MARCAS_DA_BARRA = ("barra", "polpa")
MARCAS_DA_BASE = ("acai",)


def _fold(value: Any) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()


class YieldService:
    def __init__(self, store: Store):
        self.store = store
        self.erp = ERPService(store)

    # ------------------------------------------------------------------

    def acai_yield(self, days: int = 90, project_kg: float = 20.0) -> dict[str, Any]:
        """O rendimento deduzido, e o que N quilos rendem em copos."""

        days = max(7, min(days, 365))
        inicio = self.erp._window_start(days)
        insumos = self.store.list("inventory")

        barras = [item for item in insumos if self._e_barra(item)]
        base = self._base_pronta(insumos)
        if not barras:
            return self._sem_resposta(days, project_kg, [{
                "title": "Nenhum insumo de barra cadastrado",
                "description": "O cálculo precisa saber qual insumo é a barra que entra. "
                               "Cadastre-a no estoque, em quilo.",
            }])

        kg = self._consumo_de_barra(barras, inicio)
        vendas = self._mililitros_vendidos(base, inicio)

        bloqueios = self._bloqueios(kg, vendas, days, base)
        if bloqueios:
            return self._sem_resposta(days, project_kg, bloqueios, kg=kg, vendas=vendas)

        ml_por_kg = vendas["ml"] / kg["consumido"]
        return {
            "ready": True,
            "period_days": days,
            "ml_per_kg": round(ml_por_kg, 1),
            "litres_per_kg": round(ml_por_kg / 1000, 2),
            "kg": kg,
            "sales": vendas,
            "confidence": self._confianca(kg, vendas, days),
            "basis": (
                f"{kg['consumido']:.1f} kg de barra consumidos e {vendas['cups']} copo(s) vendidos "
                f"em {days} dias. O consumo vem do saldo e das compras, não da ficha técnica — "
                "por isso ele serve para corrigi-la."
            ),
            "projection": self.project(ml_por_kg, project_kg, base),
            "blockers": [],
        }

    def project(self, ml_por_kg: float, kg: float, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Quantos copos de cada tamanho saem de N quilos de barra."""

        base = base or self._base_pronta(self.store.list("inventory"))
        ml_total = ml_por_kg * max(0.0, kg)
        copos = []
        for produto in self.store.list("products"):
            if produto.get("active") is False:
                continue
            consumo = self._base_do_produto(produto, base)
            if consumo <= 0:
                continue
            copos.append({
                "product_id": produto["id"],
                "name": produto["name"],
                "base_ml": round(consumo, 1),
                "cups": int(ml_total // consumo),
                "revenue": round((produto.get("price") or 0) * (ml_total // consumo), 2),
            })
        copos.sort(key=lambda linha: linha["base_ml"])
        return {"kg": round(kg, 2), "ml": round(ml_total), "cups": copos}

    # ------------------------------------------------------------------

    def _consumo_de_barra(self, barras: list[dict[str, Any]], inicio: datetime) -> dict[str, Any]:
        """Quilos que saíram do estoque: saldo inicial + compras − saldo final.

        Vem do saldo e não do movimento que a venda gera, de propósito: pela
        ficha técnica a conta seria circular — ela diria quanto saiu e o
        resultado confirmaria a própria ficha.
        """

        identificadores = {item["id"] for item in barras}
        saldo_final = sum(item.get("quantity") or 0 for item in barras)
        comprado = 0.0
        contado_inicial = 0.0
        movimentos = 0
        for movimento in self.store.list("inventory_movements"):
            if movimento.get("inventory_id") not in identificadores:
                continue
            if self.erp._timestamp(movimento["created_at"]) < inicio:
                continue
            movimentos += 1
            delta = movimento.get("quantity_delta") or 0
            if movimento.get("kind") == "compra" or delta > 0:
                comprado += delta

        # Sem contagem no inicio da janela, o saldo de entao e desconhecido: o
        # que se pode afirmar e apenas que o consumo foi ao menos o comprado
        # menos o que sobrou. Dizer qual dos dois casos e este importa mais do
        # que devolver um numero redondo.
        consumido = max(0.0, comprado + contado_inicial - saldo_final)
        return {
            "purchased": round(comprado, 3),
            "closing": round(saldo_final, 3),
            "consumido": round(consumido, 3),
            "movements": movimentos,
            "items": [item["name"] for item in barras],
        }

    def _mililitros_vendidos(self, base: dict[str, Any] | None, inicio: datetime) -> dict[str, Any]:
        """Mililitros de base que as vendas do período consumiram."""

        produtos = {item["id"]: item for item in self.store.list("products")}
        consumo_por_produto = {
            identificador: self._base_do_produto(produto, base)
            for identificador, produto in produtos.items()
        }
        ml = 0.0
        copos = 0
        sem_ficha = 0
        for pedido in self.store.list("orders"):
            if not is_active_order(pedido):
                continue
            if self.erp._timestamp(pedido["created_at"]) < inicio:
                continue
            if not pedido.get("items"):
                # Pedido importado sem itens nao diz o que saiu: ele nao entra
                # na conta, e a falta e declarada em vez de virar zero.
                sem_ficha += 1
                continue
            for linha in pedido["items"]:
                consumo = consumo_por_produto.get(linha.get("product_id"), 0.0)
                if consumo <= 0:
                    continue
                ml += consumo * (linha.get("quantity") or 0)
                copos += linha.get("quantity") or 0
        return {"ml": round(ml, 1), "cups": copos, "orders_without_items": sem_ficha}

    def _base_do_produto(self, produto: dict[str, Any], base: dict[str, Any] | None) -> float:
        """Quantos mililitros de base este produto consome, pela ficha.

        A ficha pode estar errada em muita coisa, mas a linha da base é a que
        sobrevive: ela é a única que não depende de conversão de unidade, porque
        o copo é vendido em mililitro e a base é medida em mililitro.
        """

        if not base:
            return 0.0
        for linha in produto.get("recipe") or []:
            if linha.get("inventory_id") == base["id"]:
                return float(linha.get("quantity") or 0)
        return 0.0

    # ------------------------------------------------------------------

    def _e_barra(self, item: dict[str, Any]) -> bool:
        nome = _fold(item.get("name"))
        return any(marca in nome for marca in MARCAS_DA_BARRA)

    def _base_pronta(self, insumos: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A base pronta: é açaí e não é barra."""

        candidatos = [
            item for item in insumos
            if any(marca in _fold(item.get("name")) for marca in MARCAS_DA_BASE) and not self._e_barra(item)
        ]
        # O nome mais curto e quase sempre o insumo cru ("Acai") em vez de um
        # derivado ("Acai com morango"), e e ele que a ficha do copo consome.
        return min(candidatos, key=lambda item: len(str(item.get("name", "")))) if candidatos else None

    def _bloqueios(self, kg, vendas, days, base) -> list[dict[str, str]]:
        bloqueios: list[dict[str, str]] = []
        if not base:
            bloqueios.append({
                "title": "Nenhum insumo de base pronta cadastrado",
                "description": "O cálculo precisa saber qual insumo o copo consome. "
                               "É o açaí já batido, medido em mililitro.",
            })
        if kg["consumido"] < KG_MINIMOS:
            bloqueios.append({
                "title": f"Só {kg['consumido']:.1f} kg de barra consumidos no período",
                "description": (
                    f"São precisos ao menos {KG_MINIMOS:g} kg para a razão parar de mudar de metade "
                    "a cada compra. Registre as compras de barra em Estoque → Entrou, e faça a "
                    "contagem do que sobra."
                ),
            })
        if vendas["cups"] < COPOS_MINIMOS:
            bloqueios.append({
                "title": f"Só {vendas['cups']} copo(s) vendidos com produto identificado",
                "description": (
                    "O outro lado da conta são as vendas. "
                    + (f"{vendas['orders_without_items']} pedido(s) do período vieram sem os itens, "
                       "e por isso não dizem quantos mililitros saíram."
                       if vendas["orders_without_items"] else
                       "Registre as vendas item a item para o cálculo ter de onde partir.")
                ),
            })
        return bloqueios

    def _confianca(self, kg, vendas, days) -> str:
        if kg["consumido"] >= KG_MINIMOS * 4 and vendas["cups"] >= COPOS_MINIMOS * 4:
            return "alta"
        if kg["consumido"] >= KG_MINIMOS * 2 and vendas["cups"] >= COPOS_MINIMOS * 2:
            return "média"
        return "baixa"

    def _sem_resposta(self, days, project_kg, bloqueios, kg=None, vendas=None) -> dict[str, Any]:
        return {
            "ready": False,
            "period_days": days,
            "ml_per_kg": None,
            "litres_per_kg": None,
            "kg": kg or {"purchased": 0.0, "closing": 0.0, "consumido": 0.0, "movements": 0, "items": []},
            "sales": vendas or {"ml": 0.0, "cups": 0, "orders_without_items": 0},
            "confidence": "sem base",
            "basis": "",
            "projection": {"kg": round(project_kg, 2), "ml": 0, "cups": []},
            "blockers": bloqueios,
        }
