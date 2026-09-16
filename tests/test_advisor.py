"""Briefing do conselheiro.

O risco deste modulo nao e errar a conta — ele nao inventa conta. O risco e
soar como direcao quando ainda e lista, ou apresentar teto como lucro. Cada
teste trava uma dessas duas metades.
"""

from datetime import datetime, timedelta

from app.repositories.store import DemoStore
from app.services import AdvisorService
from app.services.erp import FUSO_LOJA

PRECOS = {"acai-300": 16.0, "acai-500": 22.0, "acai-700": 28.0, "super-bowl": 31.0}
CUSTOS = {"acai-300": 8.6, "acai-500": 13.82, "acai-700": 19.09, "super-bowl": 20.6}
NOMES = {"acai-300": "Açaí 300 ml", "acai-500": "Açaí 500 ml", "acai-700": "Açaí 700 ml", "super-bowl": "Super Bowl"}


def _pedido(loja, numero, dias_atras, itens, canal="Balcao", cliente=None, hora=14, custo=True):
    dia = datetime.now(FUSO_LOJA).date() - timedelta(days=dias_atras)
    momento = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=hora)
    linhas = [{
        "product_id": pid,
        "name": NOMES[pid],
        "quantity": quantidade,
        "unit_price": PRECOS[pid],
        "unit_cost": CUSTOS[pid],
        "line_total": round(PRECOS[pid] * quantidade, 2),
    } for pid, quantidade in itens]
    total = round(sum(linha["line_total"] for linha in linhas), 2)
    loja.put("orders", f"p-{numero}", {
        "id": f"p-{numero}", "number": numero, "created_at": momento.isoformat(),
        "channel": canal, "payment_method": "Pix", "customer_name": cliente,
        "discount": 0, "total": total,
        "cost": round(sum(CUSTOS[pid] * q for pid, q in itens), 2) if custo else None,
        "channel_fee": round(total * 0.27, 2) if canal == "iFood" else 0,
        "status": "Concluido", "items": linhas if custo else [],
        "stock_usage": [],
    })


def _loja_movimentada(loja, dias=42):
    numero = 1000
    for atras in range(dias, 0, -1):
        dia = datetime.now(FUSO_LOJA).date() - timedelta(days=atras)
        pedidos_do_dia = 1 if dia.weekday() == 1 else 4
        for indice in range(pedidos_do_dia):
            numero += 1
            itens = [("acai-500", 1), ("super-bowl", 1)] if indice % 2 == 0 else [("acai-300", 1)]
            _pedido(loja, numero, atras, itens, canal="iFood" if indice == 0 else "Balcao")
    return loja


def test_loja_vazia_nao_inventa_lucro_nem_meta(tmp_path):
    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])

    briefing = AdvisorService(loja).brief(30)

    assert briefing["situation"]["orders"] == 0
    assert briefing["situation"]["operating_profit"] is None or briefing["situation"]["revenue"] == 0
    assert "cadastro" in briefing["situation"]["for_owner"].lower()
    assert briefing["sources"]["growth_impact_monthly"] >= 0
    assert "meta" in briefing["dont"][0]["reason"] or "meta" in briefing["dont"][0]["title"]
    assert len(briefing["priorities"]) <= 3
    assert briefing["priorities"][0]["kind"] == "risco"


def test_saldo_negativo_abre_a_reuniao(tmp_path):
    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    acai = loja.get("inventory", "acai")
    acai["quantity"] = -2
    loja.put("inventory", "acai", acai)

    briefing = AdvisorService(loja).brief(30)

    assert briefing["priorities"][0]["id"] == "risco-saldo-negativo"
    assert "negativo" in briefing["priorities"][0]["for_owner"].lower()
    assert briefing["priorities"][0]["view"] == "inventory"


def test_cobertura_baixa_nao_vende_teto_como_lucro(tmp_path):
    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    for numero in range(8):
        _pedido(loja, 2000 + numero, numero, [("acai-500", 1)], custo=False)

    briefing = AdvisorService(loja).brief(30)

    assert briefing["situation"]["complete"] is False
    assert "teto" in briefing["situation"]["for_owner"].lower()
    assert any("teto" in item["title"].lower() or "teto" in item["reason"].lower() for item in briefing["dont"])
    assert any(item["kind"] == "risco" for item in briefing["priorities"])


def test_briefing_tem_no_maximo_tres_prioridades_e_duas_vozes(tmp_path):
    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    _loja_movimentada(loja)

    briefing = AdvisorService(loja).brief(30)

    assert 1 <= len(briefing["priorities"]) <= 3
    kinds = [item["kind"] for item in briefing["priorities"]]
    assert kinds == sorted(kinds, key=["risco", "decisao", "execucao"].index)
    for item in briefing["priorities"]:
        assert item["for_owner"]
        assert item["for_manager"]
        assert item["evidence"]
        assert item["rank"] >= 1
        assert 0 <= item["decision_score"] <= 100
        assert set(item["score_breakdown"]) == {"risk", "urgency", "evidence", "impact"}
    # A soma das oportunidades existe nos sources para auditoria, nao na fala ao dono.
    potencial = f"{briefing['sources']['growth_impact_monthly']:,.2f}"
    assert potencial.replace(",", ".") not in briefing["situation"]["for_owner"]
    assert any("desconte" in item["title"].lower() or "descont" in item["title"].lower() for item in briefing["dont"])
    assert briefing["decision_model"]["id"] == "mcda-evidencia-v1"
    assert "não uma previsão" in briefing["decision_model"]["description"].lower()


def test_decisao_prefere_cadastro_quebrado_a_promocao_sem_virada(tmp_path):
    """Marketplace sem taxa e promoção sem empate: a reunião corrige a medição."""

    from app.schemas import ChannelCreate, OrderCreate, OrderLine
    from app.services import ERPService

    loja = DemoStore(tmp_path / "store.json")
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    erp = ERPService(loja)
    erp.create_channel(ChannelCreate(name="99Food", fee_percent=0.0))
    for indice in range(6):
        erp.create_order(OrderCreate(
            items=[OrderLine(product_id="acai-500", quantity=1)],
            channel="99Food",
        ))

    briefing = AdvisorService(loja).brief(30)
    decisoes = [item for item in briefing["priorities"] if item["kind"] == "decisao"]
    assert decisoes, "com canal distorcido a reunião precisa de uma decisão"
    assert "taxa" in decisoes[0]["title"].lower() or "99" in decisoes[0]["for_owner"]
