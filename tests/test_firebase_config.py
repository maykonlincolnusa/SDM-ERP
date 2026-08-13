import pytest
from pydantic import ValidationError

from app.config import Settings


def test_firebase_mode_requires_project_and_bucket():
    with pytest.raises(ValidationError, match="FIREBASE_PROJECT_ID"):
        Settings(data_mode="firebase", firebase_project_id=None, firebase_storage_bucket=None)


def test_production_firebase_requires_authentication():
    with pytest.raises(ValidationError, match="AUTH_REQUIRED"):
        Settings(
            app_env="production",
            data_mode="firebase",
            firebase_project_id="acai-test",
            firebase_storage_bucket="acai-test.firebasestorage.app",
        )


def test_production_auto_mode_resolving_to_firebase_requires_authentication():
    """DATA_MODE defaults to "auto"; build_store() still activates Firestore once
    FIREBASE_PROJECT_ID/FIREBASE_STORAGE_BUCKET are set, so this path must be
    guarded exactly like an explicit DATA_MODE=firebase deployment."""

    with pytest.raises(ValidationError, match="AUTH_REQUIRED"):
        Settings(
            app_env="production",
            firebase_project_id="acai-test",
            firebase_storage_bucket="acai-test.firebasestorage.app",
        )


def test_development_firebase_configuration_is_valid():
    settings = Settings(
        data_mode="firebase",
        firebase_project_id="acai-test",
        firebase_storage_bucket="acai-test.firebasestorage.app",
    )

    assert settings.firebase_configured
