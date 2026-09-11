"""Projetar o produto, em vez de validar o que já foi imaginado.

O simulador responde "esta receita fecha?" — a pergunta de quem já decidiu o
produto. Quem está construindo marca tem a pergunta anterior: **quanto posso
gastar de insumo para este produto existir neste canal?**

A inversão importa por um motivo prático nesta loja: as fichas técnicas estão
erradas por mil e contaminam toda sugestão de preço do sistema. Esta conta parte
do preço e da comissão — dois números que a loja conhece — e devolve o teto do
insumo. Ela continua valendo enquanto as fichas não forem corrigidas.
"""

import pytest

from app.repositories.store import DemoStore
from app.schemas import ProductDesign
from app.services import CostingService


@pytest.fixture
def custo(tmp_path) -> CostingService:
    servico = CostingService(DemoStore(tmp_path / "store.json"))
    servico.erp.channels()  # semeia os canais padrão
    return servico


def _canal(resultado, nome):
    return next(linha for linha in resultado["channels"] if linha["channel"] == nome)


def test_a_comissao_do_canal_come_o_orcamento_do_insumo(custo):
    """R$ 25 com 60% de margem deixam R$ 10 no balcão e R$ 7,30 no iFood."""

    resultado = custo.design_budget(ProductDesign(price=25.0, target_margin=60))

    assert _canal(resultado, "Balcao")["ingredient_budget"] == 10.0
    ifood = _canal(resultado, "iFood")
    assert ifood["fee"] == 6.75
    assert ifood["ingredient_budget"] == 7.3


def test_diz_quanto_cobrar_no_canal_caro_para_igualar_o_orcamento(custo):
    """É a resposta para "quanto eu cobro no iFood" — e ela não é o mesmo preço."""

    resultado = custo.design_budget(ProductDesign(price=25.0, target_margin=60))

    ifood = _canal(resultado, "iFood")
    assert ifood["price_for_budget"] == pytest.approx(34.25, abs=0.05)
    # O canal que já tem o melhor orçamento não precisa de outro preço.
    assert _canal(resultado, "Balcao")["price_for_budget"] is None


def test_preco_que_nao_paga_a_comissao_e_marcado_como_inviavel(custo):
    """Comissão fixa é o que torna o copo barato inviável num canal."""

    custo.store.put("channels", "praca", {
        "id": "praca", "name": "Praça", "fee_percent": 10.0, "fee_fixed": 5.5, "active": True,
    })

    resultado = custo.design_budget(ProductDesign(price=5.0, target_margin=60))

    praca = _canal(resultado, "Praça")
    assert praca["ingredient_budget"] == 0.0
    assert praca["viable"] is False
    assert any("não cabe" in leitura["title"] for leitura in resultado["readings"])


def test_a_conta_nao_depende_da_ficha_tecnica(custo):
    """É o ponto: as fichas da loja estão erradas por mil e esta conta sobrevive."""

    produto = next(p for p in custo.store.list("products"))
    produto["recipe"] = [{"inventory_id": "acai", "quantity": 999.0}]
    custo.store.put("products", produto["id"], produto)

    resultado = custo.design_budget(ProductDesign(price=25.0, target_margin=60))

    assert _canal(resultado, "Balcao")["ingredient_budget"] == 10.0


def test_insumo_parado_vem_primeiro_na_lista_do_que_cabe(custo):
    """Capital já gasto esperando virar produto é o melhor ponto de partida."""

    custo.store.put("inventory", "pitaya", {
        "id": "pitaya", "name": "Pitaya polpa", "unit": "kg", "quantity": 2.0,
        "minimum_quantity": 1.0, "unit_cost": 22.98, "category": "Frutas",
    })

    resultado = custo.design_budget(ProductDesign(price=60.0, target_margin=50))

    parados = [linha for linha in resultado["affordable"] if linha["idle"]]
    assert any(linha["name"] == "Pitaya polpa" for linha in parados)
    # E ele aparece antes do que já está em uso.
    assert resultado["affordable"][0]["idle"] is True


def test_embalagem_fica_de_fora_da_criacao(custo):
    """Ninguém lança um copo novo por causa da tampa."""

    nomes = [linha["name"] for linha in custo.design_budget(
        ProductDesign(price=60.0, target_margin=50)).get("affordable", [])]

    assert not any("copo" in nome.lower() or "tampa" in nome.lower() for nome in nomes)


def test_o_custo_fixo_entra_quando_a_loja_pede(custo):
    """Fora dele o número é margem de contribuição; dentro, é lucro de verdade."""

    sem = custo.design_budget(ProductDesign(price=25.0, target_margin=60, include_fixed=False))
    com = custo.design_budget(ProductDesign(price=25.0, target_margin=60, include_fixed=True))

    if com["fixed_per_unit"]:
        assert _canal(com, "Balcao")["ingredient_budget"] < _canal(sem, "Balcao")["ingredient_budget"]
