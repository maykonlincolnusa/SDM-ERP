"""A trava de sanidade da ficha tecnica.

Encontrada nos dados reais da loja: a ficha do copo de 330 ml somava R$ 1.828 de
custo para um produto vendido a R$ 7,87. A causa nao era calculo — era unidade.
As quantidades foram cadastradas como a cozinha pensa (16 g de amendoim, 30 ml
de calda) enquanto o insumo esta cadastrado em quilo e litro. Mil vezes acima,
em quinze linhas de nove fichas.

Custo de ficha alimenta margem, preco sugerido, promocao e o custo dos produtos
vendidos. Um erro desses sai por todos esses lugares com cara de numero
calculado, e nao ha como encontra-lo olhando.
"""

import pytest

from app.repositories.store import DemoStore
from app.services import CostingService


@pytest.fixture
def costing(tmp_path) -> CostingService:
    return CostingService(DemoStore(tmp_path / "store.json"))


def _produto(costing: CostingService, preco: float, receita: list[tuple[str, float]]) -> None:
    costing.store.put("products", "teste", {
        "id": "teste", "name": "Copo de teste", "price": preco, "active": True,
        "category": "Acai", "line": "Classico",
        "recipe": [{"inventory_id": item, "quantity": quantidade} for item, quantidade in receita],
    })


def test_o_cardapio_correto_nao_gera_suspeita(costing):
    """Produto caro, ou vendido no custo, não pode cair na trava."""

    assert costing.recipe_audit()["suspects"] == []


def test_grama_cadastrada_onde_o_estoque_esta_em_quilo_e_apontada(costing):
    # 200 g de açaí lançados como 200 kg: o erro real encontrado na loja.
    _produto(costing, preco=15.0, receita=[("acai", 200.0)])

    suspeitos = costing.recipe_audit()["suspects"]

    assert len(suspeitos) == 1
    assert suspeitos[0]["name"] == "Copo de teste"
    assert suspeitos[0]["ratio"] > 100
    linha = suspeitos[0]["lines"][0]
    # A conferência prova que a linha é impossível, mas não diz qual dos dois
    # números errou: sugerir "divida por mil" mandaria trocar a quantidade em
    # fichas onde o errado é o custo — e numa ficha real da loja as duas
    # situações convivem.
    assert "mais que o produto inteiro" in linha["diagnosis"]
    assert "0.2 kg" in linha["diagnosis"]
    assert "ou o custo é que deveria ser por grama" in linha["diagnosis"]


def test_a_linha_que_mais_pesa_vem_primeiro(costing):
    """É quase sempre a que tem o erro, e é a correção mais útil."""

    _produto(costing, preco=15.0, receita=[("granola", 5.0), ("acai", 300.0)])

    linhas = costing.recipe_audit()["suspects"][0]["lines"]

    assert linhas[0]["name"] == "Base de açaí"
    assert linhas[0]["cost"] > linhas[1]["cost"]


def test_produto_apenas_caro_nao_e_acusado(costing):
    """Vender com margem apertada é decisão de preço, não erro de cadastro."""

    # Custo de 2,25 num produto de 3,00: pouca margem, ficha correta.
    _produto(costing, preco=3.0, receita=[("acai", 0.1)])

    assert costing.recipe_audit()["suspects"] == []


def test_produto_inativo_fica_de_fora(costing):
    _produto(costing, preco=15.0, receita=[("acai", 200.0)])
    inativo = costing.store.get("products", "teste")
    inativo["active"] = False
    costing.store.put("products", "teste", inativo)

    assert costing.recipe_audit()["suspects"] == []


def test_a_conferencia_diz_quantas_fichas_olhou(costing):
    resultado = costing.recipe_audit()

    assert resultado["checked"] >= 1
    assert resultado["threshold"] == 3.0


def test_a_base_pronta_e_a_barra_na_mesma_ficha_sao_apontadas(costing):
    """A barra vira base na batida: ter as duas tira o açaí do estoque duas vezes.

    É um erro que nenhuma correção de unidade conserta — mesmo com gramas e
    quilos certos, o açaí continua contado em dobro. Foi encontrado nas oito
    fichas de açaí da loja.
    """

    costing.store.put("inventory", "barra-12", {
        "id": "barra-12", "name": "barra 12%", "unit": "kg", "quantity": 60.0,
        "minimum_quantity": 5.0, "unit_cost": 17.98, "category": "Polpas",
    })
    _produto(costing, preco=15.0, receita=[("acai", 200.0), ("barra-12", 20.0)])

    suspeito = costing.recipe_audit()["suspects"][0]

    assert suspeito["double_counted"] == ["barra 12%", "base de açaí"] or "barra 12%" in suspeito["double_counted"]


def test_ficha_sem_dois_estagios_nao_e_apontada(costing):
    _produto(costing, preco=15.0, receita=[("acai", 200.0)])

    assert costing.recipe_audit()["suspects"][0]["double_counted"] is None
