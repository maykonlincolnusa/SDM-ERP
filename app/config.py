from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Firebase is enabled only when explicitly configured."""

    model_config = SettingsConfigDict(env_file=(".env", ".env.firebase"), extra="ignore")

    app_env: str = "development"
    data_mode: Literal["auto", "firebase", "demo"] = "auto"
    firebase_project_id: str | None = None
    firebase_service_account_path: str | None = None
    firebase_storage_bucket: str | None = None
    firebase_web_api_key: str | None = None
    firebase_web_auth_domain: str | None = None
    firebase_web_app_id: str | None = None
    firebase_web_messaging_sender_id: str | None = None
    firebase_web_measurement_id: str | None = None
    auth_required: bool = False
    demo_data_path: Path = Path("data/runtime/demo_store.json")
    data_raw_path: Path = Path("data/raw")
    data_bronze_path: Path = Path("data/bronze")
    data_silver_path: Path = Path("data/silver")
    data_gold_path: Path = Path("data/gold")
    data_runtime_path: Path = Path("data/runtime")
    # Cofre de credencial de terceiro. "auto" acompanha o modo do banco: com
    # Firestore em produção o segredo vai para o Secret Manager, nunca para o
    # lado dos dados de negócio.
    secrets_backend: Literal["auto", "secret-manager", "file"] = "auto"
    secrets_file_path: Path = Path("data/runtime/secrets.json")
    media_inbox_path: Path = Path("data/raw/inbox")
    media_library_path: Path = Path("data/runtime/media_library")
    media_sync_legacy_files: bool = False

    @model_validator(mode="after")
    def validate_firebase_settings(self) -> "Settings":
        if self.firebase_service_account_path:
            credential_path = Path(self.firebase_service_account_path).expanduser()
            if not credential_path.is_file():
                raise ValueError("FIREBASE_SERVICE_ACCOUNT_PATH nao aponta para um arquivo valido.")

        if self.data_mode == "firebase":
            missing = [
                name
                for name, value in {
                    "FIREBASE_PROJECT_ID": self.firebase_project_id,
                    "FIREBASE_STORAGE_BUCKET": self.firebase_storage_bucket,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"Modo Firebase requer: {', '.join(missing)}.")

        if self.app_env.lower() == "production" and self.firebase_configured and not self.auth_required:
            raise ValueError("AUTH_REQUIRED=true e obrigatorio para Firebase em producao.")

        if self.auth_required and not self.firebase_web_configured:
            raise ValueError(
                "AUTH_REQUIRED=true requer FIREBASE_WEB_API_KEY, FIREBASE_WEB_AUTH_DOMAIN e FIREBASE_WEB_APP_ID."
            )

        return self

    @property
    def firebase_configured(self) -> bool:
        return bool(self.firebase_project_id and self.firebase_storage_bucket)

    @property
    def firebase_requested(self) -> bool:
        return self.data_mode == "firebase" or bool(
            self.firebase_project_id
            or self.firebase_storage_bucket
            or self.firebase_service_account_path
        )

    @property
    def firebase_web_configured(self) -> bool:
        return bool(
            self.firebase_web_api_key
            and self.firebase_web_auth_domain
            and self.firebase_web_app_id
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
