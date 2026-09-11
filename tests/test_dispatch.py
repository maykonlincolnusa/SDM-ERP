"""Check-in, check-out e a entrega registrada na rua.

O sistema sabia quanto a entrega custava e não sabia de onde ela vinha. Não
havia endereço em lugar nenhum — nem no pedido, nem no relatório da Anota Aí,
nem no que a integração do iFood aproveitava. "Onde mais pedem" era pergunta
sem dado por trás, e "demorou" era impressão contra impressão.

Os testes aqui cobrem as três coisas que fazem esse registro valer:

- ele custa dois toques, porque nasce do pedido que já está no ERP;
- ele não deixa o mesmo fato ser digitado duas vezes: o acerto do turno passa a
  ser calculado do que foi registrado na rua, na mesma coleção onde o
  pagamento já morava;
- ele não mente quando a amostra é pequena — a leitura de território marca o
  que é fraco em vez de esconder.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.repositories.store import DemoStore
from app.schemas import (
    CourierCheckIn,
    CourierCheckOut,
    CourierCreate,
    CourierUpdate,
    DeliveryRunCreate,
    DeliveryRunFinish,
    DeliveryRunItem,
    DeliveryRunUpdate,
    DeliveryZoneCreate,
    DeliveryZoneUpdate,
    OrderCreate,
    OrderLine,
)
from app.services import DeliveryService, DispatchService, ERPService
from app.services.dispatch import FUSO_LOJA, RUNS, SHIFTS, ZONES


@pytest.fixture
def store(tmp_path) -> DemoStore:
    loja = DemoStore(tmp_path / "store.json")
    # Os pedidos de demonstração são de balcão e de dias passados; a fila é do
    # dia de hoje. Limpar deixa cada teste montar exatamente o seu cenário.
    for pedido in loja.list("orders"):
        loja.delete("orders", pedido["id"])
    return loja


@pytest.fixture
def dispatch(store) -> DispatchService:
    return DispatchService(store)


@pytest.fixture
def courier(store) -> dict:
    return DeliveryService(store).create_courier(
        CourierCreate(name="João", vehicle="Moto", fee_per_delivery=4.0, email="joao@loja.com")
    )


@pytest.fixture
def centro(dispatch) -> dict:
    return dispatch.create_zone(DeliveryZoneCreate(name="Centro", default_fee=6.0, target_minutes=25))


def _pedido(store, produto="acai-500", quantidade=1, frete=7.0, cliente="Ana") -> dict:
    return ERPService(store).create_order(OrderCreate(
        items=[OrderLine(product_id=produto, quantity=quantidade)],
        is_delivery=True, delivery_fee=frete, customer_name=cliente, channel="Delivery",
    ))


def _turno(dispatch, courier) -> dict:
    return dispatch.check_in(courier, CourierCheckIn())


# ----------------------------------------------------------------------
# Zonas
# ----------------------------------------------------------------------


def test_zona_repetida_e_recusada(dispatch, centro):
    """"Centro" e "centro" são a mesma rua e matariam a agregação por zona."""

    with pytest.raises(ValueError, match="Já existe uma zona"):
        dispatch.create_zone(DeliveryZoneCreate(name="  centro  "))


def test_zona_com_entrega_e_desativada_e_nao_excluida(dispatch, store, courier, centro):
    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))

    resultado = dispatch.delete_zone(centro["id"])

    assert resultado["status"] == "inativada"
    assert store.get("delivery_zones", centro["id"])["active"] is False


def test_zona_sem_entrega_pode_ser_excluida(dispatch, store, centro):
    assert dispatch.delete_zone(centro["id"])["status"] == "excluido"
    assert store.get("delivery_zones", centro["id"]) is None


def test_renomear_zona_para_um_nome_existente_e_recusado(dispatch, centro):
    outra = dispatch.create_zone(DeliveryZoneCreate(name="Vila Nova"))

    with pytest.raises(ValueError, match="Já existe uma zona"):
        dispatch.update_zone(outra["id"], DeliveryZoneUpdate(name="Centro"))


# ----------------------------------------------------------------------
# Turno
# ----------------------------------------------------------------------


def test_check_in_abre_o_turno_e_o_segundo_toque_e_recusado(dispatch, courier):
    turno = _turno(dispatch, courier)

    assert turno["open"] is True
    # Dois turnos abertos partiriam o acerto do dia em dois lançamentos.
    with pytest.raises(ValueError, match="já fez check-in"):
        dispatch.check_in(courier, CourierCheckIn())


def test_entregador_inativo_nao_abre_turno(dispatch, store, courier):
    courier["active"] = False
    store.put("couriers", courier["id"], courier)

    with pytest.raises(ValueError, match="inativo"):
        dispatch.check_in(courier, CourierCheckIn())


def test_turno_esquecido_aberto_de_ontem_nao_aparece_como_gente_na_rua(dispatch, store, courier):
    """O entregador fecha o aplicativo e vai embora; o painel não pode mentir."""

    turno = _turno(dispatch, courier)
    turno["checkin_at"] = (datetime.fromisoformat(turno["checkin_at"]) - timedelta(hours=20)).isoformat()
    store.put(SHIFTS, turno["id"], {k: v for k, v in turno.items() if k in
                                    ("id", "courier_id", "date", "checkin_at", "checkout_at", "note", "created_at")})

    assert dispatch.open_shift(courier["id"]) is None
    assert store.get(SHIFTS, turno["id"])["auto_closed"]


def test_turno_esquecido_aberto_ainda_paga_o_que_foi_entregue(dispatch, store, courier, centro):
    """Esquecer de tocar no botão não pode custar o dia de trabalho.

    O fechamento automático existe para o painel não mentir. Parar aí criaria
    um problema pior: as entregas ficam registradas e o acerto nunca nasce.
    """

    turno = _turno(dispatch, courier)
    for _ in range(2):
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())
    # E uma que ficou em rota: ninguém confirmou que chegou.
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
    ))

    bruto = store.get(SHIFTS, turno["id"])
    bruto["checkin_at"] = (datetime.fromisoformat(bruto["checkin_at"]) - timedelta(hours=20)).isoformat()
    store.put(SHIFTS, turno["id"], bruto)
    assert dispatch.open_shift(courier["id"]) is None

    fechado = store.get(SHIFTS, turno["id"])
    assert fechado["deliveries"] == 2
    assert fechado["freight_total"] == 14.0
    # A que ficou em rota fica visível para o balcão encerrar ou excluir.
    assert fechado["left_on_route"] == 1
    lancamento = store.list("delivery_entries")
    assert len(lancamento) == 1
    assert lancamento[0]["deliveries"] == 2
    assert DeliveryService(store).performance(30)["totals"]["payout"] == 8.0


def test_turno_esquecido_sem_nenhuma_entrega_nao_gera_acerto(dispatch, store, courier):
    """Lançamento de zero entregas seria linha de ruído no acerto do mês."""

    turno = _turno(dispatch, courier)
    bruto = store.get(SHIFTS, turno["id"])
    bruto["checkin_at"] = (datetime.fromisoformat(bruto["checkin_at"]) - timedelta(hours=20)).isoformat()
    store.put(SHIFTS, turno["id"], bruto)

    dispatch.open_shift(courier["id"])

    assert store.list("delivery_entries") == []


def test_entrega_exige_turno_aberto(dispatch, courier, centro):
    with pytest.raises(ValueError, match="check-in"):
        dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
        ))


# ----------------------------------------------------------------------
# A fila: o que faz o registro custar dois toques
# ----------------------------------------------------------------------


def test_a_fila_traz_produto_quantidade_e_frete_do_pedido(dispatch, store, courier, centro):
    pedido = _pedido(store, produto="acai-700", quantidade=2, frete=9.0)
    _turno(dispatch, courier)

    fila = dispatch.queue(courier["id"])["orders"]

    assert len(fila) == 1
    linha = fila[0]
    assert linha["order_id"] == pedido["id"]
    assert linha["freight"] == 9.0
    assert linha["units"] == 2
    assert linha["items"][0]["name"] == "Açaí 700 ml"


def test_a_fila_sugere_a_zona_que_o_cliente_usou_da_ultima_vez(dispatch, store, courier, centro):
    """Digitar a zona é o único dado novo da entrega; adivinhá-la é o que sobra para cortar."""

    _turno(dispatch, courier)
    primeiro = _pedido(store, cliente="Ana")
    dispatch.register_run(courier, DeliveryRunCreate(zone_id=centro["id"], order_id=primeiro["id"]))

    segundo = _pedido(store, cliente="ANA")
    sugestao = next(linha for linha in dispatch.queue(courier["id"])["orders"] if linha["order_id"] == segundo["id"])

    assert sugestao["suggested_zone_id"] == centro["id"]


def test_pedido_de_balcao_nao_entra_na_fila_de_entrega(dispatch, store, courier, centro):
    ERPService(store).create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))
    _turno(dispatch, courier)

    assert dispatch.queue(courier["id"])["orders"] == []


# ----------------------------------------------------------------------
# A entrega
# ----------------------------------------------------------------------


def test_entrega_do_pedido_herda_itens_e_frete_e_da_dono_ao_pedido(dispatch, store, courier, centro):
    pedido = _pedido(store, produto="acai-500", quantidade=3, frete=8.5)
    _turno(dispatch, courier)

    run = dispatch.register_run(courier, DeliveryRunCreate(zone_id=centro["id"], order_id=pedido["id"]))

    assert run["units"] == 3
    assert run["freight"] == 8.5
    assert run["order_total"] == pedido["total"]
    # Sem dono no pedido, a conferência do acerto continuaria acusando furo.
    atualizado = store.get("orders", pedido["id"])
    assert atualizado["courier_id"] == courier["id"]
    assert atualizado["zone_name"] == "Centro"


def test_entrega_sem_pedido_usa_o_frete_da_zona(dispatch, courier, centro):
    _turno(dispatch, courier)

    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-300", quantity=2)]
    ))

    assert run["freight"] == 6.0
    assert run["items"][0]["name"] == "Açaí 300 ml"


def test_o_mesmo_pedido_nao_pode_ser_pego_duas_vezes(dispatch, store, courier, centro):
    """Dois entregadores tocando na mesma linha da fila inflaria o acerto dos dois."""

    pedido = _pedido(store)
    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(zone_id=centro["id"], order_id=pedido["id"]))

    with pytest.raises(ValueError, match="já foi pego"):
        dispatch.register_run(courier, DeliveryRunCreate(zone_id=centro["id"], order_id=pedido["id"]))


def test_entrega_sem_pedido_e_sem_itens_e_recusada():
    with pytest.raises(ValueError, match="pedido da fila ou informe os produtos"):
        DeliveryRunCreate(zone_id="zona-centro")


def test_chegada_mede_o_tempo_pelo_relogio(dispatch, store, courier, centro):
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    bruto = store.get(RUNS, run["id"])
    bruto["departed_at"] = (datetime.fromisoformat(bruto["departed_at"]) - timedelta(minutes=18)).isoformat()
    store.put(RUNS, run["id"], bruto)

    chegou = dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    assert chegou["minutes"] == 18
    assert chegou["open"] is False


def test_entrega_de_outro_entregador_nao_pode_ser_encerrada(dispatch, store, courier, centro):
    outro = DeliveryService(store).create_courier(CourierCreate(name="Maria"))
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))

    with pytest.raises(ValueError, match="de outro entregador"):
        dispatch.finish_run(outro, run["id"], DeliveryRunFinish())


def test_a_loja_corrige_a_zona_trocada_na_pressa(dispatch, courier, centro):
    outra = dispatch.create_zone(DeliveryZoneCreate(name="Vila Nova"))
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))

    corrigida = dispatch.update_run(run["id"], DeliveryRunUpdate(zone_id=outra["id"]))

    assert corrigida["zone_name"] == "Vila Nova"


# ----------------------------------------------------------------------
# Check-out: o acerto deixa de ser digitado
# ----------------------------------------------------------------------


def test_check_out_recusa_turno_com_entrega_em_rota(dispatch, courier, centro):
    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))

    with pytest.raises(ValueError, match="em rota"):
        dispatch.check_out(courier, CourierCheckOut())


def test_check_out_monta_o_acerto_do_turno_sem_ninguem_digitar(dispatch, store, courier, centro):
    """A segunda digitação do mesmo fato é sempre a que diverge."""

    _turno(dispatch, courier)
    for _ in range(3):
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    fechado = dispatch.check_out(courier, CourierCheckOut())

    assert fechado["deliveries"] == 3
    assert fechado["freight_total"] == 21.0
    # O lançamento nasce na coleção onde o pagamento já morava, e a conta do
    # entregador — 4,00 por entrega — sai dela sem nenhuma mudança.
    assert fechado["entry"]["payout"] == 12.0
    lancado = store.list("delivery_entries")
    assert len(lancado) == 1
    assert lancado[0]["source"] == "check-out"
    assert lancado[0]["shift_id"] == fechado["id"]


def test_entrega_que_voltou_nao_conta_no_acerto_nem_na_media(dispatch, courier, centro):
    _turno(dispatch, courier)
    boa = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
    ))
    dispatch.finish_run(courier, boa["id"], DeliveryRunFinish())
    voltou = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
    ))
    dispatch.finish_run(courier, voltou["id"], DeliveryRunFinish(failed=True, note="Cliente não atendeu"))

    fechado = dispatch.check_out(courier, CourierCheckOut())

    assert fechado["deliveries"] == 1
    assert fechado["freight_total"] == 7.0
    assert fechado["failed"] == 1


def test_o_acerto_calculado_aparece_no_custo_da_entrega_que_ja_existia(dispatch, store, courier, centro):
    """A máquina de pagamento continua inteira: o check-out só a alimenta."""

    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
    ))
    dispatch.finish_run(courier, run["id"], DeliveryRunFinish())
    dispatch.check_out(courier, CourierCheckOut())

    desempenho = DeliveryService(store).performance(30)

    assert desempenho["totals"]["deliveries"] == 1
    assert desempenho["totals"]["freight"] == 7.0
    assert desempenho["totals"]["payout"] == 4.0


# ----------------------------------------------------------------------
# O painel
# ----------------------------------------------------------------------


def test_o_painel_mostra_quem_esta_na_rua_agora(dispatch, courier, centro):
    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))

    agora = dispatch.board(30)["now"]

    assert agora["checked_in"] == 1
    assert agora["on_route"] == 1
    assert agora["on_shift"][0]["on_route"][0]["zone_name"] == "Centro"


def test_o_painel_aponta_a_entrega_que_passou_do_tempo_combinado(dispatch, store, courier, centro):
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    bruto = store.get(RUNS, run["id"])
    bruto["departed_at"] = (datetime.fromisoformat(bruto["departed_at"]) - timedelta(minutes=40)).isoformat()
    store.put(RUNS, run["id"], bruto)

    painel = dispatch.board(30)

    assert painel["now"]["on_shift"][0]["on_route"][0]["late"] is True
    assert any("passaram do tempo" in leitura["title"] for leitura in painel["readings"])


def test_zona_sem_tempo_combinado_nunca_atrasa(dispatch, store, courier):
    """Limite inventado apontaria problema onde a loja não definiu expectativa."""

    livre = dispatch.create_zone(DeliveryZoneCreate(name="Interior"))
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=livre["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    bruto = store.get(RUNS, run["id"])
    bruto["departed_at"] = (datetime.fromisoformat(bruto["departed_at"]) - timedelta(hours=3)).isoformat()
    store.put(RUNS, run["id"], bruto)

    assert dispatch.board(30)["now"]["on_shift"][0]["on_route"][0]["late"] is False


def test_o_painel_separa_zona_de_entregador(dispatch, courier, centro):
    outra = dispatch.create_zone(DeliveryZoneCreate(name="Vila Nova"))
    _turno(dispatch, courier)
    for zona in (centro, centro, outra):
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=zona["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=2)], freight=5.0
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    painel = dispatch.board(30)

    assert [linha["name"] for linha in painel["zones"]] == ["Centro", "Vila Nova"]
    assert painel["zones"][0]["deliveries"] == 2
    assert painel["zones"][0]["share"] == pytest.approx(66.7)
    assert painel["couriers"][0]["deliveries"] == 3
    assert painel["couriers"][0]["units"] == 6
    assert painel["totals"]["freight"] == 15.0


# ----------------------------------------------------------------------
# O que a rua ensina
# ----------------------------------------------------------------------


def test_territorio_sem_entrega_diz_o_que_falta_em_vez_de_mostrar_zero(dispatch):
    territorio = dispatch.territory(90)

    assert territorio["status"] == "sem-dados"
    assert territorio["zones"] == []
    assert "check-out" in territorio["note"]


def test_territorio_marca_amostra_fraca_em_vez_de_esconder(dispatch, courier, centro):
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    territorio = dispatch.territory(90)

    assert territorio["status"] == "amostra-fraca"
    assert territorio["zones"][0]["thin"] is True
    # Zona nova não pode desaparecer da tela por ser nova.
    assert territorio["zones"][0]["deliveries"] == 1


def test_territorio_aponta_o_produto_que_uma_zona_pede_acima_da_media(dispatch, courier, centro):
    """O cardápio único esconde o produto que é campeão de um bairro só."""

    outra = dispatch.create_zone(DeliveryZoneCreate(name="Vila Nova"))
    _turno(dispatch, courier)
    # O Centro só pede 700 ml; a Vila Nova só pede 300 ml.
    for zona, produto in [(centro, "acai-700")] * 10 + [(outra, "acai-300")] * 10:
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=zona["id"], items=[DeliveryRunItem(product_id=produto, quantity=1)]
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    territorio = dispatch.territory(90)
    cardapio = {linha["zone_name"]: linha for linha in territorio["menu"]}

    assert territorio["status"] == "medido"
    assert cardapio["Centro"]["product_name"] == "Açaí 700 ml"
    # Metade das unidades da loja e 100% das do Centro: o dobro da média.
    assert cardapio["Centro"]["lift"] == pytest.approx(2.0)
    assert cardapio["Vila Nova"]["product_name"] == "Açaí 300 ml"


def test_a_previsao_por_zona_abre_a_conta_em_vez_de_entregar_um_numero(dispatch, courier, centro):
    _turno(dispatch, courier)
    for _ in range(6):
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    zona = dispatch.territory(90, horizon=7)["zones"][0]

    # Os três números que produzem a previsão ficam na tela para o gestor
    # discordar de um deles: média diária, peso do dia e tendência.
    assert zona["daily_average"] == 6.0
    assert zona["trend"] == 1.0
    assert len(zona["forecast"]) == 7
    assert zona["best_weekday"]["deliveries"] == 6
    assert zona["top_products"][0]["units"] == 6


def test_a_media_por_zona_ignora_o_dia_em_que_a_loja_nao_abriu(dispatch, store, courier, centro):
    """Dividir pelo calendário transformaria porta fechada em zona fraca."""

    _turno(dispatch, courier)
    for _ in range(4):
        run = dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
        ))
        dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    zona = dispatch.territory(90)["zones"][0]

    # Quatro entregas num único dia com movimento: a média do dia aberto é 4,
    # e não 4/90.
    assert zona["active_days"] == 1
    assert zona["daily_average"] == 4.0


# ----------------------------------------------------------------------
# A venda que veio da rua
#
# O registro de entrega existia e parava na logistica: a encomenda combinada
# por telefone rodava a rua, contava no acerto do entregador e o faturamento
# dela simplesmente nao existia no sistema. Estes testes cobrem o caminho que
# fecha esse buraco sem deixar quem recebe o dinheiro lancar a propria receita
# sem revisao.
# ----------------------------------------------------------------------


def test_entrega_sem_pedido_abre_uma_venda_a_conferir(dispatch, store, courier, centro):
    _turno(dispatch, courier)

    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"],
        items=[DeliveryRunItem(product_id="acai-500", quantity=2)],
        customer_name="Ana", declared_total=50.0, payment_method="Pix",
    ))

    pendentes = dispatch.pending_orders(30)
    assert len(pendentes) == 1
    venda = pendentes[0]
    assert venda["status"] == "pendente"
    assert venda["run_id"] == run["id"]
    assert venda["declared_total"] == 50.0
    # O cardápio calcula 2 x 22,00 mais 6,00 de frete da zona; a rua recebeu
    # 50,00. Os dois ficam guardados, porque a divergência é o que a loja
    # precisa ver na hora de conferir.
    assert venda["suggested_total"] == 50.0


def test_a_venda_a_conferir_nao_e_faturamento_ate_alguem_confirmar(dispatch, store, courier, centro):
    """`is_active_order` é lista negra: um pedido pendente dentro de `orders`
    viraria receita em três lugares no mesmo instante em que fosse gravado."""

    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=2)], declared_total=50.0,
    ))

    # Nenhum pedido existe, entao nenhum dos tres filtros independentes de
    # status pode contar essa venda como faturamento.
    assert store.list("orders") == []
    assert ERPService(store).dashboard(30)["metrics"]["revenue"] == 0


def test_confirmar_promove_a_venda_e_baixa_a_ficha_tecnica(dispatch, store, courier, centro):
    from app.schemas import PendingOrderConfirm

    antes = next(item for item in ERPService(store).inventory() if item["id"] == "acai")["quantity"]
    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=2)],
        customer_name="Ana", declared_total=50.0, payment_method="Pix", freight=7.0,
    ))
    pendente = dispatch.pending_orders(30)[0]

    confirmada = dispatch.confirm_pending(pendente["id"], PendingOrderConfirm())

    assert confirmada["status"] == "confirmada"
    pedido = confirmada["order"]
    assert pedido["is_delivery"] is True
    assert pedido["courier_id"] == courier["id"]
    assert pedido["delivery_fee"] == 7.0
    # O dinheiro manda sobre o preco de tabela, como na integracao do iFood.
    # 2 x 22,00 de produto mais 7,00 de frete dariam 51,00; o cliente pagou 50,00.
    assert pedido["total"] == 50.0
    assert pedido["catalog_total"] == 51.0
    assert pedido["revenue_conflict"] is True
    # A ficha tecnica baixou de verdade: e este o pipeline inteiro.
    depois = next(item for item in ERPService(store).inventory() if item["id"] == "acai")["quantity"]
    assert depois < antes


def test_confirmar_liga_a_venda_a_entrega_e_ao_territorio(dispatch, store, courier, centro):
    from app.schemas import PendingOrderConfirm

    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], declared_total=29.0,
    ))
    dispatch.confirm_pending(dispatch.pending_orders(30)[0]["id"], PendingOrderConfirm())

    atualizada = store.get(RUNS, run["id"])
    assert atualizada["order_id"]
    # Sem o valor na entrega, a venda ficaria fora do faturamento por zona.
    assert atualizada["order_total"] == 29.0
    assert dispatch.board(30)["zones"][0]["revenue"] == 29.0


def test_confirmar_duas_vezes_e_recusado(dispatch, courier, centro):
    from app.schemas import PendingOrderConfirm

    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], declared_total=25.0,
    ))
    pendente = dispatch.pending_orders(30)[0]
    dispatch.confirm_pending(pendente["id"], PendingOrderConfirm())

    with pytest.raises(ValueError, match="já foi confirmada"):
        dispatch.confirm_pending(pendente["id"], PendingOrderConfirm())


def test_recusar_exige_motivo_e_nao_apaga_a_entrega(dispatch, store, courier, centro):
    """Entrega recusada rodou a rua igual, e conta no acerto do entregador."""

    from app.schemas import PendingOrderReject

    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], declared_total=25.0,
    ))
    pendente = dispatch.pending_orders(30)[0]

    recusada = dispatch.reject_pending(pendente["id"], PendingOrderReject(reason="Já estava lançada no balcão"))

    assert recusada["status"] == "recusada"
    assert recusada["reason"] == "Já estava lançada no balcão"
    assert store.get(RUNS, run["id"]) is not None
    assert store.list("orders") == []
    assert dispatch.pending_orders(30) == []


def test_a_venda_da_rua_entra_num_canal_proprio(dispatch, store, courier, centro):
    """Misturá-la com o balcão apagaria a pergunta que essa origem responde."""

    from app.schemas import PendingOrderConfirm

    _turno(dispatch, courier)
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], declared_total=25.0,
    ))
    confirmada = dispatch.confirm_pending(dispatch.pending_orders(30)[0]["id"], PendingOrderConfirm())

    assert confirmada["order"]["channel"] == "Entrega direta"


# ----------------------------------------------------------------------
# Data: a entrega de outro dia
# ----------------------------------------------------------------------


def test_entrega_de_ontem_pode_ser_lancada_sem_turno_aberto(dispatch, store, courier, centro):
    """Obrigar a reabrir turno encerrado faria o esquecimento ser mais barato."""

    ontem = datetime.now(FUSO_LOJA) - timedelta(days=1)

    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
        occurred_at=ontem, declared_total=25.0,
    ))

    assert run["date"] == ontem.date().isoformat()
    assert run["backdated"] is True
    # Ela ja chegou: ninguem vai tocar em "Cheguei" amanha.
    assert run["finished_at"]
    # E a duracao e desconhecida, nao calculada — a diferenca entre a saida de
    # ontem e o relogio de agora seriam horas, e isso entraria na mediana.
    assert run["minutes"] is None


def test_a_entrega_atrasada_nao_polui_a_mediana_de_tempo(dispatch, courier, centro):
    _turno(dispatch, courier)
    hoje = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    dispatch.finish_run(courier, hoje["id"], DeliveryRunFinish())
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
        occurred_at=datetime.now(FUSO_LOJA) - timedelta(days=2),
    ))

    zona = dispatch.board(30)["zones"][0]

    assert zona["deliveries"] == 2
    # A mediana sai só da entrega cujo tempo foi medido de verdade.
    assert zona["minutes_median"] == 0


def test_entrega_no_futuro_e_recusada(dispatch, courier, centro):
    with pytest.raises(ValueError, match="no futuro"):
        dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
            occurred_at=datetime.now(FUSO_LOJA) + timedelta(hours=2),
        ))


def test_entrega_alem_da_janela_de_retroagir_e_recusada(dispatch, courier, centro):
    """Mês já fechado e pago não pode receber entrega nova pelo celular."""

    with pytest.raises(ValueError, match="últimos 7 dias"):
        dispatch.register_run(courier, DeliveryRunCreate(
            zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
            occurred_at=datetime.now(FUSO_LOJA) - timedelta(days=9),
        ))


def test_entrega_atrasada_refaz_o_acerto_do_turno_daquele_dia(dispatch, store, courier, centro):
    """Sem isso o entregador só descobriria a falta no fim do mês."""

    ontem = datetime.now(FUSO_LOJA) - timedelta(days=1)
    # Um turno de ontem, fechado e acertado com uma entrega só.
    turno = dispatch.check_in(courier, CourierCheckIn())
    bruto = store.get(SHIFTS, turno["id"])
    bruto.update({"date": ontem.date().isoformat(), "checkin_at": ontem.isoformat()})
    store.put(SHIFTS, turno["id"], bruto)
    primeira = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
        occurred_at=ontem, freight=7.0,
    ))
    assert store.get(RUNS, primeira["id"])["shift_id"] == turno["id"]
    dispatch._resettlement(store.get(SHIFTS, turno["id"]), courier)
    assert DeliveryService(store).performance(30)["totals"]["payout"] == 4.0

    # A segunda entrega chega depois, e o acerto se corrige sozinho.
    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
        occurred_at=ontem, freight=7.0,
    ))

    desempenho = DeliveryService(store).performance(30)
    assert desempenho["totals"]["deliveries"] == 2
    assert desempenho["totals"]["freight"] == 14.0
    assert desempenho["totals"]["payout"] == 8.0
    # Um lançamento só, atualizado — não dois.
    assert len(store.list("delivery_entries")) == 1


# ----------------------------------------------------------------------
# Historico: turno com hora, e o que ficou fora dele
# ----------------------------------------------------------------------


def test_o_entregador_ve_o_proprio_turno_com_hora_de_entrada_e_saida(dispatch, courier, centro):
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)], freight=7.0
    ))
    dispatch.finish_run(courier, run["id"], DeliveryRunFinish())
    dispatch.check_out(courier, CourierCheckOut())

    historico = dispatch.field_history(courier, 30)

    assert len(historico["shifts"]) == 1
    turno = historico["shifts"][0]
    assert turno["checkin_at"] and turno["checkout_at"]
    assert turno["open"] is False
    assert turno["deliveries"] == 1
    assert turno["freight"] == 7.0
    assert len(turno["runs"]) == 1
    assert historico["totals"]["deliveries"] == 1


def test_o_historico_mostra_a_entrega_lancada_para_um_dia_sem_turno(dispatch, courier, centro):
    """Escondida, ela viraria trabalho feito e não pago."""

    dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
        occurred_at=datetime.now(FUSO_LOJA) - timedelta(days=3), freight=6.0,
    ))

    historico = dispatch.field_history(courier, 30)

    assert historico["shifts"] == []
    assert len(historico["loose_runs"]) == 1
    assert historico["totals"]["deliveries"] == 1
    assert historico["totals"]["freight"] == 6.0


def test_o_historico_e_so_do_proprio_entregador(dispatch, store, courier, centro):
    outro = DeliveryService(store).create_courier(CourierCreate(name="Maria"))
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=centro["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)]
    ))
    dispatch.finish_run(courier, run["id"], DeliveryRunFinish())

    assert dispatch.field_history(outro, 30)["shifts"] == []
    assert dispatch.field_history(outro, 30)["totals"]["deliveries"] == 0


def test_a_loja_ve_o_ponto_de_todo_mundo_com_hora(dispatch, store, courier, centro):
    outro = DeliveryService(store).create_courier(CourierCreate(name="Maria"))
    _turno(dispatch, courier)
    dispatch.check_in(outro, CourierCheckIn())

    turnos = dispatch.shifts(30)

    assert len(turnos) == 2
    assert {linha["courier_name"] for linha in turnos} == {"João", "Maria"}
    assert all(linha["checkin_at"] for linha in turnos)
    assert all(linha["open"] for linha in turnos)
    # E dá para olhar um entregador só.
    assert len(dispatch.shifts(30, courier_id=courier["id"])) == 1


def test_o_ponto_marca_o_turno_que_o_sistema_fechou_sozinho(dispatch, store, courier):
    """Duração inventada precisa se identificar em vez de parecer medida."""

    turno = _turno(dispatch, courier)
    bruto = store.get(SHIFTS, turno["id"])
    bruto["checkin_at"] = (datetime.fromisoformat(bruto["checkin_at"]) - timedelta(hours=20)).isoformat()
    store.put(SHIFTS, turno["id"], bruto)
    dispatch.open_shift(courier["id"])

    linha = dispatch.shifts(30)[0]

    assert linha["auto_closed"]
    assert linha["open"] is False


# ----------------------------------------------------------------------
# O destino, escrito no balcao
#
# A zona era o unico dado que o pedido nao trazia, e o endereco nao existia em
# lugar nenhum: o entregador saia escolhendo a zona de cabeca e procurando a rua
# no grupo do WhatsApp. Agora o balcao escreve as duas coisas na hora em que o
# cliente as diz — que e a unica hora em que alguem sabe.
# ----------------------------------------------------------------------


def test_a_fila_entrega_a_zona_e_o_endereco_que_a_venda_trouxe(dispatch, store, courier, centro):
    ERPService(store).create_order(OrderCreate(
        items=[OrderLine(product_id="acai-500", quantity=1)],
        is_delivery=True, delivery_fee=8.0,
        delivery_zone_id=centro["id"], delivery_address="Rua das Flores, 120",
    ))
    _turno(dispatch, courier)

    fila = dispatch.queue(courier["id"])["orders"]

    assert len(fila) == 1
    assert fila[0]["zone_id"] == centro["id"]
    assert fila[0]["address"] == "Rua das Flores, 120"
    # A zona do balcao manda sobre a sugestao tirada do historico do cliente.
    assert fila[0]["suggested_zone_id"] == centro["id"]


def test_a_entrega_guarda_o_endereco_do_pedido(dispatch, store, courier, centro):
    """Sem isso o mapa parava de funcionar assim que ela saía da fila."""

    pedido = ERPService(store).create_order(OrderCreate(
        items=[OrderLine(product_id="acai-500", quantity=1)],
        is_delivery=True, delivery_fee=8.0, delivery_address="Rua das Flores, 120",
    ))
    _turno(dispatch, courier)

    run = dispatch.register_run(courier, DeliveryRunCreate(zone_id=centro["id"], order_id=pedido["id"]))

    assert store.get(RUNS, run["id"])["address"] == "Rua das Flores, 120"


def test_venda_de_balcao_nao_guarda_destino(dispatch, store):
    """Endereço em venda de balcão é dado sem dono: ninguém vai entregar nada."""

    pedido = ERPService(store).create_order(OrderCreate(items=[OrderLine(product_id="acai-500", quantity=1)]))

    assert pedido["delivery_zone_id"] is None
    assert pedido["delivery_address"] is None


# ----------------------------------------------------------------------
# O contrato HTTP
#
# O servico pode estar certo e a rota errada. Aqui o que se prova e o caminho
# que o celular do entregador chama de verdade, de ponta a ponta.
# ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.setenv("DEMO_DATA_PATH", str(tmp_path / "http.json"))
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("DATA_RAW_PATH", str(tmp_path / "raw"))
    monkeypatch.setenv("MEDIA_INBOX_PATH", str(tmp_path / "inbox"))
    monkeypatch.setenv("MEDIA_LIBRARY_PATH", str(tmp_path / "library"))
    monkeypatch.setenv("MEDIA_SYNC_LEGACY_FILES", "false")
    get_settings.cache_clear()
    with TestClient(app) as cliente:
        yield cliente
    get_settings.cache_clear()


def test_o_turno_inteiro_pelo_http(client):
    zona = client.post("/api/delivery/zones", json={"name": "Centro", "default_fee": 6, "target_minutes": 25})
    assert zona.status_code == 201
    zone_id = zona.json()["id"]
    entregador = client.post("/api/couriers", json={"name": "João", "fee_per_delivery": 4, "email": "joao@loja.com"})
    assert entregador.status_code == 201
    courier_id = entregador.json()["id"]

    # Sem autenticacao, o aplicativo de rua diz de quem e o turno.
    entrada = client.post("/api/field/check-in", json={"courier_id": courier_id})
    assert entrada.status_code == 201
    assert entrada.json()["open"] is True

    estado = client.get(f"/api/field/state?courier_id={courier_id}").json()
    assert estado["courier"]["name"] == "João"
    assert estado["shift"]["open"] is True
    assert [linha["id"] for linha in estado["queue"]["zones"]] == [zone_id]

    saiu = client.post(f"/api/field/runs?courier_id={courier_id}", json={
        "zone_id": zone_id, "items": [{"product_id": "acai-500", "quantity": 2}],
    })
    assert saiu.status_code == 201
    run_id = saiu.json()["id"]
    assert saiu.json()["freight"] == 6.0

    # Turno com entrega em rota nao fecha: o acerto sairia sem ela.
    recusado = client.post(f"/api/field/check-out?courier_id={courier_id}", json={})
    assert recusado.status_code == 422
    assert "em rota" in recusado.json()["detail"]

    assert client.post(f"/api/field/runs/{run_id}/finish?courier_id={courier_id}", json={}).status_code == 200
    fechado = client.post(f"/api/field/check-out?courier_id={courier_id}", json={})
    assert fechado.status_code == 200
    assert fechado.json()["deliveries"] == 1

    # O acerto aparece na tela de entregas que ja existia, sem ninguem digitar.
    assert client.get("/api/deliveries").json()[0]["payout"] == 4.0
    painel = client.get("/api/delivery/board").json()
    assert painel["totals"]["deliveries"] == 1
    assert painel["zones"][0]["name"] == "Centro"


def test_zona_repetida_responde_422_com_a_frase_do_balcao(client):
    client.post("/api/delivery/zones", json={"name": "Centro"})

    resposta = client.post("/api/delivery/zones", json={"name": "centro"})

    assert resposta.status_code == 422
    assert "Já existe uma zona" in resposta.json()["detail"]


def test_entrega_sem_turno_responde_422_e_nao_500(client):
    zona = client.post("/api/delivery/zones", json={"name": "Centro"}).json()
    entregador = client.post("/api/couriers", json={"name": "João"}).json()

    resposta = client.post(f"/api/field/runs?courier_id={entregador['id']}", json={
        "zone_id": zona["id"], "items": [{"product_id": "acai-500", "quantity": 1}],
    })

    assert resposta.status_code == 422
    assert "check-in" in resposta.json()["detail"]


def test_conta_sem_entregador_cadastrado_recebe_o_caminho_da_solucao(client):
    """Em modo aberto não há e-mail para casar, e o check-in sem escolha é recusado."""

    resposta = client.post("/api/field/check-in", json={})

    assert resposta.status_code == 422
    assert "entregador" in resposta.json()["detail"]


def test_a_loja_abrindo_a_tela_de_rua_recebe_a_lista_e_nao_uma_recusa(client, dispatch, store):
    """O gestor abriu o próprio link e bateu num beco sem saída.

    A conta dele passa na guarda — ele entra de propósito, para fechar o turno
    de quem ficou sem bateria — e então o servidor não achava nenhum cadastro
    de entregador com aquele e-mail e respondia "peça para a loja cadastrar
    você". Pedir para si mesmo. A resposta certa é a pergunta que falta: por
    quem você quer lançar?
    """

    from app.services.dispatch import SemEntregadorEscolhido

    DeliveryService(store).create_courier(CourierCreate(name="Maria"))
    with pytest.raises(SemEntregadorEscolhido):
        dispatch.courier_for({"uid": "u1", "email": "loja@exemplo.com", "erp_admin": True})

    # E quem não é da loja continua recebendo o caminho do cadastro.
    with pytest.raises(ValueError, match="cadastrar você"):
        dispatch.courier_for({"uid": "u2", "email": "estranho@exemplo.com", "courier": True})


def test_a_lista_para_escolher_traz_so_entregador_ativo(dispatch, store):
    entrega = DeliveryService(store)
    entrega.create_courier(CourierCreate(name="Maria"))
    inativo = entrega.create_courier(CourierCreate(name="Zulmira"))
    entrega.update_courier(inativo["id"], CourierUpdate(active=False))

    nomes = [linha["name"] for linha in dispatch.couriers_for_picker()]

    assert "Maria" in nomes
    assert "Zulmira" not in nomes


def test_o_territorio_responde_mesmo_sem_nenhuma_entrega(client):
    territorio = client.get("/api/delivery/territory").json()

    assert territorio["status"] == "sem-dados"
    assert territorio["basis"]["deliveries"] == 0


# ----------------------------------------------------------------------
# Zona criada na rua
#
# A trava era dura: sem zona cadastrada o entregador ficava parado na calcada
# esperando o balcao. A saida nao pode ser texto livre — "Centro", "centro" e
# "Ctr" matam o relatorio que a zona existe para alimentar —, entao ela nasce
# valendo e marcada, e a loja confirma depois.
# ----------------------------------------------------------------------


def test_o_entregador_cria_zona_e_ela_vale_na_mesma_entrega(dispatch, store, courier):
    from app.schemas import FieldZoneCreate

    zona = dispatch.propose_zone(courier, FieldZoneCreate(name="Jardim América"))

    assert zona["proposed"] is True
    # Frete zero de proposito: na calcada nao se decide preco.
    assert zona["default_fee"] == 0
    _turno(dispatch, courier)
    run = dispatch.register_run(courier, DeliveryRunCreate(
        zone_id=zona["id"], items=[DeliveryRunItem(product_id="acai-500", quantity=1)],
    ))
    assert run["zone_name"] == "Jardim América"


def test_zona_que_ja_existe_devolve_a_existente_em_vez_de_recusar(dispatch, store, courier, centro):
    """Recusar por duplicidade seria o mesmo beco de antes com outra mensagem."""

    from app.schemas import FieldZoneCreate

    zona = dispatch.propose_zone(courier, FieldZoneCreate(name="  centro  "))

    assert zona["id"] == centro["id"]
    assert len([linha for linha in store.list(ZONES) if linha["name"].lower() == "centro"]) == 1


def test_a_loja_confirma_a_zona_da_rua_ao_definir_o_frete(dispatch, courier):
    from app.schemas import DeliveryZoneUpdate, FieldZoneCreate

    zona = dispatch.propose_zone(courier, FieldZoneCreate(name="Vila Nova"))

    confirmada = dispatch.update_zone(zona["id"], DeliveryZoneUpdate(default_fee=8.0, proposed=False))

    assert confirmada["proposed"] is False
    assert confirmada["default_fee"] == 8.0


# ----------------------------------------------------------------------
# A cerca do ponto: bater o ponto de onde se trabalha
#
# "Prova" e a palavra que decide o desenho. O GPS devolve uma posicao e uma
# margem de erro, e as duas contam: alguem a 200 m com margem de 300 m pode
# estar na porta da loja, e recusar ai seria acusar quem chegou.
# ----------------------------------------------------------------------

LOJA = (-22.9632676, -43.406644)


def _com_cerca(dispatch, raio=150, obrigatorio=True):
    from app.schemas import StoreLocation

    return dispatch.set_store_location(StoreLocation(
        latitude=LOJA[0], longitude=LOJA[1], radius_m=raio,
        address="Rua São Marcelo, 12 — Curicica", required=obrigatorio,
    ))


def test_o_ponto_batido_na_loja_e_aceito(dispatch, courier):
    _com_cerca(dispatch)

    turno = dispatch.check_in(courier, CourierCheckIn(latitude=LOJA[0], longitude=LOJA[1], accuracy=20))

    assert turno["checkin_at"]


def test_o_ponto_batido_longe_e_recusado(dispatch, courier):
    """É o que a loja pediu: ponto batido de casa não é ponto."""

    _com_cerca(dispatch)

    # Cerca de 3 km ao norte da loja.
    with pytest.raises(ValueError, match="da loja"):
        dispatch.check_in(courier, CourierCheckIn(latitude=LOJA[0] + 0.027, longitude=LOJA[1], accuracy=15))


def test_sinal_ruim_na_porta_da_loja_nao_acusa_ninguem(dispatch, courier):
    """200 m de distância com 300 m de margem pode ser a porta da loja.

    Recusar aqui seria acusar quem chegou. A recusa exige que a distância menos
    a margem já passe do raio — só aí o aparelho está provando.
    """

    _com_cerca(dispatch, raio=150)

    turno = dispatch.check_in(courier, CourierCheckIn(
        latitude=LOJA[0] + 0.0018, longitude=LOJA[1], accuracy=300,
    ))

    assert turno["checkin_at"]
    bruto = dispatch.store.get(SHIFTS, turno["id"])
    # Mas a leitura entra marcada: a loja vê que aquele ponto não foi conferido.
    assert bruto["checkin_location"]["location_uncertain"] is True


def test_sem_localizacao_o_ponto_e_recusado_quando_a_loja_exige(dispatch, courier):
    _com_cerca(dispatch, obrigatorio=True)

    with pytest.raises(ValueError, match="Autorize o acesso"):
        dispatch.check_in(courier, CourierCheckIn())


def test_sem_localizacao_o_ponto_passa_quando_a_loja_so_registra(dispatch, courier):
    """Começar registrando deixa a loja ver as distâncias antes de barrar."""

    _com_cerca(dispatch, obrigatorio=False)

    turno = dispatch.check_in(courier, CourierCheckIn())

    assert dispatch.store.get(SHIFTS, turno["id"])["checkin_location"]["location_missing"] is True


def test_sem_a_loja_dizer_onde_fica_nada_e_conferido(dispatch, courier):
    """Inventar um centro recusaria justamente quem está na porta."""

    turno = dispatch.check_in(courier, CourierCheckIn())

    assert dispatch.store.get(SHIFTS, turno["id"])["checkin_location"] is None


def test_a_distancia_do_ponto_fica_registrada(dispatch, courier):
    _com_cerca(dispatch)

    turno = dispatch.check_in(courier, CourierCheckIn(latitude=LOJA[0], longitude=LOJA[1], accuracy=12))

    local = dispatch.store.get(SHIFTS, turno["id"])["checkin_location"]
    assert local["distance_m"] == 0.0
    assert local["accuracy_m"] == 12.0


def test_o_check_out_tambem_e_conferido(dispatch, courier):
    """Fechar turno de casa faria a cerca do check-in servir para nada."""

    _com_cerca(dispatch)
    dispatch.check_in(courier, CourierCheckIn(latitude=LOJA[0], longitude=LOJA[1], accuracy=10))

    with pytest.raises(ValueError, match="da loja"):
        dispatch.check_out(courier, CourierCheckOut(latitude=LOJA[0] + 0.027, longitude=LOJA[1], accuracy=10))
