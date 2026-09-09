from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel as PydanticBaseModel, ConfigDict, Field, field_validator, model_validator


class BaseModel(PydanticBaseModel):
    # NaN/Infinity cannot represent stock or money and break JSON responses.
    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)

    @field_validator("due_date", "date", "observed_on", check_fields=False)
    @classmethod
    def valid_calendar_date(cls, value):
        if value is None:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except (ValueError, TypeError) as error:
            raise ValueError("Informe uma data válida no formato AAAA-MM-DD.") from error

    @field_validator("starts_on", "repeat_until", check_fields=False)
    @classmethod
    def valid_calendar_month(cls, value):
        if value is None:
            return None
        try:
            date.fromisoformat(f"{value}-01")
        except (ValueError, TypeError) as error:
            raise ValueError("Informe um mês válido no formato AAAA-MM.") from error
        return value

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
    # Como a equipe conta este insumo no dia a dia: "barra", "pote", "fardo".
    # O estoque continua em quilo, porque e nele que custo e ficha tecnica
    # falam; mas ninguem no balcao pensa em "8,4 kg de acai" — pensa em "3
    # barras". Sem essa conversao, ou o lancamento vira conta de cabeca ou vira
    # balanca em toda retirada, e nos dois casos ele deixa de ser feito.
    pack_unit: str | None = Field(default=None, min_length=1, max_length=20)
    # Quanto vale uma dessas, na unidade de estoque. Uma barra de 2,8 kg = 2.8.
    pack_size: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def pack_completo(self) -> "InventoryItemCreate":
        if (self.pack_unit is None) != (self.pack_size is None):
            raise ValueError("Informe o nome e o tamanho da unidade de manuseio, ou deixe os dois em branco.")
        return self


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
    pack_unit: str | None = Field(default=None, min_length=1, max_length=20)
    pack_size: float | None = Field(default=None, gt=0)


class InventoryAdjustment(BaseModel):
    quantity_delta: float
    reason: str = Field(min_length=2, max_length=120)


class InventoryWithdrawalLine(BaseModel):
    """Um insumo e a quantidade que saiu em uma retirada operacional."""

    inventory_id: str = Field(min_length=1, max_length=80)
    quantity: float = Field(gt=0)


class InventoryWithdrawal(BaseModel):
    """Retirada de varios insumos de uma vez.

    A retirada e diferente de uma perda: ela registra quem levou os insumos
    para a operacao e alimenta o consumo usado pelo planejamento. Cada linha
    vira um movimento, mas todas carregam o mesmo identificador de retirada
    para poderem ser desfeitas juntas.
    """

    items: list[InventoryWithdrawalLine] = Field(min_length=1, max_length=50)
    responsible: str = Field(min_length=2, max_length=80)
    reason: str = Field(default="Retirada operacional", min_length=2, max_length=120)
    note: str | None = Field(default=None, max_length=160)
    # A retirada e recusada quando falta saldo, porque o erro de digitacao mais
    # comum — 50 no lugar de 0,5 — some sem isso. Mas o saldo do sistema fica
    # atrasado com frequencia, e a venda ja nunca e bloqueada por esse motivo:
    # travar a retirada faria a saida voltar a ser invisivel justamente na loja
    # que mais precisa registra-la. Entao a recusa e o padrao e a tela oferece
    # esta confirmacao explicita, que grava e deixa o saldo negativo.
    allow_negative: bool = False


class ProductionLine(BaseModel):
    """Um insumo e a quantidade que ele entra numa fabricação, na unidade do estoque."""

    inventory_id: str = Field(min_length=1, max_length=80)
    quantity: float = Field(gt=0)


class ProductionRecipeCreate(BaseModel):
    """Ficha de fabricação: o que entra e o que sai de uma batida.

    Não confundir com a ficha técnica do produto. A ficha técnica diz o que um
    copo de 500 ml consome da base pronta; esta diz como a base pronta nasce —
    quantas barras de açaí de 8% ou 12% viram quantos quilos de base.

    É a peça que faltava para o saldo bater com a prateleira. Sem ela, a barra
    saía do freezer sem registro e a base pronta nunca aumentava, então o
    estoque só voltava ao lugar na contagem seguinte, com a diferença já
    disfarçada de perda.
    """

    name: str = Field(min_length=2, max_length=80)
    inputs: list[ProductionLine] = Field(min_length=1, max_length=20)
    # O que a batida produz. É este item que a ficha técnica dos copos consome.
    output_inventory_id: str = Field(min_length=1, max_length=80)
    output_quantity: float = Field(gt=0)
    active: bool = True
    note: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def output_fora_das_entradas(self) -> "ProductionRecipeCreate":
        if any(linha.inventory_id == self.output_inventory_id for linha in self.inputs):
            raise ValueError("O item produzido não pode estar também entre os insumos da fabricação.")
        vistos = {linha.inventory_id for linha in self.inputs}
        if len(vistos) != len(self.inputs):
            raise ValueError("O mesmo insumo aparece duas vezes na ficha de fabricação.")
        return self


class ProductionRecipeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    inputs: list[ProductionLine] | None = Field(default=None, min_length=1, max_length=20)
    output_inventory_id: str | None = Field(default=None, min_length=1, max_length=80)
    output_quantity: float | None = Field(default=None, gt=0)
    active: bool | None = None
    note: str | None = Field(default=None, max_length=160)


class ProductionRun(BaseModel):
    """Uma batida que aconteceu de verdade.

    A ficha preenche a tela, mas quem manda é o que saiu. Rendimento varia com
    gelo, com o ponto da barra e com o que fica na cuba, e é justamente essa
    diferença que faz o custo do quilo de base ser o custo real e não o
    planejado — por isso as quantidades chegam aqui de novo, editáveis, em vez
    de serem copiadas da ficha.
    """

    recipe_id: str | None = Field(default=None, max_length=80)
    inputs: list[ProductionLine] = Field(min_length=1, max_length=20)
    output_inventory_id: str = Field(min_length=1, max_length=80)
    output_quantity: float = Field(gt=0)
    responsible: str = Field(min_length=2, max_length=80)
    note: str | None = Field(default=None, max_length=160)
    # Mesma regra da retirada: faltar saldo recusa, e a tela confirma a exceção
    # quando o insumo saiu de verdade e o cadastro é que está atrasado.
    allow_negative: bool = False

    @model_validator(mode="after")
    def output_fora_das_entradas(self) -> "ProductionRun":
        if any(linha.inventory_id == self.output_inventory_id for linha in self.inputs):
            raise ValueError("O item produzido não pode estar também entre os insumos da fabricação.")
        return self


class InventoryPurchase(BaseModel):
    """Recebimento de uma compra de insumo.

    Compra não é um ajuste positivo: ela traz o custo que deve atualizar a
    média do estoque e os dados que permitem conferir a nota depois.
    """

    # `quantity` e `unit_cost` sao a quantidade e o custo na unidade de
    # estoque. Permanecem aceitos para a API ja existente; a tela de compra
    # usa os campos de embalagem abaixo e estes dois valores sao calculados.
    quantity: float | None = Field(default=None, gt=0)
    unit_cost: float | None = Field(default=None, gt=0)
    purchase_quantity: float | None = Field(default=None, gt=0)
    purchase_unit: str | None = Field(default=None, min_length=1, max_length=20)
    units_per_purchase: float | None = Field(default=None, gt=0)
    purchase_unit_cost: float | None = Field(default=None, gt=0)
    supplier: str | None = Field(default=None, max_length=80)
    invoice_number: str | None = Field(default=None, max_length=60)
    received_at: datetime | None = None
    note: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def normalize_purchase_unit(self) -> "InventoryPurchase":
        """Converte caixa, pacote ou fardo para a unidade do estoque.

        Exemplo: duas caixas, cada uma com 5 kg, a R$ 120 por caixa, entram
        como 10 kg a R$ 24/kg. A conversao fica no servidor para que o saldo
        e o custo nunca dependam somente da conta feita no navegador.
        """

        package_fields = (
            self.purchase_quantity,
            self.purchase_unit,
            self.units_per_purchase,
            self.purchase_unit_cost,
        )
        if any(value is not None for value in package_fields):
            if any(value is None for value in package_fields):
                raise ValueError("Informe quantidade, unidade, conteúdo e custo da unidade comprada.")
            self.quantity = self.purchase_quantity * self.units_per_purchase  # type: ignore[operator]
            self.unit_cost = self.purchase_unit_cost / self.units_per_purchase  # type: ignore[operator]
        if self.quantity is None or self.unit_cost is None:
            raise ValueError("Informe a quantidade e o custo da compra.")
        return self


class InventoryCount(BaseModel):
    """Contagem fisica: informa o saldo encontrado, nao a variacao.

    E como a loja conta de verdade — 3,2 kg de granola na prateleira. O servico
    calcula a diferenca e grava o movimento correspondente.
    """

    quantity: float = Field(ge=0)
    reason: str = Field(default="Contagem física", min_length=2, max_length=120)


class InventoryCountLine(BaseModel):
    """Uma linha da contagem geral: o que existe de verdade, ou o que sai do cadastro."""

    inventory_id: str = Field(min_length=1, max_length=80)
    # Vazio significa "não contei este". A linha é ignorada em vez de virar
    # zero — insumo não contado não é insumo que acabou.
    quantity: float | None = Field(default=None, ge=0)
    remove: bool = False


class InventoryBulkCount(BaseModel):
    """Contagem geral: alinha o cadastro inteiro com a prateleira de uma vez.

    Contar item a item pela tela de ajuste é a razão de a contagem nunca
    acontecer: são dez idas ao mesmo formulário, e no meio do caminho alguém
    desiste. Aqui a loja percorre o estoque uma vez, anota o que achou e
    confirma tudo junto — inclusive o que não deveria estar no cadastro.

    Tudo numa transação só: ou o estoque inteiro fica alinhado, ou nada muda.
    Meia contagem aplicada é pior que nenhuma, porque ninguém sabe onde parou.
    """

    items: list[InventoryCountLine] = Field(min_length=1, max_length=200)
    reason: str = Field(default="Contagem geral", min_length=2, max_length=120)
    responsible: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def alguma_coisa_para_fazer(self) -> "InventoryBulkCount":
        if not any(linha.remove or linha.quantity is not None for linha in self.items):
            raise ValueError("Informe ao menos uma contagem ou uma exclusão.")
        repetidos = [linha.inventory_id for linha in self.items]
        if len(set(repetidos)) != len(repetidos):
            raise ValueError("O mesmo insumo aparece duas vezes na contagem.")
        return self


class MovementUpdate(BaseModel):
    """Corrige um lancamento de consumo ja gravado."""

    quantity_delta: float | None = None
    reason: str | None = Field(default=None, min_length=2, max_length=120)


class AddonCreate(BaseModel):
    """Complemento escolhido na hora do pedido: banana, calda, granola, dose extra.

    Ele nao cabe na ficha tecnica do produto porque o mesmo copo de 500 ml sai
    com combinacao diferente a cada venda. Sem cadastro proprio, a banana e a
    calda somem da prateleira sem nenhum lancamento, e o pedido fica registrado
    com um custo menor do que teve de verdade.
    """

    name: str = Field(min_length=2, max_length=60)
    # Insumo que a porcao consome. E ele que fecha o cruzamento com o estoque.
    inventory_id: str = Field(min_length=1, max_length=80)
    # Quanto sai do estoque por porcao, na unidade do insumo (0,03 kg de banana).
    quantity: float = Field(gt=0)
    # Quanto e cobrado do cliente por porcao. Zero e cortesia que ainda custa.
    price: float = Field(default=0, ge=0)
    category: str = Field(default="Complemento", max_length=40)
    active: bool = True


class AddonUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    inventory_id: str | None = Field(default=None, min_length=1, max_length=80)
    quantity: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=40)
    active: bool | None = None


class OrderAddon(BaseModel):
    """Um complemento marcado na linha do pedido.

    `quantity` conta porcoes por unidade do produto: dois copos com calda dupla
    sao dois na linha e duas porcoes aqui, o que baixa quatro doses do estoque.
    """

    addon_id: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, gt=0, le=20)


class OrderLine(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, le=50)
    addons: list[OrderAddon] = Field(default_factory=list, max_length=12)


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
    # Para onde vai. Preenchidos no balcao, no momento em que o cliente diz o
    # endereco — que e a unica hora em que alguem sabe. Sem eles o entregador
    # recebia o pedido sem destino: escolhia a zona de cabeca e procurava a rua
    # no grupo do WhatsApp, o que e onde o tempo da entrega se perde.
    delivery_zone_id: str | None = Field(default=None, max_length=80)
    delivery_address: str | None = Field(default=None, max_length=160)
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
    delivery_zone_id: str | None = Field(default=None, max_length=80)
    delivery_address: str | None = Field(default=None, max_length=160)
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
    # Despesa que se repete todo mes: aluguel, energia, salario, contador. Ela
    # abre uma serie, e o sistema lanca sozinho o mes que vence. Sem isso o
    # aluguel so existiria no mes em que alguem digitou, e a DRE do trimestre
    # mostraria um aluguel onde deveriam estar tres. Fixa tambem e a unica que
    # entra no rateio do dossie de custo; variavel, nao.
    recurring: bool = False
    # Ultimo mes da repeticao, no formato AAAA-MM. Vazio significa sem fim
    # previsto — o caso do aluguel e do salario. Preenchido serve para o que
    # acaba: as doze parcelas do IPTU, o financiamento do freezer.
    repeat_until: str | None = Field(default=None, min_length=7, max_length=7)

    @model_validator(mode="after")
    def validate_repeat(self) -> "ExpenseCreate":
        if self.repeat_until is None:
            return self
        if not self.recurring:
            raise ValueError("Marque a despesa como fixa para definir até quando ela se repete.")
        try:
            fim = datetime.strptime(self.repeat_until, "%Y-%m").date()
            inicio = date.fromisoformat(str(self.due_date)[:10])
        except (TypeError, ValueError) as error:
            raise ValueError("Informe o mês final no formato AAAA-MM.") from error
        if (fim.year, fim.month) < (inicio.year, inicio.month):
            raise ValueError("O mês final da repetição não pode ser anterior ao vencimento.")
        return self


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


class FixedCostLine(BaseModel):
    """Uma linha do custo fixo da loja: o aluguel, a luz, o contador."""

    description: str = Field(min_length=2, max_length=100)
    category: str = Field(min_length=2, max_length=40)
    amount: float = Field(gt=0)
    day: int = Field(default=5, ge=1, le=31)


class FixedCostSetup(BaseModel):
    """Monta o custo fixo inteiro de uma vez.

    A loja tem umas dez contas que se repetem todo mes e nunca mudam de nome.
    Lancar uma a uma pela tela de despesa e dez idas ao mesmo formulario, e foi
    por isso que a estrutura da loja ficou fora do sistema. Aqui elas entram
    juntas, cada uma virando uma despesa que se repete sozinha daqui para a
    frente.
    """

    items: list[FixedCostLine] = Field(min_length=1, max_length=40)
    # Primeiro mes da cobranca, AAAA-MM. Colocar o mes em que a loja abriu faz o
    # historico entrar em dia de uma vez; vazio comeca no mes corrente.
    starts_on: str | None = Field(default=None, min_length=7, max_length=7)
    # Os meses ja vencidos entram como pagos. E o caso normal de quem esta
    # colocando o historico em dia: a loja pagou o aluguel dos meses passados.
    # Desmarcar deixa tudo pendente, para quem esta cadastrando conta em atraso.
    past_paid: bool = True

    @model_validator(mode="after")
    def validate_setup(self) -> "FixedCostSetup":
        # A conta repetida precisa ser barrada sempre, e nao so quando o mes
        # inicial vem preenchido: duas linhas "Aluguel" criariam duas series, e
        # o custo fixo da loja dobraria sem ninguem perceber.
        vistos: set[str] = set()
        for line in self.items:
            chave = line.description.strip().lower()
            if chave in vistos:
                raise ValueError(f"'{line.description}' aparece duas vezes na lista.")
            vistos.add(chave)
        if self.starts_on is not None:
            try:
                datetime.strptime(self.starts_on, "%Y-%m")
            except ValueError as error:
                raise ValueError("Informe o mês inicial no formato AAAA-MM.") from error
        return self


class ExpenseSeriesUpdate(BaseModel):
    """Edita a regra da despesa que se repete.

    A mudanca vale para os meses que o sistema ainda vai lancar. O que ja foi
    lancado nao se reescreve sozinho: o aluguel de julho continua sendo o que
    foi cobrado em julho, mesmo depois do reajuste.
    """

    description: str | None = Field(default=None, min_length=2, max_length=100)
    category: str | None = Field(default=None, min_length=2, max_length=40)
    amount: float | None = Field(default=None, gt=0)
    # Dia do vencimento. Mes que nao tem o dia 31 recebe o ultimo dia dele.
    day: int | None = Field(default=None, ge=1, le=31)
    repeat_until: str | None = Field(default=None, min_length=7, max_length=7)
    active: bool | None = None


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
    # E-mail da conta com que o entregador entra no aplicativo de rua. E o
    # vinculo entre a pessoa autenticada e o cadastro: sem ele o check-in nao
    # sabe de quem e o turno, e o entregador teria de se identificar numa lista
    # — que e exatamente onde alguem marca o nome do colega por engano.
    email: str | None = Field(default=None, max_length=120)
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
    email: str | None = Field(default=None, max_length=120)
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


class MarketPriceCreate(BaseModel):
    """Preço que um concorrente cobra por um produto equivalente.

    O sistema não tem, e não tem como ter, preço de mercado: não existe fonte
    dentro da loja que diga por quanto o vizinho vende. Sem este cadastro, o
    preço só pode ser julgado por custo e margem-alvo — o que responde "quanto
    preciso cobrar", nunca "quanto dá para cobrar".

    Uma linha aqui é uma observação datada, não uma verdade permanente: preço
    de concorrente muda, e uma referência de seis meses atrás vale menos que
    uma de ontem. Por isso a data entra e a tela mostra a idade dela.
    """

    competitor: str = Field(min_length=2, max_length=80)
    # Produto do próprio cardápio a que essa referência se compara. Vazio serve
    # para a referência de categoria, quando o concorrente não tem equivalente
    # exato — é menos preciso e a tela diz isso.
    product_id: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=40)
    description: str = Field(min_length=2, max_length=100)
    price: float = Field(gt=0)
    # Onde o preço foi visto. O mesmo concorrente cobra diferente no balcão e
    # no marketplace, e comparar o preço de iFood dele com o de balcão da loja
    # é comparar coisas diferentes.
    channel: str = Field(default="Balcao", min_length=2, max_length=40)
    observed_on: str | None = None
    note: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def require_target(self) -> "MarketPriceCreate":
        if not self.product_id and not self.category:
            raise ValueError("Ligue a referência a um produto do cardápio ou a uma categoria.")
        if self.observed_on:
            try:
                date.fromisoformat(str(self.observed_on)[:10])
            except ValueError as error:
                raise ValueError("Informe a data da observação no formato AAAA-MM-DD.") from error
        return self


class MarketPriceUpdate(BaseModel):
    competitor: str | None = Field(default=None, min_length=2, max_length=80)
    product_id: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, min_length=2, max_length=100)
    price: float | None = Field(default=None, gt=0)
    channel: str | None = Field(default=None, min_length=2, max_length=40)
    observed_on: str | None = None
    note: str | None = Field(default=None, max_length=160)


class SportAnchorCreate(BaseModel):
    """Uma âncora esportiva do bairro: a academia, o grupo de corrida, o time.

    O sistema não tem nenhum dado sobre treino. Deduzir de venda funciona
    quando a linha Sport já tem histórico, e hoje ela não tem nenhum — então
    sugerir "segunda é dia de academia" seria o conselho genérico que o resto
    do módulo se recusa a dar.

    Cadastrar a âncora é a saída: o gestor declara a realidade que ele conhece
    e o dado ainda não mostra. Cada sugestão diz de qual das duas fontes veio,
    para nunca se confundir uma coisa com a outra.
    """

    name: str = Field(min_length=2, max_length=80)
    kind: Literal["Academia", "Corrida", "Time", "Evento", "Outro"] = "Academia"
    # Dias em que esse público treina, 0 = segunda. É o que liga a âncora ao
    # calendário da semana; vazio significa "todo dia".
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    # Momento do treino. O relatório da Anota Aí não traz hora, então isto não
    # se deduz de venda nenhuma — só o gestor sabe.
    time_of_day: Literal["Manhã", "Tarde", "Noite"] = "Manhã"
    # Tamanho declarado do público. Serve de ordem de grandeza no impacto, e
    # aparece na tela como premissa, nunca como medição.
    people: int | None = Field(default=None, ge=1, le=100000)
    distance_note: str | None = Field(default=None, max_length=80)
    active: bool = True
    note: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_weekdays(self) -> "SportAnchorCreate":
        if any(dia < 0 or dia > 6 for dia in self.weekdays):
            raise ValueError("Os dias da semana vão de 0 (segunda) a 6 (domingo).")
        self.weekdays = sorted(set(self.weekdays))
        return self


class SportAnchorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    kind: Literal["Academia", "Corrida", "Time", "Evento", "Outro"] | None = None
    weekdays: list[int] | None = Field(default=None, max_length=7)
    time_of_day: Literal["Manhã", "Tarde", "Noite"] | None = None
    people: int | None = Field(default=None, ge=1, le=100000)
    distance_note: str | None = Field(default=None, max_length=80)
    active: bool | None = None
    note: str | None = Field(default=None, max_length=160)


class MediaUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=120)
    category: str | None = Field(default=None, min_length=2, max_length=60)
    tags: list[str] | None = Field(default=None, max_length=12)
    entity_type: str | None = Field(default=None, max_length=40)
    entity_id: str | None = Field(default=None, max_length=120)


class IFoodActivationStart(BaseModel):
    """Chave e segredo do aplicativo criado no Portal do Desenvolvedor do iFood.

    Eles chegam uma vez, para gerar o código de ativação e depois renovar o
    token sozinhos. Nenhum dos dois volta para a tela.
    """

    client_id: str = Field(min_length=8, max_length=120)
    client_secret: str = Field(min_length=8, max_length=200)


class IFoodActivationConfirm(BaseModel):
    """O código de autorização que o Portal do Parceiro devolve à loja."""

    authorization_code: str = Field(min_length=4, max_length=200)
    # Uma chave pode enxergar mais de uma loja; sem escolha explícita vale a
    # primeira que o iFood listar.
    merchant_id: str | None = Field(default=None, max_length=120)


# ----------------------------------------------------------------------
# Operacao de rua: zona, turno e entrega
#
# O acerto do entregador (DeliveryEntryCreate, acima) continua sendo por turno,
# porque e assim que o pagamento acontece no balcao. O que entra aqui e outra
# coisa: o registro que o proprio entregador faz no celular, entrega a entrega,
# enquanto o turno acontece. Ele existe para responder a pergunta que o
# fechamento por turno nunca respondeu — de onde vem o pedido — e o fechamento
# passa a ser calculado a partir dele em vez de digitado de novo.
# ----------------------------------------------------------------------


class DeliveryZoneCreate(BaseModel):
    """Um bairro, condominio ou trecho que a loja atende.

    A zona e escolhida numa lista em vez de digitada a cada entrega por um
    motivo pratico: "Centro", "centro" e "Ctr" sao a mesma rua e tres linhas
    diferentes em qualquer relatorio. Sem lista fechada, "onde mais pedem" nao
    tem resposta — tem tres respostas parciais que ninguem consegue somar.

    O ponto de referencia continua livre no lancamento da entrega, porque
    "portao azul depois da praca" e informacao util e nao cabe em cadastro.
    """

    name: str = Field(min_length=2, max_length=60)
    # Frete que a loja cobra para chegar ali. Preenche o lancamento sozinho: o
    # entregador confirma em vez de digitar, e um frete digitado errado na rua
    # e a divergencia que aparece so no acerto do fim do mes.
    default_fee: float = Field(default=0, ge=0)
    # Quantos minutos a loja considera normais ate a porta do cliente. E o que
    # transforma "demorou" em "passou do combinado", que e uma frase acionavel.
    target_minutes: int | None = Field(default=None, ge=1, le=240)
    note: str | None = Field(default=None, max_length=160)
    active: bool = True


class DeliveryZoneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=60)
    default_fee: float | None = Field(default=None, ge=0)
    target_minutes: int | None = Field(default=None, ge=1, le=240)
    note: str | None = Field(default=None, max_length=160)
    active: bool | None = None


class CourierCheckIn(BaseModel):
    """Chegada do entregador. Abre o turno e passa a contar o tempo.

    `courier_id` so e aceito quando a loja opera sem autenticacao (modo
    demonstrativo e desenvolvimento). Com autenticacao ligada, quem fala e a
    conta: aceitar o identificador do corpo deixaria um entregador abrir turno
    no nome de outro.
    """

    courier_id: str | None = Field(default=None, min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=160)


class CourierCheckOut(BaseModel):
    """Saida do entregador. Fecha o turno e monta o acerto do dia.

    O fechamento nao pede numero nenhum: entregas e frete sao a soma do que foi
    registrado durante o turno. Pedir de novo seria pedir para digitar duas
    vezes o mesmo fato, e a segunda digitacao e sempre a que diverge.
    """

    note: str | None = Field(default=None, max_length=160)


class DeliveryRunItem(BaseModel):
    """Um produto e quantas unidades saem numa entrega."""

    product_id: str = Field(min_length=1, max_length=80)
    quantity: int = Field(gt=0, le=50)


class DeliveryRunCreate(BaseModel):
    """Uma entrega saindo da loja.

    Quando ela nasce de um pedido ja registrado, `order_id` basta: produtos,
    quantidade e frete vem de la e o entregador so confirma a zona. E o caminho
    normal, e o unico que se sustenta numa noite cheia — digitar item a item na
    calcada e o que faz qualquer registro de rua ser abandonado.

    `items` existe para a entrega que nao tem pedido no sistema: a encomenda
    combinada por telefone, o pedido que o balcao ainda nao lancou.
    """

    zone_id: str = Field(min_length=1, max_length=80)
    order_id: str | None = Field(default=None, max_length=80)
    items: list[DeliveryRunItem] = Field(default_factory=list, max_length=30)
    # Vazio herda o frete do pedido, e na falta dele o frete cadastrado na zona.
    freight: float | None = Field(default=None, ge=0)
    # "Portao azul depois da praca". Nao cabe em cadastro e ajuda na proxima.
    reference: str | None = Field(default=None, max_length=120)
    customer_name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=160)
    # Quando a entrega aconteceu, para o dia esquecido nao se perder. Vazio e
    # agora. O limite de quanto da para retroagir e do servico, nao do schema:
    # ele depende do relogio, e um teto fixo aqui envelheceria em silencio.
    occurred_at: datetime | None = None
    # Quanto o entregador recebeu do cliente, **frete incluido** — o cliente
    # paga uma coisa so, e o total do pedido tambem soma os dois. So valem para
    # a entrega que nao tem pedido no sistema: dela nasce a venda que a loja vai
    # conferir. Vazio deixa a loja preencher na conferencia.
    declared_total: float | None = Field(default=None, ge=0)
    payment_method: str | None = Field(default=None, min_length=2, max_length=30)

    @model_validator(mode="after")
    def tem_o_que_entregar(self) -> "DeliveryRunCreate":
        if not self.order_id and not self.items:
            raise ValueError("Escolha o pedido da fila ou informe os produtos que saíram.")
        vistos = {linha.product_id for linha in self.items}
        if len(vistos) != len(self.items):
            raise ValueError("O mesmo produto aparece duas vezes na entrega.")
        return self


class DeliveryRunFinish(BaseModel):
    """Chegada na porta do cliente.

    O minuto vem do relogio, nao do dedo: a diferenca entre a saida e este
    momento e o unico tempo de entrega que nao depende de alguem lembrar.
    `failed` cobre o cliente que nao atendeu — entrega que voltou tambem
    consumiu a rua, e esconde-la faria a media de tempo mentir para baixo.
    """

    failed: bool = False
    note: str | None = Field(default=None, max_length=160)


class DeliveryRunUpdate(BaseModel):
    """Correcao da entrega ja registrada, pela loja.

    O entregador registra; quem corrige e o balcao. Zona trocada na pressa e o
    erro mais comum, e ele contamina exatamente o relatorio que a zona existe
    para alimentar.
    """

    zone_id: str | None = Field(default=None, min_length=1, max_length=80)
    freight: float | None = Field(default=None, ge=0)
    reference: str | None = Field(default=None, max_length=120)
    customer_name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=160)


class PendingOrderConfirm(BaseModel):
    """A loja confere a venda que o entregador trouxe da rua.

    O entregador declara o que levou e quanto recebeu; quem transforma isso em
    faturamento e baixa de estoque e a loja, aqui. A conferencia existe porque
    quem recebe o dinheiro nao deveria ser tambem quem lanca o proprio valor
    sem revisao — e porque a ficha tecnica so pode baixar contra um pedido que
    alguem confirmou que aconteceu.

    Tudo e opcional: confirmar sem mexer em nada aceita o que veio da rua.
    """

    # O que a loja de fato recebeu, quando difere do declarado. Vazio aceita o
    # valor que o entregador informou.
    total: float | None = Field(default=None, ge=0)
    payment_method: str | None = Field(default=None, min_length=2, max_length=30)
    channel: str | None = Field(default=None, min_length=2, max_length=40)
    note: str | None = Field(default=None, max_length=160)


class PendingOrderReject(BaseModel):
    """Recusa a venda trazida da rua, com o motivo.

    Recusar sem motivo transformaria a conferencia num botao de apagar, e a
    diferenca entre "nao aconteceu" e "ja estava lancado" e exatamente o que
    alguem vai querer saber daqui a um mes.
    """

    reason: str = Field(min_length=2, max_length=160)
