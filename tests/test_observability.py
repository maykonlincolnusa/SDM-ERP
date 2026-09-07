import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability import observe_request


def test_request_diagnostics_do_not_log_business_data(caplog):
    app = FastAPI()
    app.middleware("http")(observe_request)

    @app.get("/api/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    @app.get("/api/broken")
    def broken():
        raise RuntimeError("private-exception-value")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"), TestClient(app) as client:
        response = client.get("/api/items/private-item?secret=private-query", headers={"Authorization": "Bearer private-token"})
        failed = client.get("/api/broken")
    assert response.status_code == 200
    assert failed.status_code == 500
    assert len(response.headers["X-Request-ID"]) == 32
    assert failed.headers["X-Request-ID"] != response.headers["X-Request-ID"]
    assert response.headers["Server-Timing"].startswith("app;dur=")
    records = "\n".join(record.getMessage() for record in caplog.records if record.name == "uvicorn.error")
    assert "route=/api/items/{item_id}" in records
    assert "status=500" in records
    assert failed.headers["X-Request-ID"] in records
    assert "private-" not in records
    assert "private-" not in failed.text
