from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.repositories.store import Store, build_store
from app.schemas import ExpenseCreate, InventoryAdjustment, InventoryItemCreate, MediaUpdate, OrderCreate, ProductCreate
from app.services import ERPService, MediaService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.store = build_store(settings)
    app.state.erp = ERPService(app.state.store)
    app.state.media = MediaService(app.state.store, settings)
    app.state.media.sync_workspace()
    yield


app = FastAPI(
    title="Açaí Gestão",
    description="ERP operacional para gestão de lojas de açaí.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def erp(request: Request) -> ERPService:
    return request.app.state.erp


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


@app.get("/api/products")
def products(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.products()


@app.post("/api/products", status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_product(payload))


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


@app.post("/api/inventory", status_code=status.HTTP_201_CREATED)
def create_inventory_item(payload: InventoryItemCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_inventory_item(payload))


@app.post("/api/inventory/{inventory_id}/adjust")
def adjust_inventory(inventory_id: str, payload: InventoryAdjustment, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.adjust_inventory(inventory_id, payload))


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


@app.get("/api/expenses")
def expenses(service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> list[dict]:
    return service.expenses()


@app.post("/api/expenses", status_code=status.HTTP_201_CREATED)
def create_expense(payload: ExpenseCreate, service: ERPService = Depends(erp), _: dict = Depends(current_user)) -> dict:
    return _run(lambda: service.create_expense(payload))


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
