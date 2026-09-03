import pytest
from openpyxl import Workbook

from app.repositories.store import DemoStore
from scripts.import_anota_ai import importar, ler_relatorio


def _relatorio(tmp_path, linhas):
    workbook = Workbook()
    aba = workbook.active
    aba.title = "Pedidos"
    aba.append([
        "Número do Pedido", "Origem", "Condição de pagamento", "Cartão Utilizado", "Retirada",
        "Descontos (Apenas Informativo)", "Valor do frete", "Subtotal", "Valor total", "Data",
        "Entregador", "Status",
    ])
    for linha in linhas:
        aba.append(linha)
    caminho = tmp_path / "relatorio.xlsx"
    workbook.save(caminho)
    return caminho


def test_importer_keeps_revenue_and_marks_cost_as_unknown(tmp_path):
    """Custo desconhecido nao pode virar zero.

    Zero significaria que o pedido saiu de graca e faria a margem do painel
    subir sozinha, que e o erro mais perigoso porque parece uma boa noticia.
    """

    caminho = _relatorio(tmp_path, [
        [9, "PDV", "Cartão", "", "Retirado no estabelecimento", 0, 0, 20, 20, "17/08/2026", "", "Finalizado"],
    ])

    pedidos, _ = ler_relatorio(caminho)

    assert len(pedidos) == 1
    assert pedidos[0]["total"] == 20
    assert pedidos[0]["cost"] is None
    assert pedidos[0]["channel"] == "Balcao"


def test_importer_discards_orders_that_never_became_money(tmp_path):
    caminho = _relatorio(tmp_path, [
        [1, "PDV", "Dinheiro", "", "Retirado", 0, 0, 10, 10, "17/08/2026", "", "Finalizado"],
        [2, "iFood", "Crédito", "", "Delivery", 0, 0, 30, 30, "17/08/2026", "", "Cancelado"],
        [3, "Whatsapp", "Pix", "", "Delivery", 0, 0, 25, 25, "17/08/2026", "", "Negado"],
        [4, "Site", "Pix", "", "Delivery", 0, 0, 0, 0, "17/08/2026", "", "Finalizado"],
    ])

    pedidos, descartes = ler_relatorio(caminho)

    assert [p["number"] for p in pedidos] == ["1"]
    assert descartes["status:cancelado"] == 1
    assert descartes["status:negado"] == 1
    assert descartes["valor_zerado_ou_negativo"] == 1


def test_importer_maps_channels_and_does_not_duplicate(tmp_path):
    """Reimportar o mesmo relatorio nao pode dobrar o faturamento."""

    caminho = _relatorio(tmp_path, [
        [10, "iFood", "Crédito Online iFood", "", "Delivery", 0, 6, 24, 30, "16/08/2026", "Leo", "Finalizado"],
        [11, "Compartilhamento do menu", "Pix", "", "Delivery", 0, 0, 18, 18, "16/08/2026", "", "Finalizado"],
    ])
    pedidos, _ = ler_relatorio(caminho)
    store = DemoStore(tmp_path / "store.json")

    primeiro = importar(pedidos, store, sobrescrever=False)
    segundo = importar(pedidos, store, sobrescrever=False)

    assert {p["channel"] for p in pedidos} == {"iFood", "Link proprio"}
    assert primeiro["criados"] == 2
    assert segundo["criados"] == 0 and segundo["ignorados"] == 2


# ----------------------------------------------------------------------
# Precisao do que o relatorio ja traz
# ----------------------------------------------------------------------


def test_frete_maior_que_o_total_nao_cria_receita_negativa(tmp_path):
    """Subsidio do marketplace: o relatorio real traz subtotal negativo.

    Manter o frete cheio faria a receita de produto ficar negativa e envenenaria
    toda margem que a lesse. O frete e limitado ao que entrou, e a diferenca
    fica gravada em vez de apagada.
    """

    caminho = _relatorio(tmp_path, [
        [8190, "iFood", "Crédito Online iFood", "", "Delivery", 0, 6, -3.01, 2.99, "17/08/2026", "Leo", "Finalizado"],
    ])

    pedido = ler_relatorio(caminho)[0][0]

    assert pedido["total"] == 2.99
    assert pedido["delivery_fee"] == 2.99
    assert pedido["total"] - pedido["delivery_fee"] == 0
    assert pedido["reported_delivery_fee"] == 6
    assert pedido["reported_subtotal"] == -3.01
    assert pedido["revenue_conflict"] is True


def test_desconto_do_relatorio_deixa_de_ser_zerado(tmp_path):
    """E o unico registro de promocao ja aplicada que a loja tem."""

    caminho = _relatorio(tmp_path, [
        [1, "PDV", "Dinheiro", "", "Retirado", 4.5, 0, 15.5, 15.5, "17/08/2026", "", "Finalizado"],
    ])

    assert ler_relatorio(caminho)[0][0]["discount"] == 4.5


def test_link_proprio_nao_se_mistura_com_o_site(tmp_path):
    """No relatorio real o link proprio e o segundo maior canal e nao paga comissao."""

    caminho = _relatorio(tmp_path, [
        [1, "Compartilhamento do menu", "Pix", "", "Delivery", 0, 0, 20, 20, "17/08/2026", "", "Finalizado"],
        [2, "Site", "Pix", "", "Delivery", 0, 0, 20, 20, "17/08/2026", "", "Finalizado"],
        [3, "iFood", "Pix", "", "Delivery", 0, 0, 20, 20, "17/08/2026", "", "Finalizado"],
    ])

    pedidos, _ = ler_relatorio(caminho)

    assert [p["channel"] for p in pedidos] == ["Link proprio", "Site", "iFood"]


def test_importacao_cadastra_o_canal_que_o_relatorio_revelou(tmp_path):
    """Canal que existe no pedido e nao no cadastro fica com taxa zero sem ninguem decidir."""

    caminho = _relatorio(tmp_path, [
        [1, "Compartilhamento do menu", "Pix", "", "Delivery", 0, 0, 20, 20, "17/08/2026", "", "Finalizado"],
    ])
    pedidos, _ = ler_relatorio(caminho)
    store = DemoStore(tmp_path / "store.json")
    for canal in store.list("channels"):
        store.delete("channels", canal["id"])

    importar(pedidos, store, sobrescrever=False)

    canal = next(c for c in store.list("channels") if c["name"] == "Link proprio")
    assert canal["fee_percent"] == 0.0
    assert "Anota" in canal["note"]


# ----------------------------------------------------------------------
# Segundo relatorio: os itens
# ----------------------------------------------------------------------


def _relatorio_itens(tmp_path, linhas, cabecalho=None):
    workbook = Workbook()
    aba = workbook.active
    aba.title = "Itens"
    aba.append(cabecalho or ["Número do Pedido", "Produto", "Quantidade", "Valor total"])
    for linha in linhas:
        aba.append(linha)
    caminho = tmp_path / "itens.xlsx"
    workbook.save(caminho)
    return caminho


def _loja_com_pedido(tmp_path, numero=77, produto="Açaí 500 ml"):
    store = DemoStore(tmp_path / "store.json")
    for pedido in store.list("orders"):
        store.delete("orders", pedido["id"])
    caminho = _relatorio(tmp_path, [
        [numero, "PDV", "Dinheiro", "", "Retirado", 0, 0, 22, 22, "17/08/2026", "", "Finalizado"],
    ])
    pedidos, _ = ler_relatorio(caminho)
    importar(pedidos, store, sobrescrever=True)
    return store


def test_itens_dao_custo_ao_pedido_que_estava_sem(tmp_path):
    from scripts.import_anota_ai import aplicar_itens, ler_relatorio_itens

    store = _loja_com_pedido(tmp_path)
    assert store.list("orders")[0]["cost"] is None

    itens, _ = ler_relatorio_itens(_relatorio_itens(tmp_path, [[77, "Açaí 500 ml", 1, 22]]))
    resultado = aplicar_itens(itens, store)

    pedido = store.list("orders")[0]
    assert resultado["pedidos_detalhados"] == 1
    assert pedido["cost"] > 0
    assert pedido["items"][0]["product_id"] == "acai-500"
    assert pedido["items"][0]["quantity"] == 1


def test_itens_nao_mexem_no_estoque(tmp_path):
    """A baixa desses insumos aconteceu na prateleira meses atras."""

    from scripts.import_anota_ai import aplicar_itens, ler_relatorio_itens

    store = _loja_com_pedido(tmp_path)
    antes = {item["id"]: item["quantity"] for item in store.list("inventory")}

    itens, _ = ler_relatorio_itens(_relatorio_itens(tmp_path, [[77, "Açaí 500 ml", 3, 66]]))
    aplicar_itens(itens, store)

    assert {item["id"]: item["quantity"] for item in store.list("inventory")} == antes


def test_produto_sem_cadastro_deixa_o_pedido_sem_custo(tmp_path):
    """Meio custo apresentado como custo inteiro e pior do que custo nenhum."""

    from scripts.import_anota_ai import aplicar_itens, ler_relatorio_itens

    store = _loja_com_pedido(tmp_path)
    itens, _ = ler_relatorio_itens(_relatorio_itens(tmp_path, [
        [77, "Açaí 500 ml", 1, 22],
        [77, "Milkshake que a loja nao cadastrou", 1, 18],
    ]))

    resultado = aplicar_itens(itens, store)

    assert resultado["pedidos_detalhados"] == 0
    assert resultado["pedidos_incompletos"] == 1
    assert store.list("orders")[0]["cost"] is None
    assert resultado["produtos_sem_cadastro"][0][0] == "Milkshake que a loja nao cadastrou"


def test_relatorio_de_itens_aceita_outros_rotulos_de_coluna(tmp_path):
    from scripts.import_anota_ai import ler_relatorio_itens

    caminho = _relatorio_itens(
        tmp_path, [[77, "Açaí 500 ml", 2, 44]],
        cabecalho=["Pedido", "Item", "Qtd", "Total"],
    )

    itens, _ = ler_relatorio_itens(caminho)

    assert itens == [{"number": "77", "name": "Açaí 500 ml", "quantity": 2.0, "line_total": 44.0}]


def test_relatorio_agregado_explica_o_que_falta(tmp_path):
    """Sem o numero do pedido nao da para calcular custo pedido a pedido."""

    from scripts.import_anota_ai import ler_relatorio_itens

    caminho = _relatorio_itens(
        tmp_path, [["Açaí 500 ml", 120, 2640]],
        cabecalho=["Produto", "Quantidade", "Valor total"],
    )

    with pytest.raises(ValueError, match="número do pedido"):
        ler_relatorio_itens(caminho)
