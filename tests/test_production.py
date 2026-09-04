"""Fabricacao: a barra de acai vira base pronta.

Este e o unico movimento do sistema que tira de um item e poe em outro. Todo o
resto do estoque so soma ou so subtrai, e por isso a fabricacao e a operacao com
mais chance de furar a conta em silencio: se o custo da barra for contado aqui
*e* de novo quando o copo for vendido, o mesmo acai e cobrado duas vezes e a
margem do cardapio inteiro fica menor do que e.

A maior parte dos testes aqui prova exatamente isso — que a fabricacao nao
aparece como perda, nem como retirada, nem como quebra — e que o custo do quilo
de base sai do que foi realmente usado, e nao da ficha.
"""

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    InventoryItemCreate,
    InventoryPurchase,
    MovementUpdate,
    ProductionLine,
    ProductionRecipeCreate,
    ProductionRecipeUpdate,
    ProductionRun,
)
from app.services import ERPService, PlanningService


@pytest.fixture
def loja(tmp_path):
    """Loja com as duas barras cadastradas e compradas, como a de verdade."""

    store = DemoStore(tmp_path / "store.json")
    for movimento in store.list("inventory_movements"):
        store.delete("inventory_movements", movimento["id"])
    erp = ERPService(store)
    for nome, custo_da_barra in (("Barra de açaí 8%", 42.0), ("Barra de açaí 12%", 58.0)):
        erp.create_inventory_item(InventoryItemCreate(
            name=nome, unit="kg", quantity=0, minimum_quantity=20, unit_cost=0,
            category="Açaí", shelf_life_days=180, pack_unit="barra", pack_size=2.8,
        ))
        item = next(linha for linha in erp.inventory() if linha["name"] == nome)
        erp.register_inventory_purchase(item["id"], InventoryPurchase(
            purchase_quantity=10, purchase_unit="barra",
            units_per_purchase=2.8, purchase_unit_cost=custo_da_barra,
        ))
    return erp


def _item(erp, nome):
    return next(linha for linha in erp.inventory() if linha["name"] == nome)


def _bate(erp, nome_da_barra="Barra de açaí 12%", entrada=8.4, saida=10.0, **kwargs):
    barra = _item(erp, nome_da_barra)
    return erp.register_production(ProductionRun(
        inputs=[ProductionLine(inventory_id=barra["id"], quantity=entrada)],
        output_inventory_id="acai", output_quantity=saida,
        responsible="Maria", **kwargs,
    ))


# ----------------------------------------------------------------------
# A unidade de manuseio
# ----------------------------------------------------------------------


def test_a_barra_e_cadastrada_em_quilo_e_contada_em_barra(loja):
    """A equipe conta em barra; custo e ficha tecnica falam em quilo."""

    barra = _item(loja, "Barra de açaí 12%")

    assert barra["unit"] == "kg"
    assert barra["pack_unit"] == "barra"
    assert barra["pack_size"] == 2.8
    # 10 barras de 2,8 kg a R$ 58 a barra.
    assert barra["quantity"] == 28
    assert barra["unit_cost"] == pytest.approx(58 / 2.8, abs=0.001)


def test_unidade_de_manuseio_pela_metade_e_recusada():
    with pytest.raises(ValueError, match="nome e o tamanho"):
        InventoryItemCreate(
            name="Barra solta", unit="kg", quantity=0, minimum_quantity=1,
            unit_cost=10, pack_unit="barra",
        )


# ----------------------------------------------------------------------
# A ficha de fabricacao
# ----------------------------------------------------------------------


def test_ficha_guarda_o_que_entra_e_o_que_sai(loja):
    barra = _item(loja, "Barra de açaí 12%")

    ficha = loja.create_production_recipe(ProductionRecipeCreate(
        name="Batida de 12%",
        inputs=[ProductionLine(inventory_id=barra["id"], quantity=8.4)],
        output_inventory_id="acai", output_quantity=10.0,
    ))

    assert ficha["output_quantity"] == 10.0
    assert ficha["inputs"][0]["quantity"] == 8.4
    assert loja.production_recipes()[0]["id"] == ficha["id"]


def test_ficha_nao_pode_produzir_o_proprio_insumo(loja):
    with pytest.raises(ValueError, match="não pode estar também entre os insumos"):
        ProductionRecipeCreate(
            name="Circular", inputs=[ProductionLine(inventory_id="acai", quantity=1)],
            output_inventory_id="acai", output_quantity=1,
        )


def test_ficha_com_insumo_repetido_e_recusada(loja):
    barra = _item(loja, "Barra de açaí 8%")

    with pytest.raises(ValueError, match="duas vezes"):
        ProductionRecipeCreate(
            name="Repetida",
            inputs=[ProductionLine(inventory_id=barra["id"], quantity=1),
                    ProductionLine(inventory_id=barra["id"], quantity=2)],
            output_inventory_id="acai", output_quantity=1,
        )


def test_ficha_que_aponta_para_insumo_inexistente_e_recusada(loja):
    """Quebraria so na hora da batida, com o freezer aberto."""

    with pytest.raises(ValueError, match="não existe no estoque"):
        loja.create_production_recipe(ProductionRecipeCreate(
            name="Fantasma", inputs=[ProductionLine(inventory_id="nao-existe", quantity=1)],
            output_inventory_id="acai", output_quantity=1,
        ))


def test_ficha_pode_ser_editada_e_removida(loja):
    barra = _item(loja, "Barra de açaí 8%")
    ficha = loja.create_production_recipe(ProductionRecipeCreate(
        name="Batida de 8%", inputs=[ProductionLine(inventory_id=barra["id"], quantity=8.4)],
        output_inventory_id="acai", output_quantity=11.0,
    ))

    editada = loja.update_production_recipe(ficha["id"], ProductionRecipeUpdate(output_quantity=12.0))
    assert editada["output_quantity"] == 12.0

    assert loja.delete_production_recipe(ficha["id"])["status"] == "excluida"
    assert loja.production_recipes() == []


# ----------------------------------------------------------------------
# A batida
# ----------------------------------------------------------------------


def test_a_barra_sai_e_a_base_entra(loja):
    barra_antes = _item(loja, "Barra de açaí 12%")["quantity"]
    base_antes = _item(loja, "Base de açaí")["quantity"]

    _bate(loja)

    assert _item(loja, "Barra de açaí 12%")["quantity"] == barra_antes - 8.4
    assert _item(loja, "Base de açaí")["quantity"] == base_antes + 10.0


def test_o_custo_do_quilo_de_base_vem_do_que_foi_usado(loja):
    """8,4 kg de barra a R$ 20,714 dão R$ 174 de lote; em 10 kg, R$ 17,40 o quilo."""

    resultado = _bate(loja)

    assert resultado["total_cost"] == pytest.approx(174.0, abs=0.01)
    assert resultado["output"]["unit_cost"] == pytest.approx(17.4, abs=0.01)


def test_barra_mais_cara_produz_base_mais_cara(loja):
    """E a diferenca entre 8% e 12% que faz a margem do copo ser real."""

    com_8 = _bate(loja, "Barra de açaí 8%")["output"]["unit_cost"]
    com_12 = _bate(loja, "Barra de açaí 12%")["output"]["unit_cost"]

    assert com_12 > com_8


def test_rendimento_pior_encarece_o_quilo(loja):
    """A perda de processo aparece no custo em vez de sumir."""

    bom = _bate(loja, entrada=8.4, saida=10.0)["output"]["unit_cost"]
    ruim = _bate(loja, entrada=8.4, saida=8.0)["output"]["unit_cost"]

    assert ruim > bom


def test_a_base_recebe_media_ponderada_e_nao_o_custo_do_lote(loja):
    base = _item(loja, "Base de açaí")
    saldo_antes, custo_antes = base["quantity"], base["unit_cost"]

    resultado = _bate(loja)

    esperado = (saldo_antes * custo_antes + 10.0 * resultado["output"]["unit_cost"]) / (saldo_antes + 10.0)
    assert _item(loja, "Base de açaí")["unit_cost"] == pytest.approx(esperado, abs=0.001)
    assert custo_antes != resultado["output"]["unit_cost"], "o teste só vale se os dois custos diferem"


def test_batida_sem_saldo_e_recusada_e_nao_baixa_nada(loja):
    barra_antes = _item(loja, "Barra de açaí 12%")["quantity"]
    base_antes = _item(loja, "Base de açaí")["quantity"]

    with pytest.raises(ValueError, match="Saldo insuficiente"):
        _bate(loja, entrada=999)

    assert _item(loja, "Barra de açaí 12%")["quantity"] == barra_antes
    assert _item(loja, "Base de açaí")["quantity"] == base_antes


def test_confirmacao_explicita_registra_com_saldo_negativo(loja):
    resultado = _bate(loja, entrada=999, allow_negative=True)

    assert _item(loja, "Barra de açaí 12%")["quantity"] < 0
    assert resultado["below_balance"] == ["Barra de açaí 12%"]
    assert resultado["inputs"][0]["below_balance"] is True


# ----------------------------------------------------------------------
# O que a fabricacao NAO pode virar
# ----------------------------------------------------------------------


def test_fabricacao_nao_conta_como_perda_nem_retirada(loja):
    """Contar aqui e de novo na venda cobraria o mesmo acai duas vezes."""

    _bate(loja)

    totais = loja.operating_result(30)["totals"]
    assert totais["inventory_losses"] == 0.0
    assert totais["inventory_withdrawals"] == 0

    dia = loja.sales_day()["totals"]
    assert dia["loss_cost"] == 0.0
    assert dia["withdrawal_cost"] == 0.0


def test_fabricacao_nao_conta_como_quebra_no_custo_do_produto(loja):
    """A barra que virou base nao se perdeu: ela mudou de item."""

    from app.services import CostingService

    _bate(loja)
    dossie = CostingService(loja.store).dossier("acai-500", days=30)

    assert dossie["cost"]["shrinkage"] == 0.0
    assert dossie["cost"]["with_shrinkage"] == dossie["cost"]["direct"]


def test_fabricacao_nao_entra_como_saida_sem_venda_na_previsao(loja):
    from app.services import ForecastService

    _bate(loja)
    saidas = ForecastService(loja.store)._saidas_sem_venda(30)

    assert saidas["withdrawal"] == 0.0
    assert saidas["loss"] == 0.0


def test_o_consumo_marca_a_fabricacao_com_tipo_proprio(loja):
    resultado = _bate(loja)

    historico = loja.consumption(7)["history"]
    linhas = [linha for linha in historico if linha.get("production_id") == resultado["id"]]

    assert len(linhas) == 2, "uma linha para a barra que saiu, outra para a base que entrou"
    assert {linha["kind"] for linha in linhas} == {"fabricacao"}
    # Corrigir uma ponta sozinha deixaria a outra órfã.
    assert all(linha["editable"] is False for linha in linhas)


def test_linha_de_fabricacao_nao_se_corrige_sozinha(loja):
    resultado = _bate(loja)
    movimento = resultado["inputs"][0]["movement_id"]

    with pytest.raises(ValueError, match="fabricação"):
        loja.update_movement(movimento, MovementUpdate(quantity_delta=-1))


# ----------------------------------------------------------------------
# Planejamento
# ----------------------------------------------------------------------


def test_o_consumo_de_barra_entra_na_serie_do_planejamento(loja):
    """A ficha tecnica consome base, nunca barra.

    Sem contar a fabricacao, o planejamento nunca veria a barra sair do freezer
    e mandaria repor so o que os copos consomem — que e base, nao barra.
    """

    barra = _item(loja, "Barra de açaí 12%")
    planning = PlanningService(loja.store)
    estoque = {item["id"]: item for item in loja.store.list("inventory")}

    antes, _ = planning._series(30, estoque)
    assert barra["id"] not in antes

    _bate(loja)
    depois, _ = planning._series(30, estoque)

    assert barra["id"] in depois
    assert sum(quantidade for _, quantidade in depois[barra["id"]]) == 8.4


def test_uma_batida_so_nao_vira_plano_de_compra(loja):
    """Coerente com o resto do modulo: amostra pequena rebaixa, nao inventa."""

    barra = _item(loja, "Barra de açaí 12%")
    _bate(loja)

    plano = PlanningService(loja.store).plan(days=30)

    assert not [row for row in plano["purchase"] if row["inventory_id"] == barra["id"]]


# ----------------------------------------------------------------------
# Desfazer
# ----------------------------------------------------------------------


def test_desfazer_devolve_as_duas_pontas(loja):
    barra_antes = _item(loja, "Barra de açaí 12%")["quantity"]
    base_antes = _item(loja, "Base de açaí")["quantity"]
    resultado = _bate(loja)

    desfeito = loja.undo_production(resultado["id"])

    assert desfeito["movements_reverted"] == 2
    assert _item(loja, "Barra de açaí 12%")["quantity"] == barra_antes
    assert _item(loja, "Base de açaí")["quantity"] == base_antes


def test_desfazer_restaura_o_custo_medio_da_base(loja):
    """Devolver so a quantidade deixaria o custo de uma batida que nao existe mais."""

    custo_antes = _item(loja, "Base de açaí")["unit_cost"]
    resultado = _bate(loja)
    assert _item(loja, "Base de açaí")["unit_cost"] != custo_antes

    loja.undo_production(resultado["id"])

    assert _item(loja, "Base de açaí")["unit_cost"] == custo_antes


def test_desfazer_duas_vezes_e_recusado(loja):
    resultado = _bate(loja)
    loja.undo_production(resultado["id"])

    with pytest.raises(ValueError, match="não encontrada"):
        loja.undo_production(resultado["id"])


# ----------------------------------------------------------------------
# O custo chega como o fornecedor cobra
# ----------------------------------------------------------------------


def test_compra_em_barra_vira_custo_por_quilo(loja):
    """O gestor sabe o preco da barra; a ficha tecnica precisa do quilo."""

    barra = _item(loja, "Barra de açaí 12%")

    # 10 barras de 2,8 kg a R$ 58 a barra = 28 kg a R$ 20,714 o quilo.
    assert barra["quantity"] == 28
    assert barra["unit_cost"] == pytest.approx(58 / 2.8, abs=0.001)
    assert barra["unit_cost"] * barra["pack_size"] == pytest.approx(58.0, abs=0.01)


def test_editar_o_insumo_nao_apaga_a_unidade_de_manuseio(loja):
    """Perder a conversao faria a barra voltar a ser lancada em quilo."""

    from app.schemas import InventoryItemUpdate

    barra = _item(loja, "Barra de açaí 12%")
    loja.update_inventory_item(barra["id"], InventoryItemUpdate(minimum_quantity=30))

    depois = _item(loja, "Barra de açaí 12%")
    assert depois["minimum_quantity"] == 30
    assert depois["pack_unit"] == "barra"
    assert depois["pack_size"] == 2.8


def test_a_unidade_de_manuseio_pode_ser_corrigida(loja):
    from app.schemas import InventoryItemUpdate

    barra = _item(loja, "Barra de açaí 12%")
    loja.update_inventory_item(barra["id"], InventoryItemUpdate(pack_unit="caixa", pack_size=10.0))

    depois = _item(loja, "Barra de açaí 12%")
    assert depois["pack_unit"] == "caixa"
    assert depois["pack_size"] == 10.0


# ----------------------------------------------------------------------
# Rendimento aprendido, sem balanca
# ----------------------------------------------------------------------


def _rendimento(erp, nome="Batida de 12%"):
    return next(linha for linha in erp.production_yield(90)["recipes"] if linha["name"] == nome)


def _ficha(erp, entrada=8.4, saida=10.0):
    barra = _item(erp, "Barra de açaí 12%")
    return erp.create_production_recipe(ProductionRecipeCreate(
        name="Batida de 12%", inputs=[ProductionLine(inventory_id=barra["id"], quantity=entrada)],
        output_inventory_id="acai", output_quantity=saida))


def _bate_com_ficha(erp, ficha, vezes=4, entrada=7.0, saida=8.0):
    """Batida ligada a ficha. Sem o vinculo ela vai para o grupo 'sem ficha'."""

    for _ in range(vezes):
        _bate(erp, entrada=entrada, saida=saida, recipe_id=ficha["id"])


def test_sem_batida_o_rendimento_vem_da_ficha_e_diz_isso(loja):
    """Ficha e ponto de partida, nao medida — e o modulo nao finge o contrario."""

    _ficha(loja)

    r = _rendimento(loja)
    assert r["suggested_ratio"] == pytest.approx(10.0 / 8.4, abs=0.001)
    assert r["source"] == "ficha"
    assert r["confidence"] == "baixa"
    assert "ponto de partida" in r["reading"]


def test_o_historico_das_batidas_substitui_a_ficha(loja):
    """O que a batida entrega vale mais que o que foi planejado."""

    _bate_com_ficha(loja, _ficha(loja, entrada=8.4, saida=10.0))

    r = _rendimento(loja)
    assert r["source"] == "histórico"
    assert r["observed"]["runs"] == 4
    assert r["suggested_ratio"] == pytest.approx(8.0 / 7.0, abs=0.001)
    assert r["declared"]["ratio"] != r["suggested_ratio"]


def test_uma_batida_so_nao_ganha_confianca(loja):
    _bate_com_ficha(loja, _ficha(loja), vezes=1)

    assert _rendimento(loja)["confidence"] == "baixa"


def test_a_contagem_fisica_corrige_o_rendimento(loja):
    """O jeito de medir sem balanca: contar a base e deixar a diferenca falar."""

    from app.schemas import InventoryCount

    _bate_com_ficha(loja, _ficha(loja))
    base = _item(loja, "Base de açaí")
    # A prateleira tem 3 kg a menos do que o sistema achava.
    loja.count_inventory("acai", InventoryCount(quantity=round(base["quantity"] - 3.0, 3)))

    r = _rendimento(loja)
    assert r["source"] == "contagem"
    assert r["confidence"] == "alta"
    assert r["corrected"]["count_difference"] == -3.0
    # 32 kg declarados menos 3 que faltaram, sobre 28 kg de barra.
    assert r["suggested_ratio"] == pytest.approx((32.0 - 3.0) / 28.0, abs=0.001)


def test_a_correcao_pela_contagem_e_apresentada_como_teto(loja):
    """Dentro da diferenca cabe retirada nao lancada e quebra, nao so rendimento."""

    from app.schemas import InventoryCount

    _bate_com_ficha(loja, _ficha(loja))
    base = _item(loja, "Base de açaí")
    loja.count_inventory("acai", InventoryCount(quantity=round(base["quantity"] - 3.0, 3)))

    leitura = _rendimento(loja)["reading"]
    assert "teto" in leitura
    assert "retirada não lançada" in leitura


def test_divergencia_grande_entre_ficha_e_operacao_vira_aviso(loja):
    _bate_com_ficha(loja, _ficha(loja, entrada=8.4, saida=12.0))

    bloqueios = loja.production_yield(90)["blockers"]
    assert any("rende" in b["title"] and "menos do que a ficha diz" in b["title"] for b in bloqueios)


def test_sem_contagem_o_modulo_avisa_que_ninguem_verificou(loja):
    _bate_com_ficha(loja, _ficha(loja))

    bloqueios = loja.production_yield(90)["blockers"]
    assert any("não foi confirmado por contagem" in b["title"] for b in bloqueios)
    assert any("sem precisar de balança" in b["description"] for b in bloqueios)


# ----------------------------------------------------------------------
# Nada foi pesado: o rendimento e uma faixa, nao um numero
# ----------------------------------------------------------------------


def _abastece_barra(erp, barras=40, nome="Barra de açaí 12%", custo=58.0):
    """Mais barra no freezer: a fixture compra 10, e os testes de faixa gastam mais."""

    item = _item(erp, nome)
    erp.register_inventory_purchase(item["id"], InventoryPurchase(
        purchase_quantity=barras, purchase_unit="barra",
        units_per_purchase=item["pack_size"], purchase_unit_cost=custo))


def _bate_variado(erp, ficha, saidas):
    _abastece_barra(erp)
    for saida in saidas:
        _bate(erp, entrada=7.0, saida=saida, recipe_id=ficha["id"])


def test_o_rendimento_e_devolvido_como_faixa(loja):
    """Um numero com quatro casas daria uma precisao que a operacao nao tem."""

    ficha = _ficha(loja)
    _bate_variado(loja, ficha, (7.5, 8.2, 7.8, 8.6, 8.0, 7.2))

    faixa = _rendimento(loja)["range"]
    assert faixa["low"] < faixa["likely"] < faixa["high"]
    assert faixa["observations"] == 6
    assert "6 lotes observados" in faixa["basis"]
    assert "assumed" not in faixa


def test_a_faixa_encolhe_conforme_os_lotes_se_acumulam(loja):
    """A largura e a informacao honesta: ela diminui, mas nunca some."""

    ficha = _ficha(loja)
    sem_lote = _rendimento(loja)["range"]["width_percent"]

    _bate_variado(loja, ficha, (7.5, 8.2, 7.8, 8.6, 8.0, 7.2))
    com_lote = _rendimento(loja)["range"]["width_percent"]

    assert com_lote < sem_lote
    assert com_lote > 0, "a faixa nunca vira um ponto"


def test_sem_lote_a_margem_e_declarada_como_suposta(loja):
    _ficha(loja)

    faixa = _rendimento(loja)["range"]
    assert faixa["assumed"] is True
    assert "margem de ±20% assumida" in faixa["basis"]
    assert "a margem é suposta" in _rendimento(loja)["reading"]


def test_lotes_todos_iguais_nao_viram_precisao(loja):
    """Confirmar a sugestao da tela nao e medir: o sistema aprenderia o proprio palpite."""

    ficha = _ficha(loja)
    _bate_variado(loja, ficha, (8.0, 8.0, 8.0, 8.0))

    r = _rendimento(loja)
    assert r["observed"]["identical"] is True
    # Quatro lotes iguais sao uma observacao repetida, nao quatro observacoes.
    assert r["confidence"] == "baixa"
    assert r["range"].get("assumed") is True
    assert "não é precisão" in r["reading"]


def test_lotes_iguais_viram_aviso_explicito(loja):
    ficha = _ficha(loja)
    _bate_variado(loja, ficha, (8.0, 8.0, 8.0, 8.0))

    bloqueios = loja.production_yield(90)["blockers"]
    aviso = next((b for b in bloqueios if "mesmo rendimento" in b["title"]), None)

    assert aviso is not None
    assert "aprendendo com o" in aviso["description"]
    assert "cubas ou potes" in aviso["description"]


def test_lotes_variados_nao_disparam_o_aviso(loja):
    ficha = _ficha(loja)
    _bate_variado(loja, ficha, (7.5, 8.2, 7.8, 8.6))

    bloqueios = loja.production_yield(90)["blockers"]
    assert not any("mesmo rendimento" in b["title"] for b in bloqueios)


def test_a_faixa_nunca_extrapola_o_que_os_lotes_mostraram(loja):
    """Se nenhum lote rendeu acima de X, a faixa nao inventa um X maior."""

    ficha = _ficha(loja)
    saidas = (7.5, 8.2, 7.8, 8.6, 8.0, 7.2)
    _bate_variado(loja, ficha, saidas)

    r = _rendimento(loja)
    assert r["range"]["low"] >= r["observed"]["min"]
    assert r["range"]["high"] <= r["observed"]["max"]


# ----------------------------------------------------------------------
# Custo real: os dois unicos numeros que a loja conta
# ----------------------------------------------------------------------


def _loja_com_movimento(erp, dias=28, copos_500=3, copos_300=2, entrada=8.4, saida=10.0):
    """Barra saindo do freezer e copo saindo pelo caixa, que e tudo o que se conta."""

    from datetime import datetime, timedelta

    from app.schemas import ChannelCreate, OrderCreate, OrderLine
    from app.services.erp import FUSO_LOJA

    try:
        erp.create_channel(ChannelCreate(name="Balcao", fee_percent=0.0))
    except ValueError:
        pass
    _abastece_barra(erp, barras=60)
    ficha = _ficha(erp, entrada=entrada, saida=saida)
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(dias, 0, -1):
        quando = datetime.combine(hoje - timedelta(days=atras), datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        if atras % 4 == 0:
            _bate(erp, entrada=entrada, saida=saida, recipe_id=ficha["id"])
        erp.create_order(OrderCreate(
            items=[OrderLine(product_id="acai-500", quantity=copos_500),
                   OrderLine(product_id="acai-300", quantity=copos_300)],
            channel="Balcao", occurred_at=quando))
    return ficha


def test_o_custo_real_sai_de_barra_consumida_e_copo_vendido(loja):
    """Sem balanca e sem rendimento: os dois lados sao contados."""

    _loja_com_movimento(loja)

    e = loja.production_economics(30)
    assert e["available"] is True
    assert e["bars"][0]["packs"] > 0, "a barra aparece contada em barras, como a equipe conta"
    assert e["cups"] > 0
    assert e["cost_per_cup_real"] > 0
    # O custo por copo e o custo da barra dividido pelos copos, e mais nada.
    assert e["cost_per_cup_real"] == pytest.approx(e["bars_cost"] / e["cups"], abs=0.001)


def test_o_rendimento_implicito_nao_precisa_de_ninguem_pesando(loja):
    """Ele cai fora da conta: e a base que a venda consumiu sobre a barra que saiu."""

    _loja_com_movimento(loja)

    e = loja.production_economics(30)
    barra = sum(linha["quantity"] for linha in e["bars"])
    assert e["implied_yield"] == pytest.approx(e["base_theoretical"] / barra, abs=0.001)


def test_a_diferenca_para_o_cadastro_e_quantificada_por_produto(loja):
    _loja_com_movimento(loja)

    e = loja.production_economics(30)
    assert e["products"], "cada produto que usa a base entra na conta"
    for linha in e["products"]:
        assert linha["acai_cost_real"] > 0
        assert linha["difference_total"] == pytest.approx(
            (linha["acai_cost_real"] - linha["acai_cost_registered"]) * linha["units"], abs=0.01)
    assert e["difference_total"] == pytest.approx(
        sum(linha["difference_total"] for linha in e["products"]), abs=0.01)


def test_a_premissa_do_estoque_estavel_e_declarada(loja):
    """Sem contagem no periodo, a conta assume que o freezer comecou e terminou igual."""

    _loja_com_movimento(loja)

    bloqueios = loja.production_economics(30)["blockers"]
    aviso = next((b for b in bloqueios if "começou e terminou parecido" in b["title"]), None)
    assert aviso is not None
    assert "transforma a premissa em medida" in aviso["description"]


def test_a_contagem_no_periodo_tira_o_aviso_da_premissa(loja):
    from app.schemas import InventoryCount

    _loja_com_movimento(loja)
    base = _item(loja, "Base de açaí")
    loja.count_inventory("acai", InventoryCount(quantity=base["quantity"]))

    bloqueios = loja.production_economics(30)["blockers"]
    assert not any("começou e terminou parecido" in b["title"] for b in bloqueios)


def test_sem_batida_ou_sem_venda_a_conta_nao_e_inventada(loja):
    e = loja.production_economics(30)

    assert e["available"] is False
    assert "Faltam os dois números" in e["reason"]
    assert e["products"] == []


def test_contagem_que_bate_exatamente_tambem_conta_como_evidencia(loja):
    """Ela nao gera movimento, e e a melhor prova que existe: confirma o saldo."""

    from app.schemas import InventoryCount

    _loja_com_movimento(loja)
    base = _item(loja, "Base de açaí")
    loja.count_inventory("acai", InventoryCount(quantity=base["quantity"]))

    # Nenhum movimento de contagem foi criado, porque a diferença foi zero.
    assert not [m for m in loja.store.list("inventory_movements") if m.get("kind") == "contagem"]
    # Ainda assim a loja contou, e o carimbo no item registra isso.
    assert _item(loja, "Base de açaí")["counted_at"]
