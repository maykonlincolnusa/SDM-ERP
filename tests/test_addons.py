"""Complementos: o que o cliente pede por cima do copo.

O caso que estes testes cobrem e o do balcao: um acai de 500 ml sai com banana
e calda de chocolate. A ficha tecnica responde pelo copo, o complemento
responde pelo resto — e as duas coisas precisam cair no mesmo estoque, no mesmo
custo e no mesmo lucro operacional, senao o pedido fica registrado por menos do
que custou.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    AddonCreate,
    AddonUpdate,
    OrderAddon,
    OrderCreate,
    OrderLine,
    OrderUpdate,
)
from app.services import ERPService, ForecastService
from app.services.forecast import FUSO_LOJA


@pytest.fixture
def service(tmp_path) -> ERPService:
    return ERPService(DemoStore(tmp_path / "store.json"))


def saldo(service: ERPService, inventory_id: str) -> float:
    return next(item for item in service.inventory() if item["id"] == inventory_id)["quantity"]


def test_acai_com_banana_e_calda_baixa_os_dois_do_estoque(service):
    """O exemplo do balcao: 500 ml com banana e calda de chocolate."""

    banana_antes = saldo(service, "banana")
    calda_antes = saldo(service, "calda-chocolate")
    acai_antes = saldo(service, "acai")

    pedido = service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=2, addons=[
            OrderAddon(addon_id="addon-banana"),
            OrderAddon(addon_id="addon-calda-chocolate"),
        ]),
    ]))

    # A polpa sai pela receita (0,46 kg por copo), a banana sai pelos dois
    # caminhos: 0,1 kg da receita mais 0,04 kg da porcao escolhida.
    assert saldo(service, "acai") == pytest.approx(acai_antes - 0.92)
    assert saldo(service, "banana") == pytest.approx(banana_antes - (0.1 + 0.04) * 2)
    assert saldo(service, "calda-chocolate") == pytest.approx(calda_antes - 0.04)
    # 2 x (22,00 do copo + 2,00 da banana + 3,00 da calda)
    assert pedido["total"] == 54.0
    linha = pedido["items"][0]
    assert [chosen["name"] for chosen in linha["addons"]] == ["Banana", "Calda de chocolate"]
    assert linha["addons_price"] == 5.0


def test_custo_do_pedido_inclui_o_complemento(service):
    """Sem isso a margem do pedido com calda pareceria igual a do copo puro."""

    puro = service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))
    completo = service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-calda-chocolate")]),
    ]))

    assert completo["cost"] > puro["cost"]
    # 0,02 L de calda a R$ 18,40 o litro.
    assert completo["cost"] - puro["cost"] == pytest.approx(0.37, abs=0.01)


def test_porcao_dupla_multiplica_pelas_unidades(service):
    """Dois copos com calda dupla sao quatro doses fora da prateleira."""

    antes = saldo(service, "calda-chocolate")

    pedido = service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=2, addons=[OrderAddon(addon_id="addon-calda-chocolate", quantity=2)]),
    ]))

    assert saldo(service, "calda-chocolate") == pytest.approx(antes - 0.08)
    assert pedido["total"] == 2 * (22.0 + 2 * 3.0)


def test_cancelar_pedido_devolve_o_complemento(service):
    antes = saldo(service, "calda-chocolate")
    pedido = service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-calda-chocolate")]),
    ]))

    service.cancel_order(pedido["id"], "Cliente desistiu")

    assert saldo(service, "calda-chocolate") == pytest.approx(antes)


def test_corrigir_o_pedido_troca_o_complemento_no_estoque(service):
    """Trocar calda por granola tem de devolver uma e tirar a outra."""

    calda_antes = saldo(service, "calda-chocolate")
    granola_antes = saldo(service, "granola")
    pedido = service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-calda-chocolate")]),
    ]))

    service.update_order(pedido["id"], OrderUpdate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-granola")]),
    ]))

    assert saldo(service, "calda-chocolate") == pytest.approx(calda_antes)
    # A granola tambem esta na receita (0,055 kg), entao so a porcao de 0,03 kg
    # e a diferenca em relacao ao pedido sem complemento.
    assert saldo(service, "granola") == pytest.approx(granola_antes - 0.055 - 0.03)


def test_complemento_inativo_nao_pode_ser_vendido(service):
    service.update_addon("addon-banana", AddonUpdate(active=False))

    with pytest.raises(ValueError, match="Complemento indisponível"):
        service.create_order(OrderCreate(items=[
            OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-banana")]),
        ]))


def test_complemento_precisa_de_insumo_existente(service):
    with pytest.raises(ValueError, match="não existe no estoque"):
        service.create_addon(AddonCreate(name="Paçoca", inventory_id="pacoca", quantity=0.02, price=2.0))


def test_complemento_vendido_e_desativado_em_vez_de_apagado(service):
    """O pedido antigo precisa continuar explicando o que saiu do estoque."""

    service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-banana")]),
    ]))

    resultado = service.delete_addon("addon-banana")

    assert resultado["status"] == "inativado"
    assert service.store.get("addons", "addon-banana")["active"] is False


def test_complemento_nunca_vendido_e_removido(service):
    novo = service.create_addon(AddonCreate(name="Paçoca", inventory_id="granola", quantity=0.02, price=2.0))

    assert service.delete_addon(novo["id"])["status"] == "excluido"
    assert service.store.get("addons", novo["id"]) is None


def test_insumo_de_complemento_nao_pode_ser_apagado(service):
    """Sem insumo o complemento venderia registrando custo zero."""

    with pytest.raises(ValueError, match="porção do complemento"):
        service.delete_inventory_item("calda-chocolate")


def test_cadastro_mostra_quantas_porcoes_o_saldo_aguenta(service):
    calda = next(row for row in service.addons() if row["id"] == "addon-calda-chocolate")

    assert calda["inventory_name"] == "Calda de chocolate"
    assert calda["portions_available"] == 205  # 4,1 L / 0,02 L por porcao
    assert calda["cost"] == pytest.approx(0.368)
    assert calda["contribution"] == pytest.approx(2.63, abs=0.01)


def test_cruzamento_separa_receita_de_complemento(service):
    service.create_order(OrderCreate(items=[
        OrderLine(product_id="acai-500", quantity=1, addons=[OrderAddon(addon_id="addon-banana")]),
    ]))

    cruzamento = service.ingredient_crossing(30)
    banana = next(row for row in cruzamento["items"] if row["inventory_id"] == "banana")

    assert banana["addon_quantity"] == pytest.approx(0.04)
    # A semente ja tem vendas de 500 ml sem complemento, entao a parte da
    # receita e maior do que a deste unico pedido.
    assert banana["recipe_quantity"] >= 0.1
    assert banana["quantity"] == pytest.approx(banana["recipe_quantity"] + banana["addon_quantity"])
    assert cruzamento["totals"]["addon_revenue"] == 2.0

    complemento = next(row for row in cruzamento["addons"] if row["addon_id"] == "addon-banana")
    assert complemento["portions"] == 1
    assert complemento["contribution"] == pytest.approx(2.0 - 0.04 * 6.9, abs=0.01)


def test_cruzamento_avisa_quando_o_pedido_nao_tem_itens(service):
    service.store.put("orders", "importado", {
        "id": "importado", "number": 9001, "created_at": service.orders()[0]["created_at"],
        "channel": "iFood", "payment_method": "Credito", "discount": 0,
        "total": 120.0, "cost": None, "status": "Concluido", "items": [],
    })

    cruzamento = service.ingredient_crossing(30)

    assert cruzamento["coverage"] < 100
    assert any("não têm os itens vendidos" in blocker for blocker in cruzamento["blockers"])


def test_previsao_de_lucro_conta_o_complemento(tmp_path):
    """O lucro previsto tem de crescer com a adesao observada, nao com um chute."""

    store = DemoStore(tmp_path / "store.json")
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    # A previsao so abre depois de DIAS_MINIMOS dias de historico. Cada dia
    # vende um copo de 500 ml com uma dose de calda, entao a taxa de adesao
    # observada e de uma porcao por unidade.
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(28, 0, -1):
        momento = datetime.combine(hoje - timedelta(days=atras), datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        store.put("orders", f"h-{atras}", {
            "id": f"h-{atras}", "number": 2000 + atras, "created_at": momento.isoformat(),
            "channel": "Balcao", "payment_method": "Pix", "customer_name": None, "discount": 0,
            "total": 25.0, "cost": 11.37, "status": "Concluido",
            "items": [{
                "product_id": "acai-500", "name": "Açaí 500 ml", "quantity": 1,
                "unit_price": 22.0, "unit_cost": 11.37, "recipe_cost": 11.0,
                "addons_price": 3.0, "addons_cost": 0.37, "line_total": 25.0,
                "addons": [{
                    "addon_id": "addon-calda-chocolate", "name": "Calda de chocolate",
                    "inventory_id": "calda-chocolate", "quantity": 1, "portion": 0.02,
                    "unit_price": 3.0, "unit_cost": 0.368,
                }],
            }],
        })

    plano = ForecastService(store).forecast(7)["purchase_plan"]

    assert plano["available"]
    calda = next(row for row in plano["items"] if row["inventory_id"] == "calda-chocolate")
    assert calda["needed"] > 0, "a calda prevista precisa entrar na lista de compra"
    projecao = plano["operating_projection"]
    assert projecao["available"]
    assert projecao["totals"]["addons_revenue"] > 0
    assert projecao["totals"]["addons_cost"] > 0
    # Receita do copo mais a da calda: sem o complemento o total ficaria so nos
    # 22,00 por unidade prevista.
    linha = next(row for row in projecao["products"] if row["product_id"] == "acai-500")
    assert linha["revenue"] > 22.0 * linha["units"]
