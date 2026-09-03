"""Retirada de insumo pela equipe.

A venda ja baixava o estoque sozinha pela ficha tecnica. O que nao tinha
registro era o insumo que sai sem venda: a producao da manha, a degustacao, a
reposicao do balcao. Sem esse lancamento o saldo do sistema fica maior que o da
prateleira, e a diferenca so aparece na contagem seguinte — ja transformada em
"perda", sem ninguem saber o que de fato aconteceu.

Os testes aqui cobrem as duas coisas que fazem esse registro valer: a retirada
e atomica (ou sai tudo, ou nao sai nada) e ela aparece separada da perda em
todo lugar que le o movimento.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    InventoryAdjustment,
    InventoryWithdrawal,
    InventoryWithdrawalLine,
    MovementUpdate,
    OrderCreate,
    OrderLine,
)
from app.services import ERPService, PlanningService
from app.services.erp import FUSO_LOJA, WITHDRAWAL_KIND


@pytest.fixture
def service(tmp_path) -> ERPService:
    return ERPService(DemoStore(tmp_path / "store.json"))


def _saldo(service: ERPService, inventory_id: str) -> float:
    return next(item for item in service.inventory() if item["id"] == inventory_id)["quantity"]


def _retirada(**kwargs) -> InventoryWithdrawal:
    itens = kwargs.pop("items")
    return InventoryWithdrawal(
        items=[InventoryWithdrawalLine(inventory_id=item, quantity=quantidade) for item, quantidade in itens],
        **kwargs,
    )


# ----------------------------------------------------------------------
# Registro
# ----------------------------------------------------------------------


def test_retirada_baixa_todos_os_itens_e_guarda_quem_levou(service):
    acai_antes = _saldo(service, "acai")
    granola_antes = _saldo(service, "granola")

    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("acai", 2), ("granola", 0.5)], responsible="Maria", reason="Produção da manhã")
    )

    assert _saldo(service, "acai") == acai_antes - 2
    assert _saldo(service, "granola") == granola_antes - 0.5
    # 2 kg a 22,50 mais 0,5 kg a 27,00.
    assert retirada["total_cost"] == 58.5
    assert {linha["inventory_id"] for linha in retirada["items"]} == {"acai", "granola"}

    movimentos = [
        movimento for movimento in service.store.list("inventory_movements")
        if movimento.get("withdrawal_id") == retirada["id"]
    ]
    assert len(movimentos) == 2
    assert all(movimento["kind"] == WITHDRAWAL_KIND for movimento in movimentos)
    assert all(movimento["responsible"] == "Maria" for movimento in movimentos)
    assert all(movimento["quantity_delta"] < 0 for movimento in movimentos)


def test_retirada_guarda_o_custo_do_dia_e_nao_o_custo_de_depois(service):
    """O valor da retirada nao pode mudar quando a proxima compra mexer na media."""

    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("granola", 1)], responsible="João")
    )
    movimento = next(
        linha for linha in service.store.list("inventory_movements")
        if linha.get("withdrawal_id") == retirada["id"]
    )

    assert movimento["total_cost"] == 27.0
    assert movimento["unit_cost"] == 27.0


def test_retirada_sem_saldo_nao_baixa_nenhum_item(service):
    acai_antes = _saldo(service, "acai")
    morango_antes = _saldo(service, "morango")

    with pytest.raises(ValueError, match="Saldo insuficiente"):
        service.register_inventory_withdrawal(
            _retirada(items=[("acai", 1), ("morango", 99)], responsible="Maria")
        )

    # O acai tinha saldo de sobra: se ele saisse, a retirada teria sido parcial
    # e a prateleira ficaria diferente do que qualquer relatorio mostraria.
    assert _saldo(service, "acai") == acai_antes
    assert _saldo(service, "morango") == morango_antes
    assert not [
        movimento for movimento in service.store.list("inventory_movements")
        if movimento.get("kind") == WITHDRAWAL_KIND
    ]


def test_linhas_repetidas_do_mesmo_insumo_somam_antes_da_conferencia(service):
    """Duas linhas de 1 kg com saldo de 1,4 kg pedem 2 kg, e nao 1 kg duas vezes."""

    with pytest.raises(ValueError, match="Saldo insuficiente"):
        service.register_inventory_withdrawal(
            _retirada(items=[("morango", 1), ("morango", 1)], responsible="Maria")
        )

    assert _saldo(service, "morango") == 1.4


def test_linhas_repetidas_viram_um_unico_movimento(service):
    granola_antes = _saldo(service, "granola")

    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("granola", 0.4), ("granola", 0.2)], responsible="Maria")
    )

    assert len(retirada["items"]) == 1
    assert retirada["items"][0]["quantity"] == 0.6
    assert _saldo(service, "granola") == round(granola_antes - 0.6, 3)


def test_retirada_de_insumo_inexistente_e_recusada(service):
    with pytest.raises(ValueError, match="não encontrado"):
        service.register_inventory_withdrawal(
            _retirada(items=[("insumo-que-nao-existe", 1)], responsible="Maria")
        )


# ----------------------------------------------------------------------
# Desfazer
# ----------------------------------------------------------------------


def test_desfazer_devolve_todo_o_saldo_e_apaga_os_lancamentos(service):
    acai_antes = _saldo(service, "acai")
    granola_antes = _saldo(service, "granola")
    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("acai", 2), ("granola", 0.5)], responsible="Maria")
    )

    resultado = service.undo_inventory_withdrawal(retirada["id"])

    assert resultado["items_restored"] == 2
    assert _saldo(service, "acai") == acai_antes
    assert _saldo(service, "granola") == granola_antes
    assert not [
        movimento for movimento in service.store.list("inventory_movements")
        if movimento.get("withdrawal_id") == retirada["id"]
    ]


def test_desfazer_duas_vezes_e_recusado(service):
    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("granola", 0.5)], responsible="Maria")
    )
    service.undo_inventory_withdrawal(retirada["id"])

    with pytest.raises(ValueError, match="não encontrada"):
        service.undo_inventory_withdrawal(retirada["id"])


def test_linha_de_retirada_nao_se_corrige_sozinha(service):
    """Corrigir uma linha deixaria o resto da retirada com saldo devolvido pela metade."""

    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("acai", 2), ("granola", 0.5)], responsible="Maria")
    )
    movimento = next(
        linha for linha in service.store.list("inventory_movements")
        if linha.get("withdrawal_id") == retirada["id"]
    )

    with pytest.raises(ValueError, match="retirada"):
        service.update_movement(movimento["id"], MovementUpdate(quantity_delta=-1))


# ----------------------------------------------------------------------
# Onde a retirada aparece
# ----------------------------------------------------------------------


def test_consumo_separa_retirada_de_perda(service):
    service.register_inventory_withdrawal(
        _retirada(items=[("granola", 1)], responsible="Maria")
    )
    service.adjust_inventory("banana", InventoryAdjustment(quantity_delta=-1, reason="Fruta passada"))

    consumo = service.consumption(7)
    granola = next(linha for linha in consumo["items"] if linha["inventory_id"] == "granola")
    banana = next(linha for linha in consumo["items"] if linha["inventory_id"] == "banana")

    assert granola["withdrawn"] == 1
    assert granola["lost"] == 0
    assert banana["lost"] == 1
    assert banana["withdrawn"] == 0
    assert consumo["totals"]["withdrawal_cost"] == 27.0
    assert consumo["totals"]["loss_cost"] == 6.9
    # A retirada entra no total do que saiu, mas nao infla a fatia de perda.
    assert consumo["totals"]["total_cost"] == round(
        consumo["totals"]["sale_cost"] + 27.0 + 6.9, 2
    )


def test_historico_do_consumo_marca_a_retirada_como_tal(service):
    retirada = service.register_inventory_withdrawal(
        _retirada(items=[("granola", 1)], responsible="Maria", note="Turno da tarde")
    )

    linha = next(
        row for row in service.consumption(7)["history"]
        if row.get("withdrawal_id") == retirada["id"]
    )

    assert linha["kind"] == WITHDRAWAL_KIND
    assert linha["responsible"] == "Maria"
    assert linha["note"] == "Turno da tarde"
    # A tela oferece desfazer a retirada inteira, nunca o lapis da linha.
    assert linha["editable"] is False


def test_fechamento_do_dia_separa_retirada_de_perda(service):
    service.register_inventory_withdrawal(
        _retirada(items=[("granola", 1)], responsible="Maria")
    )
    service.adjust_inventory("banana", InventoryAdjustment(quantity_delta=-1, reason="Fruta passada"))

    resumo = service.sales_day()["totals"]

    assert resumo["withdrawal_cost"] == 27.0
    assert resumo["loss_cost"] == 6.9


def test_resultado_operacional_conta_retirada_fora_da_perda(service):
    """A retirada custa, mas nao e perda: somar as duas esconderia a quebra real."""

    service.register_inventory_withdrawal(
        _retirada(items=[("granola", 1)], responsible="Maria")
    )

    totais = service.operating_result(30)["totals"]

    assert totais["inventory_withdrawals"] == 27.0
    assert totais["inventory_losses"] == 0.0
    # E custo mesmo assim: o insumo saiu e nao volta.
    assert totais["total_operating_cost"] >= 27.0


def test_plano_de_producao_conta_a_retirada_como_consumo(tmp_path):
    """Retirada e consumo previsivel: ignora-la faz o plano mandar comprar menos."""

    store = DemoStore(tmp_path / "store.json")
    for movimento in store.list("inventory_movements"):
        store.delete("inventory_movements", movimento["id"])
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    erp, planning = ERPService(store), PlanningService(store)

    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(20, 0, -1):
        quando = datetime.combine(hoje - timedelta(days=atras), datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=3)], occurred_at=quando))

    sem_retirada = planning.plan(days=30)
    erp.register_inventory_withdrawal(_retirada(items=[("acai", 3)], responsible="Maria"))
    com_retirada = planning.plan(days=30)

    def _consumo(plano: dict) -> float:
        return next(linha for linha in plano["purchase"] if linha["inventory_id"] == "acai")["daily_average"]

    assert _consumo(com_retirada) > _consumo(sem_retirada)


def test_lucro_previsto_desconta_a_retirada(tmp_path):
    """A DRE futura tem de descontar o mesmo que a DRE realizada desconta.

    Sem isso o lucro projetado fica otimista exatamente no valor do insumo que
    a equipe consome todo dia — que e o custo que este registro existe para
    tornar visivel.
    """

    from app.services import ForecastService

    store = DemoStore(tmp_path / "store.json")
    for colecao in ("orders", "inventory_movements"):
        for linha in store.list(colecao):
            store.delete(colecao, linha["id"])
    erp, forecast = ERPService(store), ForecastService(store)

    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(30, 0, -1):
        quando = datetime.combine(hoje - timedelta(days=atras), datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        erp.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=3)], occurred_at=quando))
        for item in erp.inventory():
            erp.adjust_inventory(item["id"], InventoryAdjustment(quantity_delta=20, reason="Reposição de teste"))

    sem = forecast.forecast(7)["purchase_plan"]["operating_projection"]
    erp.register_inventory_withdrawal(_retirada(items=[("granola", 2)], responsible="Maria"))
    com = forecast.forecast(7)["purchase_plan"]["operating_projection"]

    assert sem["totals"]["withdrawal_cost"] == 0.0
    # 2 kg a 27,00 diluidos em 30 dias de historico, projetados para 7 dias.
    assert com["totals"]["withdrawal_cost"] == round(54.0 / 30 * 7, 2)
    assert com["totals"]["operating_profit"] < sem["totals"]["operating_profit"]


# ----------------------------------------------------------------------
# A excecao: registrar mesmo sem saldo
# ----------------------------------------------------------------------


def test_sem_confirmacao_a_falta_de_saldo_ensina_a_saida(service):
    with pytest.raises(ValueError, match="Confirme na tela"):
        service.register_inventory_withdrawal(
            _retirada(items=[("morango", 99)], responsible="Maria")
        )


def test_confirmacao_explicita_registra_e_deixa_o_saldo_negativo(service):
    """O insumo ja saiu da prateleira; recusar so faz a saida sumir do sistema."""

    retirada = service.register_inventory_withdrawal(
        InventoryWithdrawal(
            items=[InventoryWithdrawalLine(inventory_id="morango", quantity=2)],
            responsible="Maria",
            allow_negative=True,
        )
    )

    assert _saldo(service, "morango") == -0.6
    assert retirada["below_balance"] == ["Morango"]
    movimento = next(
        linha for linha in service.store.list("inventory_movements")
        if linha.get("withdrawal_id") == retirada["id"]
    )
    # Sem essa marca o negativo apareceria no estoque sem nada explicando.
    assert movimento["below_balance"] is True


def test_confirmacao_nao_marca_a_linha_que_tinha_saldo(service):
    retirada = service.register_inventory_withdrawal(
        InventoryWithdrawal(
            items=[
                InventoryWithdrawalLine(inventory_id="morango", quantity=2),
                InventoryWithdrawalLine(inventory_id="acai", quantity=1),
            ],
            responsible="Maria",
            allow_negative=True,
        )
    )

    assert retirada["below_balance"] == ["Morango"]
    marcadas = {linha["name"]: linha["below_balance"] for linha in retirada["items"]}
    assert marcadas == {"Morango": True, "Base de açaí": False}


def test_desfazer_devolve_o_saldo_negativo_ao_que_era(service):
    antes = _saldo(service, "morango")
    retirada = service.register_inventory_withdrawal(
        InventoryWithdrawal(
            items=[InventoryWithdrawalLine(inventory_id="morango", quantity=2)],
            responsible="Maria",
            allow_negative=True,
        )
    )

    service.undo_inventory_withdrawal(retirada["id"])

    assert _saldo(service, "morango") == antes


# ----------------------------------------------------------------------
# Quebra no custo do produto
# ----------------------------------------------------------------------


def test_dossie_separa_o_custo_da_ficha_do_custo_com_quebra(service):
    """Preco decidido pelo custo da ficha e preco decidido por um custo que a loja nunca teve."""

    from app.services import CostingService

    limpo = CostingService(service.store).dossier("acai-500", days=30)
    assert limpo["cost"]["with_shrinkage"] == limpo["cost"]["direct"]
    assert limpo["cost"]["shrinkage"] == 0.0

    # A loja vende, e parte do acai sai sem virar venda.
    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=10)]))
    service.register_inventory_withdrawal(_retirada(items=[("acai", 2)], responsible="Maria"))

    dossie = CostingService(service.store).dossier("acai-500", days=30)

    assert dossie["cost"]["with_shrinkage"] > dossie["cost"]["direct"]
    assert dossie["cost"]["shrinkage"] > 0
    # A escada de preco parte do custo real, senao o preco-alvo sai baixo.
    assert dossie["price_ladder"][0]["price"] > limpo["price_ladder"][0]["price"]
    linha_acai = next(row for row in dossie["recipe"] if row["inventory_id"] == "acai")
    assert linha_acai["shrinkage_rate"] > 0
    assert linha_acai["cost_with_shrinkage"] > linha_acai["cost"]


def test_quebra_nao_encarece_o_insumo_que_nao_se_perde(service):
    """Ratear a quebra da fruta sobre o copo descartavel encareceria o produto errado."""

    from app.services import CostingService

    service.create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=10)]))
    service.register_inventory_withdrawal(_retirada(items=[("acai", 2)], responsible="Maria"))

    dossie = CostingService(service.store).dossier("acai-500", days=30)
    copo = next(row for row in dossie["recipe"] if "Copo" in row["name"])

    assert copo["shrinkage_rate"] == 0
    assert copo["cost_with_shrinkage"] == copo["cost"]
