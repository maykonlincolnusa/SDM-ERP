"""Real page and API, with an isolated demo store and no network service."""

from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_withdrawal_in_browser_updates_stock_and_clears_override(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    for key, value in {
        "DATA_MODE": "demo", "DEMO_DATA_PATH": str(tmp_path / "store.json"),
        "AUTH_REQUIRED": "false", "DATA_RAW_PATH": str(tmp_path / "raw"),
        "MEDIA_INBOX_PATH": str(tmp_path / "inbox"),
        "MEDIA_LIBRARY_PATH": str(tmp_path / "library"), "MEDIA_SYNC_LEGACY_FILES": "false",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client, playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except playwright.Error as error:
                pytest.skip(f"Chromium unavailable: {error}")
            page = browser.new_page(viewport={"width": 375, "height": 900}, service_workers="block")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != "testserver":
                    route.fulfill(status=200, body="", content_type="text/css")
                    return
                response = client.request(request.method, url.path + (f"?{url.query}" if url.query else ""),
                    content=request.post_data_buffer, headers={"content-type": request.headers.get("content-type", "")})
                route.fulfill(status=response.status_code, body=response.content,
                    content_type=response.headers.get("content-type", "application/octet-stream"))

            # A barra existe no cadastro, nao nos dados de demonstracao: e ela
            # que prova a conversao ponta a ponta, do campo ate o saldo.
            barra = client.post("/api/inventory", json={
                "name": "Barra de açaí 12%", "unit": "kg", "quantity": 28.0,
                "minimum_quantity": 5.6, "unit_cost": 20.0, "category": "Polpas",
                "pack_unit": "barra", "pack_size": 2.8,
            }).json()

            page.route("**/*", serve)
            page.goto("http://testserver/#inventory")
            page.wait_for_function("state.inventory.length > 0", timeout=90000)

            def saldo(inventory_id):
                return next(row for row in client.get("/api/inventory").json() if row["id"] == inventory_id)["quantity"]

            initial = saldo("acai")
            page.locator('#inventory [data-modal="withdrawal"]').click()
            page.locator('#withdrawal-form [name="responsible"]').fill("Teste navegador")
            page.locator('#withdrawal-lines [data-withdrawal-item]').select_option("acai")
            quantity = page.locator('#withdrawal-lines [data-withdrawal-quantity]')
            # Insumo sem unidade de manuseio so oferece a unidade de estoque.
            assert page.locator('#withdrawal-lines [data-withdrawal-unit] option').count() == 1
            quantity.fill(str(initial + 1))
            page.locator('#withdrawal-override').check()
            quantity.fill("")
            assert not page.locator('#withdrawal-override').is_checked()
            assert page.locator('#withdrawal-override-field').is_hidden()
            # A virgula do teclado brasileiro precisa chegar ao lancamento: o
            # campo de numero devolvia vazio e o operador desistia do registro.
            quantity.fill("0,001")
            assert quantity.evaluate("e => { const r=e.getBoundingClientRect(); return r.width>=80 && document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===e; }")
            page.locator('#withdrawal-form button[type="submit"]').click()
            page.wait_for_function("!document.getElementById('withdrawal-modal').open", timeout=90000)
            page.wait_for_function("submitData.pending === false", timeout=90000)
            assert saldo("acai") == pytest.approx(initial - 0.001)

            # Duas barras saem do freezer; o estoque cai 5,6 kg sem ninguem
            # dividir 5,6 por 2,8 de cabeca.
            page.locator('#inventory [data-modal="withdrawal"]').click()
            page.locator('#withdrawal-form [name="responsible"]').fill("Teste navegador")
            page.locator('#withdrawal-lines [data-withdrawal-item]').select_option(barra["id"])
            unidade = page.locator('#withdrawal-lines [data-withdrawal-unit]')
            assert unidade.input_value() == "pack"
            page.locator('#withdrawal-lines [data-withdrawal-quantity]').fill("2")
            assert "5,6 kg" in page.locator('#withdrawal-lines [data-withdrawal-hint]').inner_text()
            page.locator('#withdrawal-form button[type="submit"]').click()
            page.wait_for_function("!document.getElementById('withdrawal-modal').open", timeout=90000)
            page.wait_for_function("submitData.pending === false", timeout=90000)
            assert saldo(barra["id"]) == pytest.approx(22.4)

            assert not errors, errors
            browser.close()
    finally:
        get_settings.cache_clear()
