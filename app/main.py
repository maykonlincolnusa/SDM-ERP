from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.repositories.store import Store, build_store
from app.schemas import (
    AddonCreate,
    AddonUpdate,
    CatalogItem,
    CatalogOrder,
    CatalogRename,
    ChannelCreate,
    ChannelUpdate,
    CourierCreate,
    CourierUpdate,
    DeliveryEntryCreate,
    DeliveryEntryUpdate,
    ExpenseCreate,
    ExpenseSeriesUpdate,
    ExpenseUpdate,
    FixedCostSetup,
    InventoryAdjustment,
    InventoryCount,
    InventoryItemCreate,
    InventoryItemUpdate,
    InventoryPurchase,
    InventoryWithdrawal,
    MarketPriceCreate,
    MarketPriceUpdate,
    MediaUpdate,
    MovementUpdate,
    OrderCancel,
    OrderCreate,
    OrderUpdate,
    ProductCreate,
    ProductSimulation,
    ProductUpdate,
    SportAnchorCreate,
    SportAnchorUpdate,
)
from app.services import (
    CatalogService,
    CostingService,
    DeliveryService,
    ERPService,
    ForecastService,
    GrowthService,
    MediaService,
    PlanningService,
    PricingService,
    PromotionService,
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
    app.state.catalog = CatalogService(app.state.store)
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


def catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog


def store(request: Request) -> Store:
    return request.app.state.store


def media(request: Request) -> MediaService:
    return request.app.state.media


def current_user(request: Request) -> dict:
    """Optional production guard using Firebase Authentication ID tokens."""
    settings: Settings = request.app.state.settings
    if not settings.auth_required:
        return {"uid": "demo-user"}
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Autenticação necessária.")
    try:  # pragma: no cover - only exercised with a Firebase project
        from firebase_admin import auth

        user = auth.verify_id_token(header.removeprefix("Bearer "))
        if user.get("erp_admin") is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sua conta ainda nao tem permissao para acessar a loja.",
            )
        return user
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão inválida.") from error


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/health")
def health_check(database: Store = Depends(store)) -> dict[str, str]:
    return {"status": "healthy", "storage": database.mode}


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
    content = await report.read()
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
    content = await report.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="O relatório excede o limite de 10 MB.")

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as temporary:
            temporary.write(content)
            temp_path = Path(temporary.name)
        items, discarded = ler_relatorio_itens_anota_ai(temp_path)
        return {**aplicar_itens_anota_ai(items, database), "discarded": discarded}
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
        content = await file.read()
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
    safe_name = record["original_name"].replace('"', "")
    return Response(
        content=service.content(record),
        media_type=record["mime_type"],
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


def _run(action):
    try:
        return action()
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
