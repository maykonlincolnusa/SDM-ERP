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

    assert {p["channel"] for p in pedidos} == {"iFood", "Delivery"}
    assert primeiro["criados"] == 2
    assert segundo["criados"] == 0 and segundo["ignorados"] == 2
