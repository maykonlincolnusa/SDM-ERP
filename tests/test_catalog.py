"""Listas editaveis.

O perigo de tornar uma lista de opcoes editavel nao e a tela: e o registro que
ja usa o valor. Renomear sem migrar deixa a base inteira apontando para algo
que a lista nao oferece mais, e excluir deixa registro orfao. Os testes aqui
cobrem exatamente essas duas beiradas.
"""

import pytest

from app.repositories.store import DemoStore
from app.schemas import ExpenseCreate, ProductUpdate
from app.services import CatalogService, ERPService


@pytest.fixture
def loja(tmp_path):
    store = DemoStore(tmp_path / "store.json")
    return ERPService(store), CatalogService(store)


def _lista(catalogo, identificador):
    return [item["value"] for item in catalogo.get(identificador)["items"]]


# ----------------------------------------------------------------------


def test_catalogs_start_from_what_the_screens_already_offered(loja):
    _, catalogo = loja

    assert "Pix" in _lista(catalogo, "payment_methods")
    assert "Sport" in _lista(catalogo, "product_lines")
    assert {c["id"] for c in catalogo.all()} >= {"payment_methods", "expense_categories", "product_lines"}


def test_a_new_option_can_be_added(loja):
    _, catalogo = loja

    catalogo.add("payment_methods", "Vale-refeição")

    assert "Vale-refeição" in _lista(catalogo, "payment_methods")


def test_duplicates_and_blanks_are_refused(loja):
    _, catalogo = loja

    with pytest.raises(ValueError, match="já está na lista"):
        catalogo.add("payment_methods", "Pix")
    with pytest.raises(ValueError, match="Informe um nome"):
        catalogo.add("payment_methods", "   ")


def test_renaming_carries_the_records_along(loja):
    """Renomear sem migrar deixaria as despesas apontando para um valor morto.

    E a inconsistencia mais silenciosa que existe: so aparece meses depois, num
    relatorio que nao fecha.
    """

    erp, catalogo = loja
    despesa = erp.create_expense(ExpenseCreate(
        description="Aluguel", category="Fixo", amount=1800, due_date="2026-09-05",
    ))

    resultado = catalogo.rename("expense_categories", "Fixo", "Custo fixo")

    assert "Custo fixo" in _lista(catalogo, "expense_categories")
    assert "Fixo" not in _lista(catalogo, "expense_categories")
    assert resultado["migrated"] >= 1
    assert erp.store.get("expenses", despesa["id"])["category"] == "Custo fixo"


def test_renaming_a_product_line_moves_the_products(loja):
    erp, catalogo = loja
    erp.update_product("super-bowl", ProductUpdate(line="Sport"))

    catalogo.rename("product_lines", "Sport", "Fitness")

    assert erp.store.get("products", "super-bowl")["line"] == "Fitness"


def test_an_option_in_use_cannot_be_deleted(loja):
    """Excluir deixaria o registro valido e orfao — sem a opcao na lista.

    A recusa traz a contagem e aponta o caminho que quase sempre era o desejado.
    """

    erp, catalogo = loja
    erp.create_expense(ExpenseCreate(description="Energia", category="Fixo", amount=300, due_date="2026-09-05"))

    with pytest.raises(ValueError, match="em uso por"):
        catalogo.remove("expense_categories", "Fixo")


def test_an_unused_option_is_deleted(loja):
    _, catalogo = loja

    catalogo.remove("expense_categories", "Marketing")

    assert "Marketing" not in _lista(catalogo, "expense_categories")


def test_the_list_never_goes_empty(loja):
    _, catalogo = loja
    for item in _lista(catalogo, "courier_vehicles")[:-1]:
        catalogo.remove("courier_vehicles", item)

    with pytest.raises(ValueError, match="pelo menos uma"):
        catalogo.remove("courier_vehicles", _lista(catalogo, "courier_vehicles")[0])


def test_a_value_left_over_from_an_import_is_shown_as_orphan(loja):
    """Valor que existe no registro mas saiu da lista nao pode sumir da tela.

    E ele que explica por que um relatorio antigo mostra uma categoria que
    ninguem consegue mais escolher.
    """

    erp, catalogo = loja
    erp.create_expense(ExpenseCreate(
        description="Importada", category="Categoria antiga", amount=50, due_date="2026-09-05",
    ))

    orfaos = {item["value"]: item["usage"] for item in catalogo.get("expense_categories")["orphans"]}

    assert orfaos.get("Categoria antiga") == 1


def test_usage_count_comes_with_the_list(loja):
    erp, catalogo = loja
    erp.create_expense(ExpenseCreate(description="Polpa", category="Insumos", amount=10, due_date="2026-09-05"))
    erp.create_expense(ExpenseCreate(description="Granola", category="Insumos", amount=20, due_date="2026-09-05"))

    insumos = next(item for item in catalogo.get("expense_categories")["items"] if item["value"] == "Insumos")

    assert insumos["usage"] >= 2


def test_reordering_cannot_invent_or_lose_items(loja):
    _, catalogo = loja
    atual = _lista(catalogo, "payment_methods")

    catalogo.reorder("payment_methods", list(reversed(atual)))
    assert _lista(catalogo, "payment_methods") == list(reversed(atual))

    with pytest.raises(ValueError, match="mesmos itens"):
        catalogo.reorder("payment_methods", ["Pix"])


def test_an_unknown_catalog_is_refused(loja):
    _, catalogo = loja

    with pytest.raises(ValueError, match="não encontrada"):
        catalogo.get("inventado")


def test_every_change_leaves_a_trail(loja):
    erp, catalogo = loja

    catalogo.add("payment_methods", "Boleto")
    catalogo.rename("payment_methods", "Boleto", "Boleto bancário")

    trilha = erp.audit("catalog", "payment_methods")
    assert [row["action"] for row in trilha] == ["editado", "criado"]
