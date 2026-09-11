"""O rendimento da barra, deduzido do que a loja já registra.

O gestor não sabe quantas barras fazem um açaí de 500 ml, e não vai parar para
medir a cuba a cada batida. Mas ele não precisa: os dois lados da conta já
passam pelo sistema por outros motivos. O quilo que entrou está na compra,
porque a loja precisa saber quanto gastou; o mililitro que saiu está na venda,
porque a loja precisa faturar.

    rendimento = mililitros de base vendidos ÷ quilos de barra consumidos

É melhor que a batida declarada, e por uma razão que importa: já traz dentro o
que a batida esconde — o que ficou na cuba, o que derreteu, a mão pesada de quem
serve. A batida diz o que deveria ter saído; esta conta diz o que saiu pela
porta.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import InventoryPurchase, OrderCreate, OrderLine
from app.services import ERPService, YieldService
from app.services.erp import FUSO_LOJA


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for colecao in ("orders", "inventory_movements"):
        for linha in store.list(colecao):
            store.delete(colecao, linha["id"])
    erp = ERPService(store)
    # A barra que entra, em quilo, e a base que o copo consome, em mililitro.
    store.put("inventory", "barra", {
        "id": "barra", "name": "barra 12%", "unit": "kg", "quantity": 0.0,
        "minimum_quantity": 5.0, "unit_cost": 18.0, "category": "Polpas",
    })
    store.put("inventory", "base", {
        "id": "base", "name": "Açaí", "unit": "ml", "quantity": 0.0,
        "minimum_quantity": 1000.0, "unit_cost": 0.0084, "category": "Polpas",
    })
    for identificador, nome, preco, ml in [
        ("copo-300", "Açaí 300 ml", 13.0, 200.0),
        ("copo-500", "Açaí 500 ml", 18.0, 400.0),
    ]:
        store.put("products", identificador, {
            "id": identificador, "name": nome, "price": preco, "category": "Acai",
            "active": True, "line": "Classico",
            "recipe": [{"inventory_id": "base", "quantity": ml}],
        })
    return store, erp, YieldService(store)


def _comprar(erp, kg: float, dias_atras: int = 30) -> None:
    quando = datetime.now(FUSO_LOJA) - timedelta(days=dias_atras)
    erp.register_inventory_purchase("barra", InventoryPurchase(
        quantity=kg, unit_cost=18.0, supplier="Fornecedor", received_at=quando,
    ))


def _sobrou(store, kg: float) -> None:
    """O que a contagem encontrou na prateleira.

    Sem ela o sistema não sabe quanto saiu: a barra vira base na batida, e a
    batida não é registrada. Contar o que sobra é a única ação que a loja
    precisa acrescentar — uma vez por semana, no mesmo lugar onde já confere o
    resto do estoque.
    """

    item = store.get("inventory", "barra")
    item["quantity"] = kg
    store.put("inventory", "barra", item)


def _vender(erp, produto: str, copos: int, dias_atras: int = 10) -> None:
    """Um pedido não leva cinquenta copos: a venda vira vários, como na loja."""

    quando = datetime.now(FUSO_LOJA) - timedelta(days=dias_atras)
    restante = copos
    while restante > 0:
        lote = min(10, restante)
        erp.create_order(OrderCreate(items=[OrderLine(product_id=produto, quantity=lote)], occurred_at=quando))
        restante -= lote


# ----------------------------------------------------------------------
# A deducao
# ----------------------------------------------------------------------


def test_o_rendimento_sai_da_compra_e_da_venda_sem_ninguem_medir(loja):
    store, erp, rendimento = loja
    _comprar(erp, 40.0)
    _sobrou(store, 0.0)
    # 200 copos de 500 ml = 80.000 ml de base, saídos de 40 kg de barra.
    _vender(erp, "copo-500", 200)

    resultado = rendimento.acai_yield(days=90)

    assert resultado["ready"] is True
    assert resultado["kg"]["consumido"] == 40.0
    assert resultado["sales"]["ml"] == 80000.0
    assert resultado["ml_per_kg"] == 2000.0
    assert resultado["litres_per_kg"] == 2.0


def test_o_que_sobrou_no_estoque_nao_conta_como_consumido(loja):
    """Consumo é o que saiu, não o que entrou: senão o rendimento sai pela metade."""

    store, erp, rendimento = loja
    _comprar(erp, 50.0)
    _vender(erp, "copo-500", 100)
    # A contagem achou 10 kg na prateleira: só 40 foram consumidos.
    _sobrou(store, 10.0)

    resultado = rendimento.acai_yield(days=90)

    assert resultado["kg"]["consumido"] == 40.0
    assert resultado["ml_per_kg"] == 1000.0


def test_a_conta_nao_depende_da_ficha_da_barra(loja):
    """Viesse do movimento que a venda gera, a ficha confirmaria a si mesma.

    Vindo do saldo e das compras, o número é independente dela — e é por isso
    que ele serve para corrigi-la.
    """

    store, erp, rendimento = loja
    # Uma ficha absurda, como a que a loja tem hoje: 20 kg de barra por copo.
    produto = store.get("products", "copo-500")
    produto["recipe"].append({"inventory_id": "barra", "quantity": 20.0})
    store.put("products", produto["id"], produto)
    _comprar(erp, 40.0)
    _vender(erp, "copo-500", 100)
    _sobrou(store, 0.0)

    resultado = rendimento.acai_yield(days=90)

    # 100 copos x 400 ml = 40.000 ml, de 40 kg. A ficha da barra não entrou.
    assert resultado["ml_per_kg"] == 1000.0


def test_pedido_sem_itens_nao_vira_zero_e_a_falta_e_declarada(loja):
    """928 pedidos importados sem itens não dizem quantos mililitros saíram."""

    store, erp, rendimento = loja
    _comprar(erp, 40.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 200)
    store.put("orders", "importado", {
        "id": "importado", "created_at": datetime.now(FUSO_LOJA).isoformat(),
        "total": 20.0, "status": "Concluido", "items": [], "channel": "Balcao",
    })

    resultado = rendimento.acai_yield(days=90)

    assert resultado["sales"]["orders_without_items"] == 1
    # O rendimento não afundou por causa dele.
    assert resultado["ml_per_kg"] == 2000.0


# ----------------------------------------------------------------------
# A pergunta do gestor: 20 kg rendem quantos copos?
# ----------------------------------------------------------------------


def test_vinte_quilos_rendem_quantos_copos_de_cada_tamanho(loja):
    store, erp, rendimento = loja
    _comprar(erp, 40.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 200)

    projecao = rendimento.acai_yield(days=90, project_kg=20.0)["projection"]

    assert projecao["kg"] == 20.0
    # 20 kg x 2.000 ml/kg = 40.000 ml de base.
    assert projecao["ml"] == 40000
    por_nome = {linha["name"]: linha for linha in projecao["cups"]}
    assert por_nome["Açaí 300 ml"]["cups"] == 200   # 40.000 / 200 ml
    assert por_nome["Açaí 500 ml"]["cups"] == 100   # 40.000 / 400 ml
    # E quanto isso vira em faturamento, que é a pergunta seguinte.
    assert por_nome["Açaí 500 ml"]["revenue"] == 1800.0


def test_a_projecao_ordena_do_copo_menor_para_o_maior(loja):
    store, erp, rendimento = loja
    _comprar(erp, 40.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 200)

    copos = rendimento.acai_yield(days=90)["projection"]["cups"]

    assert [linha["base_ml"] for linha in copos] == [200.0, 400.0]


# ----------------------------------------------------------------------
# Quando nao da para responder
# ----------------------------------------------------------------------


def test_amostra_pequena_nao_devolve_numero_fragil(loja):
    """Rendimento de três dias vira decisão de compra para o mês inteiro."""

    store, erp, rendimento = loja
    _comprar(erp, 2.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 4)

    resultado = rendimento.acai_yield(days=90)

    assert resultado["ready"] is False
    assert resultado["ml_per_kg"] is None
    assert any("kg de barra" in bloqueio["title"] for bloqueio in resultado["blockers"])
    assert any("copo" in bloqueio["title"] for bloqueio in resultado["blockers"])


def test_o_bloqueio_diz_onde_registrar_a_compra(loja):
    store, erp, rendimento = loja

    resultado = rendimento.acai_yield(days=90)

    falta = next(b for b in resultado["blockers"] if "kg de barra" in b["title"])
    assert "Estoque → Entrou" in falta["description"]


def test_a_confianca_cresce_com_a_amostra(loja):
    store, erp, rendimento = loja
    _comprar(erp, 12.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 70)
    assert rendimento.acai_yield(days=90)["confidence"] == "média"

    _comprar(erp, 30.0)
    _sobrou(store, 0.0)
    _vender(erp, "copo-500", 150)
    assert rendimento.acai_yield(days=90)["confidence"] == "alta"


def test_sem_barra_cadastrada_o_calculo_diz_o_que_falta(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for item in store.list("inventory"):
        store.delete("inventory", item["id"])

    resultado = YieldService(store).acai_yield(days=90)

    assert resultado["ready"] is False
    assert "barra" in resultado["blockers"][0]["title"]
