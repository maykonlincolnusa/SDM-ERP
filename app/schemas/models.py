from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# A loja pode cadastrar os proprios canais, entao o pedido nao valida mais o
# canal contra uma lista fixa: quem valida e o cadastro de canais no servico.
PAYMENT_METHODS = ("Pix", "Credito", "Debito", "Dinheiro", "Voucher", "Outro")

# Linha de marca. "Sport" existe para a loja medir separadamente o que vende
# como produto de treino e o que vende como sobremesa: sao publicos, precos e
# margens diferentes, e sem separar isso o resultado da linha nova se dissolve
# na media do cardapio inteiro.
PRODUCT_LINES = ("Classico", "Sport", "Kids", "Sazonal")


class RecipeLine(BaseModel):
    inventory_id: str
    quantity: float = Field(gt=0)


class Nutrition(BaseModel):
    """Numeros que sustentam a comunicacao da linha Sport.

    Ficam no cadastro do produto porque promessa de rotulo e informacao de
    produto, nao de campanha: se a ficha mudar, a comunicacao acompanha.
    """

    kcal: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)
    fiber_g: float | None = Field(default=None, ge=0)


class ProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    category: str = Field(min_length=2, max_length=40)
    price: float = Field(gt=0)
    recipe: list[RecipeLine] = Field(default_factory=list)
    active: bool = True
    line: str = Field(default="Classico", max_length=30)
    claims: list[str] = Field(default_factory=list, max_length=8)
    nutrition: Nutrition = Field(default_factory=Nutrition)


class ProductUpdate(BaseModel):
    """Tudo opcional: a tela envia apenas o que mudou."""

    name: str | None = Field(default=None, min_length=2, max_length=80)
    category: str | None = Field(default=None, min_length=2, max_length=40)
    price: float | None = Field(default=None, gt=0)
    recipe: list[RecipeLine] | None = None
    active: bool | None = None
    line: str | None = Field(default=None, max_length=30)
    claims: list[str] | None = Field(default=None, max_length=8)
    nutrition: Nutrition | None = None


class Product(ProductCreate):
    id: str
    image: str | None = None


class InventoryItemCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    unit: str = Field(min_length=1, max_length=12)
    quantity: float = Field(ge=0)
    minimum_quantity: float = Field(ge=0)
    unit_cost: float = Field(ge=0)
    category: str = Field(default="Insumos", max_length=40)
    # Quantos dias o insumo dura depois de preparado ou aberto. Vazio significa
    # "nao perece" — e o que separa o item que entra no plano de producao do
    # dia daquele que so precisa de ponto de pedido.
    shelf_life_days: int | None = Field(default=None, ge=1, le=365)
    # Prazo do fornecedor. E sobre ele que o ponto de pedido e calculado: pedir
    # quando o saldo cobre exatamente o que se consome ate a entrega chegar.
    lead_time_days: int = Field(default=2, ge=1, le=60)


class InventoryItem(InventoryItemCreate):
    id: str
    updated_at: datetime | None = None


class InventoryItemUpdate(BaseModel):
    """Edita o cadastro do insumo.

    `quantity` fica de fora de proposito: saldo so muda por ajuste ou contagem,
    que gravam um movimento. Deixar a tela sobrescrever o saldo direto criaria
    diferenca de estoque sem nenhum lancamento explicando de onde veio.
    """

    name: str | None = Field(default=None, min_length=2, max_length=80)
    unit: str | None = Field(default=None, min_length=1, max_length=12)
    minimum_quantity: float | None = Field(default=None, ge=0)
    unit_cost: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=40)
    shelf_life_days: int | None = Field(default=None, ge=1, le=365)
    lead_time_days: int | None = Field(default=None, ge=1, le=60)


class InventoryAdjustment(BaseModel):
    quantity_delta: float
    reason: str = Field(min_length=2, max_length=120)


class InventoryCount(BaseModel):
    """Contagem fisica: informa o saldo encontrado, nao a variacao.

    E como a loja conta de verdade — 3,2 kg de granola na prateleira. O servico
    calcula a diferenca e grava o movimento correspondente.
    """

    quantity: float = Field(ge=0)
    reason: str = Field(default="Contagem física", min_length=2, max_length=120)


class MovementUpdate(BaseModel):
    """Corrige um lancamento de consumo ja gravado."""

    quantity_delta: float | None = None
    reason: str | None = Field(default=None, min_length=2, max_length=120)


class OrderLine(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, le=50)


class OrderCreate(BaseModel):
    items: list[OrderLine] = Field(min_length=1)
    channel: str = Field(default="Balcao", min_length=2, max_length=40)
    payment_method: str = Field(default="Pix", min_length=2, max_length=30)
    customer_name: str | None = Field(default=None, max_length=80)
    discount: float = Field(default=0, ge=0)
    # A maioria dos pedidos e balcao. A entrega e marcada de forma explicita,
    # e nao deduzida do frete: entrega com frete gratis existe, e um frete de
    # zero nao poderia distingui-la de uma venda no balcao.
    is_delivery: bool = False
    # Frete cobrado do cliente. Entra no total porque e dinheiro que a loja
    # recebe, mas fica separado do faturamento de produto: ele existe para
    # pagar a entrega, e somar os dois esconde se a entrega se paga.
    delivery_fee: float = Field(default=0, ge=0)
    courier_id: str | None = Field(default=None, max_length=80)
    # Cadastro do pedido de ontem. Sem isso a loja so consegue lancar o que
    # esta vendendo neste instante, e o movimento de ontem entra com a data de
    # hoje — o que desloca o historico que a previsao aprende.
    occurred_at: datetime | None = None


class OrderUpdate(BaseModel):
    items: list[OrderLine] | None = Field(default=None, min_length=1)
    channel: str | None = Field(default=None, min_length=2, max_length=40)
    payment_method: str | None = Field(default=None, min_length=2, max_length=30)
    customer_name: str | None = Field(default=None, max_length=80)
    discount: float | None = Field(default=None, ge=0)
    is_delivery: bool | None = None
    delivery_fee: float | None = Field(default=None, ge=0)
    courier_id: str | None = Field(default=None, max_length=80)
    occurred_at: datetime | None = None
    # Pedido importado da Anota Ai nao tem itens; o faturamento e o unico fato
    # conhecido. Corrigir o total desses precisa ser possivel sem inventar uma
    # ficha tecnica que a origem nunca informou.
    total: float | None = Field(default=None, ge=0)


class OrderCancel(BaseModel):
    reason: str = Field(default="Cancelado pela loja", min_length=2, max_length=120)


class ExpenseCreate(BaseModel):
    description: str = Field(min_length=2, max_length=100)
    category: str = Field(min_length=2, max_length=40)
    amount: float = Field(gt=0)
    due_date: str
    status: Literal["Paga", "Pendente"] = "Pendente"
    # Despesa fixa entra no rateio do dossie de custo; variavel, nao.
    recurring: bool = False


class ExpenseUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=2, max_length=100)
    category: str | None = Field(default=None, min_length=2, max_length=40)
    amount: float | None = Field(default=None, gt=0)
    due_date: str | None = None
    status: Literal["Paga", "Pendente"] | None = None
    recurring: bool | None = None


class Expense(ExpenseCreate):
    id: str
    created_at: datetime | None = None


class ChannelCreate(BaseModel):
    """Canal de distribuicao com o custo real de vender por ele.

    iFood nao cobra o mesmo que o balcao. Sem a taxa cadastrada todo pedido
    parece ter a mesma margem, e a loja decide preco e mix com o numero errado.
    """

    name: str = Field(min_length=2, max_length=40)
    fee_percent: float = Field(default=0, ge=0, le=100)
    fee_fixed: float = Field(default=0, ge=0)
    active: bool = True
    note: str | None = Field(default=None, max_length=160)


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=40)
    fee_percent: float | None = Field(default=None, ge=0, le=100)
    fee_fixed: float | None = Field(default=None, ge=0)
    active: bool | None = None
    note: str | None = Field(default=None, max_length=160)


# Formas de pagar entregador que aparecem numa loja de bairro. Elas se somam em
# vez de excluir uma a outra: e comum combinar diaria com valor por entrega, e
# forcar a escolha de um modelo so faria a loja lancar o resto "no bonus", que
# e onde o controle se perde.
class CourierCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=30)
    vehicle: str | None = Field(default=None, max_length=30)
    active: bool = True
    # Remuneracao. Todos os componentes podem coexistir.
    fee_per_delivery: float = Field(default=0, ge=0)
    commission_percent: float = Field(default=0, ge=0, le=100)
    daily_rate: float = Field(default=0, ge=0)
    monthly_salary: float = Field(default=0, ge=0)
    note: str | None = Field(default=None, max_length=160)


class CourierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=30)
    vehicle: str | None = Field(default=None, max_length=30)
    active: bool | None = None
    fee_per_delivery: float | None = Field(default=None, ge=0)
    commission_percent: float | None = Field(default=None, ge=0, le=100)
    daily_rate: float | None = Field(default=None, ge=0)
    monthly_salary: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=160)


class DeliveryEntryCreate(BaseModel):
    """Um turno de um entregador: quantas entregas, quanto de frete, o que recebeu.

    A loja nao registra entrega a entrega — registra o fechamento do dia. O
    lancamento e por turno de proposito: e assim que o acerto acontece no balcao,
    e pedir uma linha por entrega faria o controle ser abandonado na primeira
    semana movimentada.
    """

    courier_id: str = Field(min_length=1, max_length=80)
    date: str
    deliveries: int = Field(default=0, ge=0, le=500)
    freight_total: float = Field(default=0, ge=0)
    # Conta a diaria. Dia em que o entregador ficou de sobreaviso e nao rodou
    # pode ter diaria sem entrega, e o contrario tambem acontece.
    worked: bool = True
    bonus: float = Field(default=0, ge=0)
    deduction: float = Field(default=0, ge=0)
    # A conta automatica cobre o combinado; a excecao do dia entra aqui, com o
    # motivo obrigatorio ao lado, para o acerto continuar auditavel.
    payout_override: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def require_reason_for_override(self) -> "DeliveryEntryCreate":
        if self.payout_override is not None and not (self.note or "").strip():
            raise ValueError("Ao substituir o valor calculado, escreva o motivo no campo de observação.")
        return self


class DeliveryEntryUpdate(BaseModel):
    courier_id: str | None = Field(default=None, min_length=1, max_length=80)
    date: str | None = None
    deliveries: int | None = Field(default=None, ge=0, le=500)
    freight_total: float | None = Field(default=None, ge=0)
    worked: bool | None = None
    bonus: float | None = Field(default=None, ge=0)
    deduction: float | None = Field(default=None, ge=0)
    payout_override: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=160)


class CatalogItem(BaseModel):
    value: str = Field(min_length=1, max_length=40)


class CatalogRename(BaseModel):
    value: str = Field(min_length=1, max_length=40)
    new_value: str = Field(min_length=1, max_length=40)


class CatalogOrder(BaseModel):
    items: list[str] = Field(min_length=1, max_length=60)


class ProductSimulation(BaseModel):
    """Testa um produto novo sem gravar nada no cardapio."""

    name: str = Field(default="Produto novo", max_length=80)
    price: float = Field(gt=0)
    recipe: list[RecipeLine] = Field(default_factory=list)
    # Insumo que ainda nao existe no estoque. Produto novo quase sempre traz
    # ingrediente que a loja nunca comprou, e exigir o cadastro antes de saber
    # se o produto se paga inverte a ordem da decisao.
    extra_cost: float = Field(default=0, ge=0)
    extra_cost_label: str | None = Field(default=None, max_length=60)
    target_margin: float = Field(default=0, ge=0, lt=100)
    expected_daily_units: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_some_cost(self) -> "ProductSimulation":
        if not self.recipe and not self.extra_cost:
            raise ValueError("Informe a ficha técnica ou um custo estimado para simular.")
        return self


class MediaUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    category: str | None = Field(default=None, min_length=2, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=12)
    entity_type: str | None = Field(default=None, max_length=40)
    entity_id: str | None = Field(default=None, max_length=120)
