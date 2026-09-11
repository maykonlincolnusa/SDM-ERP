from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.observability import observe_request
from app.repositories.secrets import build_secret_vault
from app.repositories.store import Store, build_store
from app.schemas import (
    AddonCreate,
    AddonUpdate,
    AnotaAiCatalogImport,
    AnotaAiConnect,
    CatalogItem,
    CatalogOrder,
    CatalogRename,
    ChannelCreate,
    ChannelUpdate,
    CourierCreate,
    CourierUpdate,
    CourierCheckIn,
    CourierCheckOut,
    DeliveryEntryCreate,
    DeliveryEntryUpdate,
    DeliveryRunCreate,
    DeliveryRunFinish,
    DeliveryRunUpdate,
    DeliveryZoneCreate,
    DeliveryZoneUpdate,
    ExpenseCreate,
    ExpenseSeriesUpdate,
    ExpenseUpdate,
    FieldZoneCreate,
    FixedCostSetup,
    IFoodActivationConfirm,
    IFoodActivationStart,
    InventoryAdjustment,
    InventoryBulkCount,
    InventoryCount,
    InventoryItemCreate,
    InventoryItemUpdate,
    InventoryPurchase,
    InventoryWithdrawal,
    MarketPriceCreate,
    MarketPriceUpdate,
    MediaUpdate,
    MovementUpdate,
    PendingOrderConfirm,
    PendingOrderReject,
    OrderCancel,
    OrderCreate,
    OrderUpdate,
    ProductCreate,
    ProductionRecipeCreate,
    ProductionRecipeUpdate,
    ProductionRun,
    ProductSimulation,
    ProductUpdate,
    SportAnchorCreate,
    SportAnchorUpdate,
    StoreLocation,
)
from app.services.dispatch import SemEntregadorEscolhido
from app.services import (
    AnotaAiError,
    AnotaAiService,
    CatalogService,
    CostingService,
    DeliveryService,
    DispatchService,
    ERPService,
    ForecastService,
    GrowthService,
    IFoodService,
    MediaService,
    PlanningService,
    PricingService,
    PromotionService,
    YieldService,
)
from scripts.import_anota_ai import (
    aplicar_itens as aplicar_itens_anota_ai,
    importar as importar_anota_ai,
    ler_relatorio as ler_relatorio_anota_ai,
    ler_relatorio_itens as ler_relatorio_itens_anota_ai,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.store = build_store(settings)
    app.state.erp = ERPService(app.state.store)
    app.state.forecast = ForecastService(app.state.store)
    app.state.costing = CostingService(app.state.store)
    app.state.growth = GrowthService(app.state.store)
    app.state.planning = PlanningService(app.state.store)
    app.state.delivery = DeliveryService(app.state.store)
    app.state.dispatch = DispatchService(app.state.store)
    app.state.catalog = CatalogService(app.state.store)
    app.state.yields = YieldService(app.state.store)
    app.state.secrets = build_secret_vault(settings, app.state.store.mode)
    app.state.anotaai = AnotaAiService(app.state.store, app.state.erp, app.state.secrets)
    app.state.ifood = IFoodService(app.state.store, app.state.erp, app.state.secrets)
    app.state.media = MediaService(app.state.store, settings)
    app.state.media.sync_workspace()
    yield


app = FastAPI(
    title="SDM",
    description="ERP operacional para gestão de lojas de açaí.",
    version="1.3.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.middleware("http")(observe_request)


def erp(request: Request) -> ERPService:
    return request.app.state.erp


def forecast_service(request: Request) -> ForecastService:
    return request.app.state.forecast


def costing_service(request: Request) -> CostingService:
    return request.app.state.costing


def growth_service(request: Request) -> GrowthService:
    return request.app.state.growth


def planning_service(request: Request) -> PlanningService:
    return request.app.state.planning


def delivery_service(request: Request) -> DeliveryService:
    return request.app.state.delivery


def dispatch_service(request: Request) -> DispatchService:
    return request.app.state.dispatch


def catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog


def ifood_service(request: Request) -> IFoodService:
    return request.app.state.ifood


def store(request: Request) -> Store:
    return request.app.state.store


def media(request: Request) -> MediaService:
    return request.app.state.media


def current_identity(request: Request) -> dict:
    """Sessão válida, sem exigir papel nenhum.

    Quem decide o papel é a rota. Separar as duas coisas é o que permite o
    entregador ter conta própria: ele autentica como qualquer um, mas só
    alcança as rotas de rua — as da loja continuam exigindo `erp_admin`, e é
    essa exigência que mantém DRE, custo e precificação fora do celular dele.
    """

    settings: Settings = request.app.state.settings
    if not settings.auth_required:
        return {"uid": "demo-user", "erp_admin": True, "courier": True}
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação necessária.")
    try:  # pragma: no cover - only exercised with a Firebase project
        from firebase_admin import auth

        return auth.verify_id_token(header.removeprefix("Bearer "))
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão inválida.") from error


def current_user(request: Request) -> dict:
    """Optional production guard using Firebase Authentication ID tokens."""
    user = current_identity(request)
    if request.app.state.settings.auth_required and user.get("erp_admin") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sua conta ainda nao tem permissao para acessar a loja.",
        )
    return user


def current_field_user(request: Request) -> dict:
    """Guarda do aplicativo de rua: entregador ou alguém da loja.

    O gestor entra junto de propósito. Entregador que perdeu o celular no meio
    do turno precisa que alguém no balcão feche a entrega por ele, e a
    alternativa — turno preso aberto até o dia seguinte — estraga o acerto e o
    painel ao mesmo tempo.
    """

    user = current_identity(request)
    if not request.app.state.settings.auth_required:
        return user
    if user.get("courier") is not True and user.get("erp_admin") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta conta ainda não foi liberada como entregador. Peça para a loja cadastrar você.",
        )
    return user


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/entregador", include_in_schema=False)
def courier_app() -> FileResponse:
    """A tela que o entregador abre no celular dele.

    Página separada do ERP porque a conta dele não tem `erp_admin`: servir o
    mesmo pacote levaria para o aparelho um código que só sabe pedir dados que
    aquela sessão nunca vai receber.
    """

    return FileResponse(STATIC_DIR / "courier.html")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/health")
def health_check(request: Request, database: Store = Depends(store)) -> dict[str, str]:
    # O modo do cofre entra aqui porque é a única forma de conferir, depois de
    # um deploy, que a credencial de terceiro não caiu num arquivo efêmero.
    return {"status": "healthy", "storage": database.mode, "secrets": request.app.state.secrets.mode}


@app.get("/api/config")
def api_config(request: Request) -> dict[str, str | bool]:
    settings: Settings = request.app.state.settings
    return {"storage": request.app.state.store.mode, "auth_required": settings.auth_required}


@app.get("/api/firebase-client-config")
def firebase_client_config(request: Request) -> dict[str, str | bool | None]:
    """Expose only Firebase's public browser configuration for token-based login."""
    settings: Settings = request.app.state.settings
    if not settings.firebase_web_configured:
        return {"enabled": False, "auth_required": settings.auth_required}
    return {
        "enabled": True,
        "auth_required": settings.auth_required,
        "apiKey": settings.firebase_web_api_key,
        "authDomain": settings.firebase_web_auth_domain,
        "projectId": settings.firebase_project_id,
        "storageBucket": settings.firebase_storage_bucket,
        "messagingSenderId": settings.firebase_web_messaging_sender_id,
        "appId": settings.firebase_web_app_id,
        "measurementId": settings.firebase_web_measurement_id,
    }


@app.get("/api/dashboard")
def dashboard(
    days: int = Query(default=7, ge=1, le=90),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return service.dashboard(days)


@app.get("/api/sales/daily")
def sales_daily(
    day: str | None = Query(default=None, description="AAAA-MM-DD no fuso da loja. Vazio = hoje."),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.sales_day(day))


@app.get("/api/forecast")
def forecast(
    horizon: int = Query(default=7, ge=1, le=30),
    service: ForecastService = Depends(forecast_service),
    _: dict = Depends(current_user),
) -> dict:
    return service.forecast(horizon)


@app.get("/api/planning")
def planning(
    days: int = Query(default=90, ge=14, le=365),
    horizon: int = Query(default=7, ge=1, le=30),
    service_level: float = Query(default=0.9, ge=0.5, le=0.99),
    service: PlanningService = Depends(planning_service),
    _: dict = Depends(current_user),
) -> dict:
    """Quanto preparar hoje e quando pedir mais, por quantil empírico."""
    return service.plan(days, horizon, service_level)


@app.get("/api/growth")
def growth(
    days: int = Query(default=90, ge=7, le=365),
    service: GrowthService = Depends(growth_service),
    _: dict = Depends(current_user),
) -> dict:
    """Produtos, combos, serviços e ações de marketing sugeridos pelo movimento da loja."""
    return service.opportunities(days)


@app.get("/api/audit")
def audit(
    entity: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(default=60, ge=1, le=300),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> list[dict]:
    return service.audit(entity, entity_id, limit)


# ----------------------------------------------------------------------
# Cardápio
# ----------------------------------------------------------------------


@app.get("/api/products")
def products(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.products()


@app.post("/api/products", status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_product(payload))


@app.post("/api/products/simulate")
def simulate_product(
    payload: ProductSimulation,
    service: CostingService = Depends(costing_service),
    _: dict = Depends(current_user),
) -> dict:
    """Conta do produto novo antes de ele existir. Não grava nada."""
    return _run(lambda: service.simulate(payload))


@app.get("/api/products/recipe-audit")
def recipe_audit(service: CostingService = Depends(costing_service), _: dict = Depends(current_user)) -> dict:
    """Fichas cujo custo não pode estar certo, com a correção provável ao lado.

    Custo de ficha alimenta margem, preço sugerido, promoção e o custo dos
    produtos vendidos. Um erro de unidade ali sai por toda parte, com cara de
    número calculado — e é por isso que ele precisa ser apontado, e não apenas
    exibido no meio de outros valores.
    """
    return service.recipe_audit()


@app.get("/api/products/lines")
def product_lines(
    days: int = Query(default=30, ge=1, le=365),
    service: CostingService = Depends(costing_service),
    _: dict = Depends(current_user),
) -> dict:
    return service.line_performance(days)


@app.get("/api/products/{product_id}/dossier")
def product_dossier(
    product_id: str,
    days: int = Query(default=30, ge=1, le=365),
    target_margin: float = Query(default=0, ge=0, lt=100),
    service: CostingService = Depends(costing_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.dossier(product_id, days, target_margin))


@app.patch("/api/products/{product_id}")
def update_product(
    product_id: str,
    payload: ProductUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_product(product_id, payload))


@app.delete("/api/products/{product_id}")
def delete_product(
    product_id: str,
    permanent: bool = Query(default=False, description="Remove do cadastro mesmo com vendas no histórico."),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.delete_product(product_id, permanent))


# ----------------------------------------------------------------------
# Complementos
# ----------------------------------------------------------------------


@app.get("/api/addons")
def addons(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    """Banana, calda, granola: o que entra no copo por escolha do cliente."""
    return service.addons()


@app.post("/api/addons", status_code=status.HTTP_201_CREATED)
def create_addon(payload: AddonCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_addon(payload))


@app.patch("/api/addons/{addon_id}")
def update_addon(
    addon_id: str,
    payload: AddonUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_addon(addon_id, payload))


@app.delete("/api/addons/{addon_id}")
def delete_addon(addon_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_addon(addon_id))


# ----------------------------------------------------------------------
# Estoque
# ----------------------------------------------------------------------


@app.get("/api/inventory")
def inventory(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.inventory()


@app.get("/api/inventory/consumption")
def inventory_consumption(
    days: int = Query(default=7, ge=1, le=90),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return service.consumption(days)


@app.get("/api/inventory/crossing")
def inventory_crossing(
    days: int = Query(default=30, ge=1, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Cruzamento das vendas com o estoque, separando receita de complemento."""
    return service.ingredient_crossing(days)


@app.post("/api/inventory", status_code=status.HTTP_201_CREATED)
def create_inventory_item(payload: InventoryItemCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_inventory_item(payload))


@app.patch("/api/inventory/movements/{movement_id}")
def update_movement(
    movement_id: str,
    payload: MovementUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_movement(movement_id, payload))


@app.delete("/api/inventory/movements/{movement_id}")
def delete_movement(movement_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_movement(movement_id))


@app.post("/api/inventory/{inventory_id}/adjust")
def adjust_inventory(inventory_id: str, payload: InventoryAdjustment, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.adjust_inventory(inventory_id, payload))


@app.post("/api/inventory/withdrawals", status_code=status.HTTP_201_CREATED)
def register_inventory_withdrawal(
    payload: InventoryWithdrawal,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Baixa varios insumos, identificando a retirada e quem a fez."""
    return _run(lambda: service.register_inventory_withdrawal(payload))


@app.delete("/api/inventory/withdrawals/{withdrawal_id}")
def undo_inventory_withdrawal(
    withdrawal_id: str,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Desfaz todas as linhas de uma retirada de uma vez."""
    return _run(lambda: service.undo_inventory_withdrawal(withdrawal_id))


@app.post("/api/inventory/bulk-count")
def bulk_count(
    payload: InventoryBulkCount,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Contagem geral: alinha o cadastro inteiro com a prateleira, tudo ou nada."""
    return _run(lambda: service.bulk_count(payload))


@app.get("/api/production/recipes")
def production_recipes(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.production_recipes()


@app.post("/api/production/recipes", status_code=status.HTTP_201_CREATED)
def create_production_recipe(
    payload: ProductionRecipeCreate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Ficha de fabricação: quantas barras viram quantos quilos de base."""
    return _run(lambda: service.create_production_recipe(payload))


@app.patch("/api/production/recipes/{recipe_id}")
def update_production_recipe(
    recipe_id: str,
    payload: ProductionRecipeUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_production_recipe(recipe_id, payload))


@app.delete("/api/production/recipes/{recipe_id}")
def delete_production_recipe(recipe_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_production_recipe(recipe_id))


@app.get("/api/production/acai-yield")
def acai_yield(
    days: int = Query(default=90, ge=7, le=365),
    kg: float = Query(default=20.0, ge=0, le=1000),
    request: Request = None,
    _: dict = Depends(current_user),
) -> dict:
    """Quanto uma barra rende, deduzido do que a loja já registra.

    O rendimento não precisa ser medido: os dois lados da conta já passam pelo
    sistema por outros motivos. O quilo que entrou está na compra; o mililitro
    que saiu está na venda. A razão entre eles já traz dentro o que a batida
    declarada esconde — o que ficou na cuba, o que derreteu, a mão pesada.
    """
    return request.app.state.yields.acai_yield(days, kg)


@app.get("/api/production/yield")
def production_yield(
    days: int = Query(default=90, ge=7, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Quanto de base sai de cada quilo de barra, medido em vez de suposto."""
    return service.production_yield(days)


@app.get("/api/production/economics")
def production_economics(
    days: int = Query(default=30, ge=7, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Custo real do acai por copo, medido por barra consumida e copo vendido."""
    return service.production_economics(days)


@app.get("/api/packaging/audit")
def packaging_audit(
    days: int = Query(default=30, ge=7, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """O copo confere a venda: um copo e uma venda, e ele se conta em manga."""
    return service.packaging_audit(days)


@app.get("/api/production/input-advice")
def production_input_advice(
    days: int = Query(default=30, ge=7, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Quanto de cada insumo vai na batida, e o que ficou nos dois lugares."""
    return service.production_input_advice(days)


@app.post("/api/production/runs", status_code=status.HTTP_201_CREATED)
def register_production(
    payload: ProductionRun,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Registra a batida: consome as barras e dá entrada na base pronta."""
    return _run(lambda: service.register_production(payload))


@app.delete("/api/production/runs/{production_id}")
def undo_production(production_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    """Desfaz a batida inteira, devolvendo a barra e retirando a base."""
    return _run(lambda: service.undo_production(production_id))


@app.post("/api/inventory/{inventory_id}/purchases", status_code=status.HTTP_201_CREATED)
def register_inventory_purchase(
    inventory_id: str,
    payload: InventoryPurchase,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Registra uma compra recebida e atualiza saldo e custo médio."""
    return _run(lambda: service.register_inventory_purchase(inventory_id, payload))


@app.post("/api/inventory/{inventory_id}/count")
def count_inventory(inventory_id: str, payload: InventoryCount, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    """Contagem física: informa o saldo encontrado, o sistema lança a diferença."""
    return _run(lambda: service.count_inventory(inventory_id, payload))


@app.patch("/api/inventory/{inventory_id}")
def update_inventory_item(
    inventory_id: str,
    payload: InventoryItemUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_inventory_item(inventory_id, payload))


@app.delete("/api/inventory/{inventory_id}")
def delete_inventory_item(inventory_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_inventory_item(inventory_id))


# ----------------------------------------------------------------------
# Pedidos
# ----------------------------------------------------------------------


@app.get("/api/orders")
def orders(
    days: int | None = Query(default=None, ge=1, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> list[dict]:
    return service.orders(days)


@app.post("/api/orders", status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_order(payload))


@app.patch("/api/orders/{order_id}")
def update_order(
    order_id: str,
    payload: OrderUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_order(order_id, payload))


@app.post("/api/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    payload: OrderCancel,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.cancel_order(order_id, payload.reason))


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_order(order_id))


# ----------------------------------------------------------------------
# Financeiro
# ----------------------------------------------------------------------


@app.get("/api/finance/operating")
def operating_result(
    days: int = Query(default=30, ge=1, le=365),
    service: ERPService = Depends(erp),
    delivery: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> dict:
    """Lucro operacional sem mascarar pedidos que ainda não têm custo."""
    delivery_cost = delivery.performance(days)["totals"]["payout"]
    return service.operating_result(days, delivery_cost)


def anotaai_service(request: Request) -> AnotaAiService:
    return request.app.state.anotaai


def _anota(acao):
    """Recusa da Anota Aí é texto para a tela, não erro 500."""
    try:
        return acao()
    except AnotaAiError as erro:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(erro)) from erro


@app.get("/api/integrations/anota-ai")
def anotaai_status(service: AnotaAiService = Depends(anotaai_service), _: dict = Depends(current_user)) -> dict:
    """Se está conectada e quando foi a última sincronização. A chave nunca volta."""
    return service.status()


@app.post("/api/integrations/anota-ai/connect")
def anotaai_connect(
    payload: AnotaAiConnect,
    service: AnotaAiService = Depends(anotaai_service),
    _: dict = Depends(current_user),
) -> dict:
    """Guarda a chave no cofre e confirma na hora que ela funciona.

    Confirmar agora evita o pior modo de falha: chave errada guardada em
    silêncio, e três dias depois "a sincronização não traz nada".
    """
    return _anota(lambda: service.connect(payload.integration_key))


@app.delete("/api/integrations/anota-ai")
def anotaai_disconnect(service: AnotaAiService = Depends(anotaai_service), _: dict = Depends(current_user)) -> dict:
    return _anota(service.disconnect)


@app.post("/api/integrations/anota-ai/sync")
def anotaai_sync(service: AnotaAiService = Depends(anotaai_service), _: dict = Depends(current_user)) -> dict:
    """Traz os pedidos novos da Anota Aí e os põe na fila do entregador.

    A fila deles é aviso de pedido novo, não histórico: resolve do momento da
    ativação em diante. O passado continua vindo da planilha, e os dois caminhos
    usam o mesmo identificador de pedido — então o que já entrou por aqui é
    pulado quando a planilha da semana chegar.
    """
    return _anota(service.sync)


@app.get("/api/integrations/anota-ai/probe")
def anotaai_probe(service: AnotaAiService = Depends(anotaai_service), _: dict = Depends(current_user)) -> dict:
    """Mostra a forma crua de um pedido da fila.

    A resposta de `/ping/get` nunca pôde ser conferida por falta de chave
    válida, então o tradutor procura o valor em vários caminhos plausíveis. Esta
    rota existe para a primeira chamada de verdade mostrar a estrutura — e a
    tradução deixar de ser hipótese.
    """
    return _anota(service.probe)


@app.get("/api/integrations/anota-ai/catalog")
def anotaai_catalog(service: AnotaAiService = Depends(anotaai_service), _: dict = Depends(current_user)) -> dict:
    """Compara o cardápio de lá com o daqui, sem gravar nada."""
    return _anota(service.catalog_preview)


@app.post("/api/integrations/anota-ai/catalog/import")
def anotaai_catalog_import(
    payload: AnotaAiCatalogImport,
    service: AnotaAiService = Depends(anotaai_service),
    _: dict = Depends(current_user),
) -> dict:
    """Cria no ERP os itens escolhidos — sem ficha técnica, que é da loja."""
    return _anota(lambda: service.import_catalog(payload.anota_ids))


@app.post("/api/imports/anota-ai")
async def import_anota_ai_report(
    report: UploadFile = File(...),
    overwrite: bool = Query(default=False),
    database: Store = Depends(store),
    _: dict = Depends(current_user),
) -> dict:
    """Importa o relatório XLSX da Anota AI com deduplicação por pedido."""
    if not (report.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Envie o relatório da Anota AI em formato .xlsx.")
    content = await report.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="O relatório excede o limite de 10 MB.")

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as temporary:
            temporary.write(content)
            temp_path = Path(temporary.name)
        orders, discarded = ler_relatorio_anota_ai(temp_path)
        result = importar_anota_ai(orders, database, sobrescrever=overwrite)
        return {
            **result,
            "orders_read": len(orders),
            "revenue": round(sum(order["total"] for order in orders), 2),
            "discarded": discarded,
            "detail": "Pedidos importados. Itens que não vierem no relatório continuam sem custo até serem detalhados.",
        }
    except (BadZipFile, ParseError, KeyError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="A planilha está inválida ou corrompida. Exporte um novo arquivo .xlsx.") from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


@app.post("/api/imports/anota-ai/items")
async def import_anota_ai_items(
    report: UploadFile = File(...),
    database: Store = Depends(store),
    _: dict = Depends(current_user),
) -> dict:
    """Enriquece pedidos já importados com os itens vendidos.

    O relatório de pedidos traz o faturamento e não traz o que foi vendido, e é
    essa falta que deixa custo, margem por produto e sugestão de promoção sem
    base. Este segundo relatório fecha a lacuna sem refazer a importação — e sem
    tocar no estoque, porque a baixa desses insumos já aconteceu na prateleira.
    """

    if not (report.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Envie o relatório de itens em formato .xlsx.")
    content = await report.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="O relatório excede o limite de 10 MB.")

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as temporary:
            temporary.write(content)
            temp_path = Path(temporary.name)
        items, discarded = ler_relatorio_itens_anota_ai(temp_path)
        return {**aplicar_itens_anota_ai(items, database), "discarded": discarded}
    except (BadZipFile, ParseError, KeyError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="A planilha está inválida ou corrompida. Exporte um novo arquivo .xlsx.") from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


@app.get("/api/expenses")
def expenses(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.expenses()


@app.post("/api/expenses", status_code=status.HTTP_201_CREATED)
def create_expense(payload: ExpenseCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_expense(payload))


@app.get("/api/expenses/recurring")
def expense_series(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    """As despesas que se repetem todo mês, com o próximo vencimento de cada uma."""
    return service.expense_series()


@app.get("/api/expenses/fixed-cost")
def fixed_monthly_cost(
    month: str | None = Query(default=None, description="AAAA-MM. Vazio = mês corrente."),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Custo fixo do mês pelas regras cadastradas — a base do lucro projetado."""
    return _run(lambda: service.recurring_monthly_cost(month))


@app.post("/api/expenses/recurring", status_code=status.HTTP_201_CREATED)
def setup_fixed_costs(payload: FixedCostSetup, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    """Monta o custo fixo da loja de uma vez: cada linha vira uma despesa mensal."""
    return _run(lambda: service.setup_fixed_costs(payload))


@app.patch("/api/expenses/recurring/{series_id}")
def update_expense_series(
    series_id: str,
    payload: ExpenseSeriesUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    """Muda a regra dos meses que ainda serão lançados; o passado fica como está."""
    return _run(lambda: service.update_expense_series(series_id, payload))


@app.delete("/api/expenses/recurring/{series_id}")
def stop_expense_series(series_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    """Para de repetir, mantendo no histórico os meses já lançados."""
    return _run(lambda: service.stop_expense_series(series_id))


@app.patch("/api/expenses/{expense_id}")
def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_expense(expense_id, payload))


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_expense(expense_id))


# ----------------------------------------------------------------------
# Canais de distribuição
# ----------------------------------------------------------------------


@app.get("/api/channels")
def channels(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.channels()


@app.get("/api/channels/performance")
def channel_performance(
    days: int = Query(default=30, ge=1, le=365),
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return service.channel_performance(days)


@app.post("/api/channels", status_code=status.HTTP_201_CREATED)
def create_channel(payload: ChannelCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_channel(payload))


@app.patch("/api/channels/{channel_id}")
def update_channel(
    channel_id: str,
    payload: ChannelUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_channel(channel_id, payload))


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_channel(channel_id))


# ----------------------------------------------------------------------
# Promoções da semana e âncoras esportivas
# ----------------------------------------------------------------------


@app.get("/api/promotions/week")
def promotions_week(
    days: int = Query(default=90, ge=14, le=365),
    horizon: int = Query(default=7, ge=1, le=14),
    database: Store = Depends(store),
    _: dict = Depends(current_user),
) -> dict:
    """Calendário de promoções dos próximos dias, com o ponto de virada de cada uma."""
    return PromotionService(database).week(days, horizon)


@app.get("/api/promotions/month")
def promotions_month(
    days: int = Query(default=90, ge=14, le=365),
    weeks: int = Query(default=4, ge=1, le=8),
    database: Store = Depends(store),
    _: dict = Depends(current_user),
) -> dict:
    """Calendário do mês: uma oferta por bloco da semana, girando as alavancas."""
    return PromotionService(database).month(days, weeks)


@app.get("/api/pricing")
def pricing_guidance(
    days: int = Query(default=90, ge=7, le=365),
    product_id: str | None = Query(default=None),
    database: Store = Depends(store),
    _: dict = Depends(current_user),
) -> dict:
    """Como precificar: piso de custo, margem-alvo e as duas leituras de mercado."""
    return PricingService(database).guidance(days, product_id)


@app.get("/api/market-prices")
def market_prices(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.market_prices()


@app.post("/api/market-prices", status_code=status.HTTP_201_CREATED)
def create_market_price(
    payload: MarketPriceCreate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.create_market_price(payload))


@app.patch("/api/market-prices/{reference_id}")
def update_market_price(
    reference_id: str,
    payload: MarketPriceUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_market_price(reference_id, payload))


@app.delete("/api/market-prices/{reference_id}")
def delete_market_price(reference_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_market_price(reference_id))


@app.get("/api/sport-anchors")
def sport_anchors(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.sport_anchors()


@app.post("/api/sport-anchors", status_code=status.HTTP_201_CREATED)
def create_sport_anchor(
    payload: SportAnchorCreate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.create_sport_anchor(payload))


@app.patch("/api/sport-anchors/{anchor_id}")
def update_sport_anchor(
    anchor_id: str,
    payload: SportAnchorUpdate,
    service: ERPService = Depends(erp),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_sport_anchor(anchor_id, payload))


@app.delete("/api/sport-anchors/{anchor_id}")
def delete_sport_anchor(anchor_id: str, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_sport_anchor(anchor_id))


# ----------------------------------------------------------------------
# Entregas
# ----------------------------------------------------------------------


@app.get("/api/couriers")
def couriers(service: DeliveryService = Depends(delivery_service), _: dict = Depends(current_user)) -> list[dict]:
    return service.couriers()


@app.get("/api/couriers/performance")
def courier_performance(
    days: int = Query(default=30, ge=1, le=365),
    service: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> dict:
    """Quanto cada entregador entregou, arrecadou de frete e custou."""
    return service.performance(days)


@app.post("/api/couriers", status_code=status.HTTP_201_CREATED)
def create_courier(payload: CourierCreate, service: DeliveryService = Depends(delivery_service), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_courier(payload))


@app.patch("/api/couriers/{courier_id}")
def update_courier(
    courier_id: str,
    payload: CourierUpdate,
    service: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_courier(courier_id, payload))


@app.delete("/api/couriers/{courier_id}")
def delete_courier(courier_id: str, service: DeliveryService = Depends(delivery_service), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_courier(courier_id))


@app.get("/api/deliveries")
def deliveries(
    days: int = Query(default=30, ge=1, le=365),
    courier_id: str | None = None,
    service: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> list[dict]:
    return service.entries(days, courier_id)


@app.get("/api/deliveries/suggest")
def suggest_delivery(
    courier_id: str,
    day: str,
    service: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> dict:
    """Preenche o turno com o que os pedidos do dia já registram."""
    return _run(lambda: service.suggest(courier_id, day))


@app.post("/api/deliveries", status_code=status.HTTP_201_CREATED)
def create_delivery(payload: DeliveryEntryCreate, service: DeliveryService = Depends(delivery_service), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_entry(payload))


@app.patch("/api/deliveries/{entry_id}")
def update_delivery(
    entry_id: str,
    payload: DeliveryEntryUpdate,
    service: DeliveryService = Depends(delivery_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_entry(entry_id, payload))


@app.delete("/api/deliveries/{entry_id}")
def delete_delivery(entry_id: str, service: DeliveryService = Depends(delivery_service), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete_entry(entry_id))


# ----------------------------------------------------------------------
# Zonas de entrega, acompanhamento e território
#
# Tudo aqui é da loja: exige `erp_admin` como o resto do ERP. O que o
# entregador alcança está no bloco seguinte, e só lá.
# ----------------------------------------------------------------------


@app.get("/api/delivery/zones")
def delivery_zones(service: DispatchService = Depends(dispatch_service), _: dict = Depends(current_user)) -> list[dict]:
    return service.zones()


@app.post("/api/delivery/zones", status_code=status.HTTP_201_CREATED)
def create_delivery_zone(
    payload: DeliveryZoneCreate,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.create_zone(payload))


@app.patch("/api/delivery/zones/{zone_id}")
def update_delivery_zone(
    zone_id: str,
    payload: DeliveryZoneUpdate,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.update_zone(zone_id, payload))


@app.delete("/api/delivery/zones/{zone_id}")
def delete_delivery_zone(
    zone_id: str,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.delete_zone(zone_id))


@app.get("/api/delivery/board")
def delivery_board(
    days: int = Query(default=30, ge=1, le=365),
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    """Quem está na rua agora e o que o período mostra por zona e por entregador."""
    return service.board(days)


@app.get("/api/delivery/territory")
def delivery_territory(
    days: int = Query(default=90, ge=7, le=365),
    horizon: int = Query(default=7, ge=1, le=30),
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    """Demanda por zona e o que cada lugar pede mais que a média da loja."""
    return service.territory(days, horizon)


@app.get("/api/delivery/runs")
def delivery_runs(
    days: int = Query(default=30, ge=1, le=365),
    courier_id: str | None = None,
    zone_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> list[dict]:
    return service.runs(days, courier_id, zone_id)


@app.patch("/api/delivery/runs/{run_id}")
def update_delivery_run(
    run_id: str,
    payload: DeliveryRunUpdate,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    """Corrige a entrega registrada na rua — zona trocada na pressa, quase sempre."""
    return _run(lambda: service.update_run(run_id, payload))


@app.delete("/api/delivery/runs/{run_id}")
def delete_delivery_run(
    run_id: str,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.delete_run(run_id))


@app.get("/api/delivery/store-location")
def store_location(service: DispatchService = Depends(dispatch_service), _: dict = Depends(current_user)) -> dict:
    """Onde fica a loja e até que distância o ponto é aceito."""
    return service.store_location()


@app.put("/api/delivery/store-location")
def set_store_location(
    payload: StoreLocation,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    """Grava a posição da loja, capturada de dentro dela.

    Capturar no lugar é melhor do que converter o endereço em coordenada: o
    conversor devolve o meio da rua, e duzentos metros de erro já recusariam
    quem chegou na porta.
    """
    return _run(lambda: service.set_store_location(payload))


@app.get("/api/delivery/shifts")
def delivery_shifts(
    days: int = Query(default=30, ge=1, le=365),
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> list[dict]:
    """O ponto de todo mundo: quem entrou, a que horas e por quanto tempo."""
    return service.shifts(days, courier_id)


@app.get("/api/delivery/pending")
def pending_orders(
    days: int = Query(default=30, ge=1, le=365),
    include_done: bool = False,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> list[dict]:
    """Vendas que o entregador trouxe da rua e ainda não viraram faturamento."""
    return service.pending_orders(days, include_done)


@app.post("/api/delivery/pending/{pending_id}/confirm")
def confirm_pending_order(
    pending_id: str,
    payload: PendingOrderConfirm,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    """Promove a venda da rua a pedido: baixa a ficha técnica e vira faturamento."""
    return _run(lambda: service.confirm_pending(pending_id, payload))


@app.post("/api/delivery/pending/{pending_id}/reject")
def reject_pending_order(
    pending_id: str,
    payload: PendingOrderReject,
    service: DispatchService = Depends(dispatch_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.reject_pending(pending_id, payload))


# ----------------------------------------------------------------------
# Aplicativo de rua
#
# Estas são as únicas rotas que uma conta de entregador alcança. Ele não tem
# `erp_admin`, então todo o resto do ERP responde 403 para ele — e isso não
# depende de nenhuma lista de bloqueio mantida à mão: é a exigência que já
# estava em cada rota da loja.
#
# `courier_id` aparece nas assinaturas apenas para o modo demonstrativo, onde
# não há autenticação e portanto ninguém para identificar. Com autenticação
# ligada quem fala é a conta, e o parâmetro é ignorado: aceitá-lo deixaria um
# entregador abrir turno e lançar entrega no nome de outro.
# ----------------------------------------------------------------------


def _courier_of(request: Request, service: DispatchService, fallback: str | None) -> dict:
    """O entregador dono da chamada.

    O identificador vindo da tela só é aceito de quem tem direito de escolher:
    a loja, que abre o aplicativo de rua para fechar o turno de quem ficou sem
    bateria, e o modo demonstrativo, que não tem e-mail para casar. Aceitá-lo
    de uma conta de entregador deixaria um deles lançar no nome do outro.
    """

    identidade = current_field_user(request)
    pode_escolher = not request.app.state.settings.auth_required or identidade.get("erp_admin") is True
    return service.courier_for(identidade, fallback if pode_escolher else None, pode_escolher)


@app.get("/api/field/state")
def field_state(
    request: Request,
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    """Turno, entregas em rota e fila do dia — tudo numa chamada.

    O celular do entregador está na calçada com sinal ruim. As trinta e quatro
    requisições que o painel da loja dispara seriam uma tela branca, e tela
    branca na calçada é registro que não acontece.
    """
    # A loja abrindo a tela de rua sem dizer por quem: a resposta é a lista de
    # entregadores para escolher, e não uma recusa — foi o beco sem saída que o
    # gestor encontrou ao abrir o próprio link. A resolução fica fora do `_run`
    # de propósito: ele traduz ValueError em 422, e `SemEntregadorEscolhido` é
    # um ValueError, então lá dentro a pergunta viraria recusa antes de alguém
    # poder respondê-la.
    try:
        entregador = _courier_of(request, service, courier_id)
    except SemEntregadorEscolhido as escolha:
        return {
            "needs_courier": True,
            "detail": str(escolha),
            "couriers": [
                {"id": linha["id"], "name": linha["name"], "vehicle": linha.get("vehicle")}
                for linha in service.couriers_for_picker()
            ],
        }
    return _run(lambda: service.field_state(entregador))


@app.get("/api/field/history")
def field_history(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    """Os turnos e as entregas do próprio entregador, com hora de entrada e saída.

    A tela de rua mostrava só o turno aberto. Sem isto o entregador não tem
    como conferir o próprio acerto — e conferir o próprio acerto é a razão de
    o registro ser feito.
    """
    return _run(lambda: service.field_history(_courier_of(request, service, courier_id), days))


@app.post("/api/field/zones", status_code=status.HTTP_201_CREATED)
def propose_field_zone(
    request: Request,
    payload: FieldZoneCreate,
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    """Cria uma zona a partir da rua, quando nenhuma cadastrada serve.

    Ela entra marcada como proposta e com frete zero: quem está na calçada não
    decide preço de frete, mas também não pode ficar parado esperando o balcão.
    """
    return _run(lambda: service.propose_zone(_courier_of(request, service, courier_id), payload))


@app.post("/api/field/check-in", status_code=status.HTTP_201_CREATED)
def field_check_in(
    request: Request,
    payload: CourierCheckIn,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    return _run(lambda: service.check_in(_courier_of(request, service, payload.courier_id), payload))


@app.post("/api/field/check-out")
def field_check_out(
    request: Request,
    payload: CourierCheckOut,
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    """Fecha o turno e monta o acerto a partir do que foi registrado na rua."""
    return _run(lambda: service.check_out(_courier_of(request, service, courier_id), payload))


@app.post("/api/field/runs", status_code=status.HTTP_201_CREATED)
def field_register_run(
    request: Request,
    payload: DeliveryRunCreate,
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    return _run(lambda: service.register_run(_courier_of(request, service, courier_id), payload))


@app.post("/api/field/runs/{run_id}/finish")
def field_finish_run(
    request: Request,
    run_id: str,
    payload: DeliveryRunFinish,
    courier_id: str | None = None,
    service: DispatchService = Depends(dispatch_service),
) -> dict:
    return _run(lambda: service.finish_run(_courier_of(request, service, courier_id), run_id, payload))


# ----------------------------------------------------------------------
# Listas editáveis
# ----------------------------------------------------------------------


@app.get("/api/catalogs")
def catalogs(service: CatalogService = Depends(catalog_service), _: dict = Depends(current_user)) -> list[dict]:
    """Listas que alimentam os campos de escolha, com a contagem de uso."""
    return service.all()


@app.post("/api/catalogs/{catalog_id}/items", status_code=status.HTTP_201_CREATED)
def add_catalog_item(
    catalog_id: str,
    payload: CatalogItem,
    service: CatalogService = Depends(catalog_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.add(catalog_id, payload.value))


@app.patch("/api/catalogs/{catalog_id}/items")
def rename_catalog_item(
    catalog_id: str,
    payload: CatalogRename,
    service: CatalogService = Depends(catalog_service),
    _: dict = Depends(current_user),
) -> dict:
    """Renomeia o item e leva junto os registros que o usam."""
    return _run(lambda: service.rename(catalog_id, payload.value, payload.new_value))


@app.patch("/api/catalogs/{catalog_id}/order")
def reorder_catalog(
    catalog_id: str,
    payload: CatalogOrder,
    service: CatalogService = Depends(catalog_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.reorder(catalog_id, payload.items))


@app.delete("/api/catalogs/{catalog_id}/items/{value}")
def delete_catalog_item(
    catalog_id: str,
    value: str,
    service: CatalogService = Depends(catalog_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.remove(catalog_id, value))


# ----------------------------------------------------------------------
# Arquivos
# ----------------------------------------------------------------------


@app.get("/api/media")
def media_items(
    category: str | None = None,
    kind: str | None = None,
    service: MediaService = Depends(media),
    _: dict = Depends(current_user),
) -> list[dict]:
    return service.list(category=category, kind=kind)


@app.get("/api/media/summary")
def media_summary(service: MediaService = Depends(media), _: dict = Depends(current_user)) -> dict:
    return service.summary()


@app.post("/api/media/sync")
def sync_media(service: MediaService = Depends(media), _: dict = Depends(current_user)) -> dict:
    return service.sync_workspace()


@app.post("/api/media/upload", status_code=status.HTTP_201_CREATED)
async def upload_media(
    files: list[UploadFile] = File(...),
    category: str | None = Form(default=None),
    service: MediaService = Depends(media),
    _: dict = Depends(current_user),
) -> list[dict]:
    uploaded = []
    for file in files:
        content = await file.read(25 * 1024 * 1024 + 1)
        uploaded.append(_run(lambda: service.upload(file.filename or "arquivo", content, category)))
    return uploaded


@app.patch("/api/media/{media_id}")
def update_media(media_id: str, payload: MediaUpdate, service: MediaService = Depends(media), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.update(media_id, payload))


@app.delete("/api/media/{media_id}")
def delete_media(media_id: str, service: MediaService = Depends(media), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.delete(media_id))


@app.get("/api/media/{media_id}/content")
def media_content(media_id: str, service: MediaService = Depends(media), _: dict = Depends(current_user)) -> Response:
    record = service.get(media_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arquivo não encontrado.")
    local_path = service.local_path(record)
    if local_path:
        return FileResponse(local_path, media_type=record["mime_type"], filename=record["original_name"], content_disposition_type="inline")
    safe_name = quote(record["original_name"], safe="")
    return Response(
        content=service.content(record),
        media_type=record["mime_type"],
        headers={"Content-Disposition": f"inline; filename*=utf-8''{safe_name}"},
    )


# ----------------------------------------------------------------------
# Integração com o iFood
# ----------------------------------------------------------------------
#
# A ativação tem duas metades porque o iFood a fez assim: aqui se gera o código
# que a loja leva ao Portal do Parceiro, e o portal devolve um código de
# autorização que volta na segunda chamada. Nada que autentica sai por estas
# rotas — o estado responde se está conectado, nunca com o quê.


@app.get("/api/integrations/ifood")
def ifood_status(service: IFoodService = Depends(ifood_service), _: dict = Depends(current_user)) -> dict:
    return service.status()


@app.post("/api/integrations/ifood/activation")
def ifood_start_activation(
    payload: IFoodActivationStart,
    service: IFoodService = Depends(ifood_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.start_activation(payload))


@app.post("/api/integrations/ifood/activation/renew")
def ifood_renew_activation(
    service: IFoodService = Depends(ifood_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(service.renew_activation)


@app.post("/api/integrations/ifood/activation/confirm")
def ifood_confirm_activation(
    payload: IFoodActivationConfirm,
    service: IFoodService = Depends(ifood_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.confirm_activation(payload))


@app.post("/api/integrations/ifood/sync")
def ifood_sync(
    force: bool = Query(default=False),
    service: IFoodService = Depends(ifood_service),
    _: dict = Depends(current_user),
) -> dict:
    return _run(lambda: service.sync(force))


@app.delete("/api/integrations/ifood")
def ifood_disconnect(service: IFoodService = Depends(ifood_service), _: dict = Depends(current_user)) -> dict:
    return _run(service.disconnect)


def _run(action):
    try:
        return action()
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
