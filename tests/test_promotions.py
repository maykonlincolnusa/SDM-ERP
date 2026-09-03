"""Promocoes da semana.

Promocao e decisao de dar margem embora, e o risco nao e sugerir de menos: e
sugerir um desconto que parece movimento e e prejuizo. Por isso a maior parte
dos testes aqui prova o que o modulo *nao* faz — nao promove no dia cheio, nao
fura o piso de custo, nao escolhe produto por venda quando nao mediu venda
nenhuma, e nao confunde dia fraco com dia fechado.
"""

from datetime import datetime, timedelta

import pytest

from app.repositories.store import DemoStore
from app.schemas import (
    InventoryAdjustment,
    OrderCreate,
    OrderLine,
    ProductCreate,
    RecipeLine,
    SportAnchorCreate,
)
from app.services import ERPService, PromotionService
from app.services.erp import FUSO_LOJA


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    for colecao in ("orders", "inventory_movements"):
        for linha in store.list(colecao):
            store.delete(colecao, linha["id"])
    return ERPService(store), PromotionService(store)


def _vende(erp: ERPService, dias_atras: int, copos: int, produto: str = "acai-500") -> None:
    dia = datetime.now(FUSO_LOJA).date() - timedelta(days=dias_atras)
    quando = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
    erp.create_order(OrderCreate(items=[OrderLine(product_id=produto, quantity=copos)], occurred_at=quando))


def _abastece(erp: ERPService, quantidade: float = 900) -> None:
    """Enche o estoque de uma vez.

    Repor dia a dia gravaria centenas de movimentos e o teste passaria a medir a
    velocidade do repositorio em vez da regra de promocao.
    """

    for item in erp.inventory():
        erp.adjust_inventory(item["id"], InventoryAdjustment(quantity_delta=quantidade, reason="Reposição de teste"))


def _semana_com_vale(erp: ERPService, semanas: int = 6, vale: int = 1) -> None:
    """Historico em que um dia da semana vende metade dos outros, com a loja sempre aberta."""

    _abastece(erp)
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(semanas * 7, 0, -1):
        dia = hoje - timedelta(days=atras)
        _vende(erp, atras, 2 if dia.weekday() == vale else 6)


def _dia_da_semana(plano, weekday):
    return next((linha for linha in plano["week"] if linha["weekday"] == weekday), None)


# ----------------------------------------------------------------------
# O vale da semana
# ----------------------------------------------------------------------


def test_promove_no_vale_e_nunca_no_dia_cheio(loja):
    erp, promo = loja
    _semana_com_vale(erp, vale=1)  # terça é o vale

    plano = promo.week(days=90)
    dias_promovidos = {linha["weekday"] for linha in plano["promotions"] if linha["kind"] == "vale-da-semana"}

    assert plano["promotions"], "um vale de metade do movimento tem de gerar sugestão"
    assert "terça" in dias_promovidos
    # O dia cheio compra venda que ja aconteceria.
    assert "domingo" not in dias_promovidos and "sábado" not in dias_promovidos


def test_a_sugestao_traz_o_ponto_de_virada(loja):
    """Sem o volume extra necessario, desconto e palpite com cara de estrategia."""

    erp, promo = loja
    _semana_com_vale(erp, vale=1)

    sugestao = next(p for p in promo.week(days=90)["promotions"] if p["kind"] == "vale-da-semana")
    virada = sugestao["breakeven"]

    assert virada is not None
    assert virada["units"] > 0
    assert virada["total_units"] > virada["base_units"]
    assert "para empatar" in virada["note"]


def test_o_desconto_nunca_fura_o_piso_de_custo(loja):
    erp, promo = loja
    _semana_com_vale(erp, vale=1)

    for sugestao in promo.week(days=90)["promotions"]:
        piso = sugestao["floor"]
        assert sugestao["price"]["to"] > piso["cost_with_shrinkage"]
        assert sugestao["price"]["discount_percent"] <= piso["max_discount_percent"]
        assert sugestao["price"]["to"] < sugestao["price"]["from"]


def test_sem_vale_nenhum_o_modulo_nao_inventa_promocao(loja):
    """Semana plana: descontar qualquer dia seria comprar venda que ja viria."""

    erp, promo = loja
    _abastece(erp)
    for atras in range(42, 0, -1):
        _vende(erp, atras, 5)

    plano = promo.week(days=90)

    assert not [p for p in plano["promotions"] if p["kind"] == "vale-da-semana"]
    assert not any(dia["is_valley"] for dia in plano["week"])


def test_dia_fechado_nao_e_dia_fraco(loja):
    """O relatorio real da loja tem 13 segundas abertas contra 20 domingos.

    A media que ignora o dia fechado responde a pergunta certa — "quando abro,
    vendo pouco?" — e o que sobra vira aviso de abertura, nao de desconto.
    """

    erp, promo = loja
    _abastece(erp)
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(42, 0, -1):
        if (hoje - timedelta(days=atras)).weekday() == 0:
            continue  # a loja nao abre na segunda
        _vende(erp, atras, 5)

    plano = promo.week(days=90)
    segunda = _dia_da_semana(plano, 0)

    assert segunda["open_days"] == 0
    assert not [p for p in plano["promotions"] if p["weekday"] == "segunda"]


def test_o_canal_de_publicacao_evita_o_marketplace(loja):
    """Promover no marketplace e pagar o desconto duas vezes."""

    erp, promo = loja
    _semana_com_vale(erp, vale=1)

    canal = promo.week(days=90)["channel"]

    assert canal["fee_percent"] == 0
    assert "iFood" in canal.get("avoid", "")


# ----------------------------------------------------------------------
# Estoque em risco
# ----------------------------------------------------------------------


def test_insumo_que_vence_antes_de_girar_vira_promocao(loja):
    """Aqui a promocao economiza: sem ela o insumo vira perda inteira."""

    erp, promo = loja
    _semana_com_vale(erp, vale=1)
    # Granola tem validade cadastrada na loja de demonstracao; um saldo muito
    # acima do giro faz o excedente vencer antes de ser vendido.
    erp.adjust_inventory("granola", InventoryAdjustment(quantity_delta=400, reason="Compra grande de teste"))

    risco = [p for p in promo.week(days=90)["promotions"] if p["kind"] == "estoque-em-risco"]

    assert risco, "saldo muito acima do giro tem de virar aviso de promoção"
    assert risco[0]["upside_monthly"] > 0
    assert "vencem antes de vender" in risco[0]["evidence"]


# ----------------------------------------------------------------------
# Linha Sport
# ----------------------------------------------------------------------


def _cria_produto_sport(erp: ERPService) -> str:
    produto = erp.create_product(ProductCreate(
        name="Açaí Proteico 400 ml",
        category="Açaí",
        price=26.0,
        line="Sport",
        claims=["alto em proteína", "sem açúcar adicionado"],
        nutrition={"kcal": 320, "protein_g": 22},
        recipe=[RecipeLine(inventory_id="acai", quantity=0.35), RecipeLine(inventory_id="copo-500", quantity=1)],
    ))
    return produto["id"]


def test_sem_ancora_e_sem_venda_o_modulo_nao_chuta_dia_de_treino(loja):
    """Inventar que segunda e dia de academia seria o conselho generico que o modulo recusa."""

    erp, promo = loja
    _semana_com_vale(erp, vale=1)
    _cria_produto_sport(erp)

    plano = promo.week(days=90)

    assert not [p for p in plano["promotions"] if p["kind"] == "linha-sport"]
    assert any("âncora" in b["title"].lower() for b in plano["blockers"])


def test_ancora_cadastrada_define_o_dia_da_acao_sport(loja):
    erp, promo = loja
    _semana_com_vale(erp, vale=1)
    _cria_produto_sport(erp)
    erp.create_sport_anchor(SportAnchorCreate(
        name="Academia do Bairro", kind="Academia",
        weekdays=[0, 2, 4], time_of_day="Noite", people=200,
    ))

    sport = [p for p in promo.week(days=90)["promotions"] if p["kind"] == "linha-sport"]

    assert sport
    assert {p["weekday"] for p in sport} <= {"segunda", "quarta", "sexta"}
    assert all(p["source"] == "âncora declarada" for p in sport)
    # A oferta se comunica com o que a ficha sustenta, nao com adjetivo.
    assert "22 g de proteína" in sport[0]["why_this_day"]


def test_a_ancora_nao_dispensa_o_piso_de_custo(loja):
    erp, promo = loja
    _semana_com_vale(erp, vale=1)
    _cria_produto_sport(erp)
    erp.create_sport_anchor(SportAnchorCreate(name="Grupo de Corrida", kind="Corrida", weekdays=[5], people=40))

    for sugestao in promo.week(days=90)["promotions"]:
        assert sugestao["price"]["to"] > sugestao["floor"]["cost_with_shrinkage"]


def test_produto_sem_ficha_nutricional_pede_o_cadastro_em_vez_de_inventar(loja):
    erp, promo = loja
    _semana_com_vale(erp, vale=1)
    erp.create_product(ProductCreate(
        name="Shake Pós-Treino", category="Açaí", price=24.0, line="Sport",
        recipe=[RecipeLine(inventory_id="acai", quantity=0.3), RecipeLine(inventory_id="copo-500", quantity=1)],
    ))
    erp.create_sport_anchor(SportAnchorCreate(name="CT do Bairro", kind="Time", weekdays=[1]))

    sport = [p for p in promo.week(days=90)["promotions"] if p["kind"] == "linha-sport"]

    assert sport
    assert "Cadastre proteína" in sport[0]["why_this_day"]


# ----------------------------------------------------------------------
# Honestidade da origem
# ----------------------------------------------------------------------


def test_sem_itens_no_historico_a_sugestao_admite_que_nao_mediu(loja):
    """Pedido importado da Anota Ai nao tem itens: a escolha do produto vem do cadastro."""

    erp, promo = loja
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(42, 0, -1):
        dia = hoje - timedelta(days=atras)
        quando = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=12)
        erp.store.put("orders", f"importado-{atras}", {
            "id": f"importado-{atras}", "number": str(atras), "created_at": quando.isoformat(),
            "channel": "Balcao", "source": "anota-ai", "total": 20.0 if dia.weekday() == 1 else 60.0,
            "cost": None, "items": [], "status": "Concluido", "is_delivery": False, "delivery_fee": 0.0,
        })

    plano = promo.week(days=90)

    assert plano["coverage"]["detail_coverage"] == 0.0
    assert plano["coverage"]["source"] == "cadastro do cardápio"
    for sugestao in plano["promotions"]:
        if sugestao["kind"] == "vale-da-semana":
            assert sugestao["source"] == "cadastro do cardápio"
            assert "cadastro" in sugestao["confidence_note"]


# ----------------------------------------------------------------------
# O mês: quatro semanas, dois blocos, mecânicas opostas
# ----------------------------------------------------------------------


def _semana_com_bloco_fraco(erp: ERPService, semanas: int = 8) -> None:
    """Começo de semana rende menos que o fim, com a loja aberta todo dia."""

    _abastece(erp, 3000)
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(semanas * 7, 0, -1):
        dia = hoje - timedelta(days=atras)
        _vende(erp, atras, 2 if dia.weekday() <= 2 else 6)


def test_o_mes_entrega_quatro_semanas_datadas(loja):
    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    plano = promo.month(days=90, weeks=4)

    assert len(plano["calendar"]) == 4
    assert [semana["week"] for semana in plano["calendar"]] == [1, 2, 3, 4]
    # A primeira semana começa numa segunda: a semana em curso não dá tempo de preparar.
    for semana in plano["calendar"]:
        assert datetime.fromisoformat(semana["starts_on"]).weekday() == 0
    inicios = [semana["starts_on"] for semana in plano["calendar"]]
    assert len(set(inicios)) == 4


def test_o_bloco_fraco_e_o_forte_saem_dos_dados(loja):
    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    plano = promo.month(days=90)

    assert plano["weak_block"] == "inicio"
    assert plano["strong_block"] == "fim"
    assert plano["split_worth_it"] is True
    inicio = next(b for b in plano["blocks"] if b["id"] == "inicio")
    fim = next(b for b in plano["blocks"] if b["id"] == "fim")
    assert inicio["average_per_open_day"] < fim["average_per_open_day"]


def test_o_bloco_cheio_nunca_recebe_desconto_no_produto(loja):
    """Descontar no pedaço cheio compra a venda que já aconteceria."""

    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    for semana in promo.month(days=90)["calendar"]:
        for oferta in semana["promotions"]:
            if oferta["kind"] == "ticket":
                # A mecânica de ticket sobe o valor do pedido; o item principal
                # continua no preço cheio.
                assert oferta["price"]["to"] >= oferta["price"]["from"] or oferta["price"]["discount_percent"] <= 5
                assert "desconto" not in oferta["mechanic"].lower()


def test_o_bloco_vazio_recebe_preco_e_respeita_o_piso(loja):
    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    trafego = [
        oferta for semana in promo.month(days=90)["calendar"]
        for oferta in semana["promotions"] if oferta["kind"] == "trafego"
    ]

    assert trafego
    for oferta in trafego:
        assert oferta["block"] == "inicio"
        assert oferta["price"]["to"] < oferta["price"]["from"]
        assert oferta["price"]["to"] > oferta["floor"]["cost_with_shrinkage"]


def test_as_semanas_nao_repetem_a_mesma_ancora_seguidas(loja):
    """Desconto que se repete deixa de ser promoção e vira o preço novo."""

    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    plano = promo.month(days=90, weeks=4)
    ancoras = [
        next((o["product"]["id"] for o in semana["promotions"] if o["kind"] == "trafego"), None)
        for semana in plano["calendar"]
    ]
    ancoras = [linha for linha in ancoras if linha]

    for anterior, seguinte in zip(ancoras, ancoras[1:]):
        assert anterior != seguinte


def test_cada_semana_indica_qual_rodar_se_so_der_para_uma(loja):
    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    for semana in promo.month(days=90)["calendar"]:
        if semana["promotions"]:
            assert semana["headline"] in {oferta["id"] for oferta in semana["promotions"]}


def test_semana_plana_diz_que_nao_vale_separar_os_blocos(loja):
    erp, promo = loja
    _abastece(erp, 3000)
    for atras in range(56, 0, -1):
        _vende(erp, atras, 5)

    plano = promo.month(days=90)

    assert plano["split_worth_it"] is False
    assert "oferta única" in plano["split_note"]


def test_a_oferta_carrega_o_veredito_de_preco_do_produto(loja):
    """Promoção não conserta preço errado: os dois números andam juntos."""

    erp, promo = loja
    _semana_com_bloco_fraco(erp)

    ofertas = [oferta for semana in promo.month(days=90)["calendar"] for oferta in semana["promotions"]]

    assert ofertas
    for oferta in ofertas:
        assert oferta["pricing"] is not None
        assert oferta["pricing"]["verdict"]
        assert oferta["pricing"]["cost_with_shrinkage"] > 0


def test_escada_de_preco_apertada_vira_achado_e_nao_oferta(loja):
    """Copo maior que cobra pouco a mais pelo insumo que leva é preço, não promoção."""

    erp, promo = loja
    _semana_com_bloco_fraco(erp)
    # O 500 passa a custar quase o mesmo que o 300, apertando a escada.
    produto = erp.store.get("products", "acai-500")
    produto["price"] = 16.5
    erp.store.put("products", "acai-500", produto)

    plano = promo.month(days=90)

    assert any("cobra pouco a mais" in b["title"] for b in plano["blockers"])
    upgrades = [
        oferta for semana in plano["calendar"] for oferta in semana["promotions"]
        if "Upgrade" in oferta.get("mechanic", "")
    ]
    # O degrau apertado sai de cena; os outros degraus da escada continuam
    # valendo, e é por isso que a checagem é do par, não do nome do produto.
    for oferta in upgrades:
        assert "300 ml para 500 ml" not in oferta["mechanic"]
