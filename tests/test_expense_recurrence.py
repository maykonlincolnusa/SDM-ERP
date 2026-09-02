"""Despesa que se repete todo mes.

Aluguel, energia, salario e contador nao sao lancamentos avulsos: sao contratos.
Enquanto `recurring` era so um rotulo, cada um deles existia apenas no mes em
que alguem digitou — e a DRE do trimestre mostrava um aluguel onde deveriam
estar tres. O que estes testes fixam e a regra que fecha esse buraco, e os
limites dela: o passado nao se reescreve, o mes apagado nao volta e o futuro
nao vira lancamento antes de vencer.
"""

from datetime import date, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    CourierCreate,
    DeliveryEntryCreate,
    ExpenseCreate,
    ExpenseSeriesUpdate,
    ExpenseUpdate,
    FixedCostLine,
    FixedCostSetup,
)
from app.services import DeliveryService, ERPService
from app.services.erp import FUSO_LOJA
from datetime import datetime


@pytest.fixture
def service(tmp_path) -> ERPService:
    erp = ERPService(DemoStore(tmp_path / "store.json"))
    # A semente traz despesas de exemplo; aqui interessa so o que o teste lanca.
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    return erp


def hoje() -> date:
    return datetime.now(FUSO_LOJA).date()


def meses_atras(quantidade: int, dia: int = 5) -> date:
    referencia = hoje()
    indice = referencia.year * 12 + referencia.month - 1 - quantidade
    return date(indice // 12, indice % 12 + 1, dia)


def aluguel(service: ERPService, quantidade_de_meses: int = 12, valor: float = 1800) -> dict:
    return service.create_expense(ExpenseCreate(
        description="Aluguel", category="Fixo", amount=valor,
        due_date=meses_atras(quantidade_de_meses).isoformat(), recurring=True,
    ))


def test_um_lancamento_vira_um_aluguel_por_mes(service):
    """O efeito medido no diagnostico: o trimestre precisa trazer tres alugueis."""

    aluguel(service, quantidade_de_meses=12)

    despesas = {
        dias: service.operating_result(dias)["totals"]["operating_expenses"]
        for dias in (30, 90, 365)
    }

    assert despesas[30] == 1800, "o mes traz um aluguel"
    assert despesas[90] == 5400, "o trimestre traz tres"
    assert despesas[365] == 21600, "o ano traz doze"


def test_a_geracao_para_no_mes_corrente(service):
    """Conta que ainda nao venceu e projecao, nao lancamento.

    Materializar o futuro encheria a lista de pendencias de despesa imaginaria e
    faria a DRE de hoje contar aluguel de dezembro.
    """

    aluguel(service, quantidade_de_meses=3)

    competencias = sorted(
        row["competence"] for row in service.store.list("expenses") if row.get("series_id")
    )

    assert competencias[-1] == hoje().strftime("%Y-%m")
    assert len(competencias) == 4, "de tres meses atras ate o mes corrente"


def test_rodar_de_novo_nao_duplica(service):
    """O identificador do lancamento e a serie mais o mes: gerar duas vezes e inofensivo."""

    aluguel(service, quantidade_de_meses=6)
    antes = len(service.store.list("expenses"))

    assert service.materialize_recurring_expenses() == 0
    assert len(service.store.list("expenses")) == antes


def test_mes_apagado_nao_volta_na_proxima_geracao(service):
    """Sem essa marca, a conta tirada hoje reapareceria amanha."""

    aluguel(service, quantidade_de_meses=3)
    alvo = next(
        row for row in service.store.list("expenses")
        if row.get("competence") == hoje().strftime("%Y-%m")
    )

    resultado = service.delete_expense(alvo["id"])
    service.materialize_recurring_expenses()

    assert "continua se repetindo" in resultado["detail"]
    assert not any(row["id"] == alvo["id"] for row in service.store.list("expenses"))


def test_reajuste_vale_para_frente_e_nao_reescreve_o_passado(service):
    """O aluguel de julho continua sendo o que foi cobrado em julho."""

    criado = aluguel(service, quantidade_de_meses=3, valor=1800)
    serie = service.expense_series()[0]

    service.update_expense_series(serie["id"], ExpenseSeriesUpdate(amount=2000))
    passado = service.store.get("expenses", criado["id"])

    assert passado["amount"] == 1800, "lancamento ja feito nao muda sozinho"
    assert service.recurring_monthly_cost()["total"] == 2000, "o proximo mes ja usa o valor novo"


def test_repeticao_com_fim_para_de_gerar(service):
    """Parcela de financiamento acaba; aluguel nao. A regra precisa aceitar os dois."""

    fim = meses_atras(2).strftime("%Y-%m")
    service.create_expense(ExpenseCreate(
        description="Parcela do freezer", category="Fixo", amount=400,
        due_date=meses_atras(5).isoformat(), recurring=True, repeat_until=fim,
    ))

    competencias = sorted(row["competence"] for row in service.store.list("expenses") if row.get("series_id"))

    assert competencias[-1] == fim
    assert service.recurring_monthly_cost()["total"] == 0, "acabou: nao entra no custo fixo de hoje"


def test_fim_anterior_ao_inicio_e_recusado(service):
    with pytest.raises(ValueError, match="anterior ao vencimento"):
        ExpenseCreate(
            description="Parcela", category="Fixo", amount=400,
            due_date=meses_atras(2).isoformat(), recurring=True,
            repeat_until=meses_atras(5).strftime("%Y-%m"),
        )


def test_dia_31_cai_no_ultimo_dia_do_mes_curto(service):
    """Sem a trava, a geracao quebraria em quatro meses do ano."""

    service.create_expense(ExpenseCreate(
        description="Contador", category="Fixo", amount=300,
        due_date="2026-01-31", recurring=True,
    ))

    fevereiro = next(
        row for row in service.store.list("expenses")
        if row.get("competence") == "2026-02"
    )

    assert fevereiro["due_date"] == "2026-02-28"


def test_encerrar_mantem_o_que_ja_foi_lancado(service):
    """Fim de contrato nao apaga os meses em que a loja pagou."""

    aluguel(service, quantidade_de_meses=4)
    serie = service.expense_series()[0]
    lancados = len([row for row in service.store.list("expenses") if row.get("series_id")])

    service.stop_expense_series(serie["id"])
    service.materialize_recurring_expenses()

    assert service.recurring_monthly_cost()["total"] == 0
    assert len([row for row in service.store.list("expenses") if row.get("series_id")]) == lancados
    assert service.expense_series()[0]["active"] is False


def test_despesa_avulsa_pode_virar_recorrente(service):
    """Ligar o interruptor numa despesa ja lancada precisa fazer algo."""

    avulsa = service.create_expense(ExpenseCreate(
        description="Energia", category="Fixo", amount=280,
        due_date=meses_atras(2).isoformat(),
    ))

    promovida = service.update_expense(avulsa["id"], ExpenseUpdate(recurring=True))

    assert promovida["series_id"]
    assert service.recurring_monthly_cost()["total"] == 280
    competencias = {row["competence"] for row in service.store.list("expenses") if row.get("series_id")}
    assert len(competencias) == 3, "de dois meses atras ate hoje"


def test_custo_fixo_lista_o_que_compoe_o_mes(service):
    aluguel(service, quantidade_de_meses=1, valor=1800)
    service.create_expense(ExpenseCreate(
        description="Contador", category="Fixo", amount=350,
        due_date=meses_atras(1).isoformat(), recurring=True,
    ))

    custo = service.recurring_monthly_cost()

    assert custo["total"] == 2150
    assert custo["count"] == 2
    assert [row["description"] for row in custo["items"]] == ["Aluguel", "Contador"]


def test_projecao_de_lucro_usa_o_contrato_e_nao_a_media(tmp_path):
    """Custo fixo do mes que vem esta no contrato, nao numa regra de tres.

    Antes, a despesa projetada saia da media do historico: a mesma loja
    projetava um valor diferente de aluguel conforme a janela escolhida.
    """

    from app.services import ForecastService

    store = DemoStore(tmp_path / "store.json")
    erp = ERPService(store)
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])

    referencia = hoje()
    for atras in range(28, 0, -1):
        momento = datetime.combine(referencia - timedelta(days=atras), datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        store.put("orders", f"h-{atras}", {
            "id": f"h-{atras}", "number": 3000 + atras, "created_at": momento.isoformat(),
            "channel": "Balcao", "payment_method": "Pix", "customer_name": None, "discount": 0,
            "total": 22.0, "cost": 11.0, "status": "Concluido",
            "items": [{"product_id": "acai-500", "name": "Açaí 500 ml", "quantity": 1,
                       "unit_price": 22.0, "unit_cost": 11.0, "line_total": 22.0}],
        })
    erp.create_expense(ExpenseCreate(
        description="Aluguel", category="Fixo", amount=1800,
        due_date=meses_atras(2).isoformat(), recurring=True,
    ))

    projecao = ForecastService(store).forecast(30)["purchase_plan"]["operating_projection"]

    assert projecao["available"]
    totais = projecao["totals"]
    assert totais["fixed_monthly_cost"] == 1800
    assert totais["fixed_expenses"] == pytest.approx(1800, abs=1), "trinta dias de horizonte, um aluguel"
    assert totais["variable_expenses"] == 0


# ----------------------------------------------------------------------
# Montagem do custo fixo
# ----------------------------------------------------------------------

# As dez linhas da planilha da loja. Nao ha de onde importa-las: aluguel esta no
# contrato e folha esta no acordo com a equipe, nao num relatorio de vendas.
PLANILHA_DA_LOJA = [
    ("Funcionários", "Equipe", 1450.0),
    ("Aluguel", "Fixo", 1200.0),
    ("MEI", "Taxas", 76.0),
    ("Luz", "Fixo", 310.0),
    ("Internet", "Fixo", 110.0),
    ("Anota Aí", "Taxas", 99.0),
    ("Tráfego pago", "Marketing", 200.0),
]


def montagem(meses_de_historico: int = 5) -> FixedCostSetup:
    return FixedCostSetup(
        starts_on=meses_atras(meses_de_historico).strftime("%Y-%m"),
        items=[FixedCostLine(description=d, category=c, amount=v, day=5) for d, c, v in PLANILHA_DA_LOJA],
    )


def test_montar_o_custo_fixo_cria_uma_serie_por_conta(service):
    """Sete contas numa tela so; dai em diante cada mes se lanca sozinho."""

    resultado = service.setup_fixed_costs(montagem(meses_de_historico=5))

    assert resultado["created"] == 7
    assert resultado["monthly_total"] == 3445.0
    assert resultado["months_released"] == 7 * 6, "de cinco meses atras ate o mes corrente"
    assert len(service.expense_series()) == 7
    # Os meses ja vencidos entram quitados; so o corrente fica na fila.
    assert resultado["settled"] == 7 * 5
    pendentes = [row for row in service.expenses() if row["status"] == "Pendente"]
    assert len(pendentes) == 7, "uma pendencia por conta, so o mes corrente"


def test_cadastro_em_atraso_deixa_tudo_pendente(service):
    """Quem esta lancando conta atrasada nao pode ver o sistema dizer que pagou."""

    pedido = montagem(meses_de_historico=3)
    pedido.past_paid = False

    resultado = service.setup_fixed_costs(pedido)

    assert resultado["settled"] == 0
    assert all(row["status"] == "Pendente" for row in service.expenses())


def test_montar_de_novo_nao_cria_um_segundo_aluguel(service):
    """Reabrir a tela para acrescentar a conta que faltava e o caso comum."""

    service.setup_fixed_costs(montagem())

    repetida = service.setup_fixed_costs(montagem())

    assert repetida["created"] == 0
    assert len(repetida["skipped"]) == 7
    assert service.recurring_monthly_cost()["total"] == 3445.0


def test_montagem_recusa_comecar_no_futuro(service):
    daqui = hoje().year * 12 + hoje().month - 1 + 2
    with pytest.raises(ValueError, match="não pode estar no futuro"):
        service.setup_fixed_costs(FixedCostSetup(
            starts_on=f"{daqui // 12:04d}-{daqui % 12 + 1:02d}",
            items=[FixedCostLine(description="Aluguel", category="Fixo", amount=1200)],
        ))


def test_montagem_recusa_a_mesma_conta_duas_vezes(service):
    """Duas linhas "Aluguel" dobrariam o custo fixo sem ninguem perceber."""

    # Sem mes inicial tambem: a checagem nao pode depender de um campo opcional.
    with pytest.raises(ValueError, match="aparece duas vezes"):
        FixedCostSetup(items=[
            FixedCostLine(description="Aluguel", category="Fixo", amount=1200),
            FixedCostLine(description=" ALUGUEL ", category="Fixo", amount=900),
        ])
    with pytest.raises(ValueError, match="aparece duas vezes"):
        FixedCostSetup(
            starts_on=meses_atras(1).strftime("%Y-%m"),
            items=[
                FixedCostLine(description="Aluguel", category="Fixo", amount=1200),
                FixedCostLine(description="aluguel", category="Fixo", amount=900),
            ],
        )


def test_o_custo_fixo_montado_escala_com_o_horizonte(service):
    """E o que o diagnostico pedia: a DRE do trimestre com tres meses de estrutura."""

    service.setup_fixed_costs(montagem(meses_de_historico=5))

    mes = service.operating_result(30)["totals"]["operating_expenses"]
    trimestre = service.operating_result(90)["totals"]["operating_expenses"]

    assert mes == 3445.0
    assert trimestre == 3445.0 * 3


# ----------------------------------------------------------------------
# Entregador nao pode ser contado duas vezes
# ----------------------------------------------------------------------


def test_despesa_de_entrega_sai_da_dre_quando_ha_turno_lancado(tmp_path):
    """O acerto do entregador ja entra pelo fechamento de turno.

    Somar tambem a despesa manual faria a loja pagar o entregador duas vezes na
    conta do lucro — e o erro nao apareceria em lugar nenhum da tela.
    """

    store = DemoStore(tmp_path / "store.json")
    erp, delivery = ERPService(store), DeliveryService(store)
    for expense in erp.expenses():
        erp.delete_expense(expense["id"])
    entregador = delivery.create_courier(CourierCreate(name="João", fee_per_delivery=3.0))
    delivery.create_entry(DeliveryEntryCreate(
        courier_id=entregador["id"], date=hoje().isoformat(), deliveries=10, freight_total=60,
    ))
    erp.create_expense(ExpenseCreate(
        description="Entregadores", category="Entregas", amount=600,
        due_date=hoje().isoformat(), recurring=True,
    ))

    pago = delivery.performance(30)["totals"]["payout"]
    resultado = erp.operating_result(30, pago)

    assert pago > 0
    assert resultado["totals"]["courier_expenses_excluded"] == 600
    assert resultado["totals"]["operating_expenses"] == 0
    assert any("Entregas" in bloqueio for bloqueio in resultado["blockers"])


def test_despesa_de_entrega_fica_quando_nao_ha_turno_lancado(service):
    """Sem fechamento, essa despesa e o unico registro do custo: tirar seria pior."""

    service.create_expense(ExpenseCreate(
        description="Entregadores", category="Entregas", amount=600,
        due_date=hoje().isoformat(), recurring=True,
    ))

    resultado = service.operating_result(30, 0.0)

    assert resultado["totals"]["courier_expenses_excluded"] == 0
    assert resultado["totals"]["operating_expenses"] == 600
