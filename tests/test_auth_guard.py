from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from firebase_admin import auth

from app.config import Settings
from app.main import current_user


def authenticated_request() -> SimpleNamespace:
    settings = Settings(
        data_mode="firebase",
        firebase_project_id="acai-test",
        firebase_storage_bucket="acai-test.firebasestorage.app",
        firebase_web_api_key="public-key",
        firebase_web_auth_domain="acai-test.firebaseapp.com",
        firebase_web_app_id="web-app-id",
        auth_required=True,
    )
    return SimpleNamespace(
        headers={"authorization": "Bearer test-token"},
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
    )


def test_auth_guard_requires_erp_admin_claim(monkeypatch):
    monkeypatch.setattr(auth, "verify_id_token", lambda _: {"uid": "user-1"})

    with pytest.raises(HTTPException) as error:
        current_user(authenticated_request())

    assert error.value.status_code == 403


def test_auth_guard_accepts_erp_admin_claim(monkeypatch):
    token = {"uid": "owner-1", "erp_admin": True}
    monkeypatch.setattr(auth, "verify_id_token", lambda _: token)

    assert current_user(authenticated_request()) == token
