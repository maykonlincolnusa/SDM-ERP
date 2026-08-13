"""Verify Firestore access without creating or changing ERP data."""

import json

from app.config import Settings, get_settings
from app.repositories.store import FirestoreStore


def firebase_settings() -> Settings:
    source = get_settings()
    return Settings(
        app_env=source.app_env,
        data_mode="firebase",
        firebase_project_id=source.firebase_project_id,
        firebase_service_account_path=source.firebase_service_account_path,
        firebase_storage_bucket=source.firebase_storage_bucket,
        auth_required=source.auth_required,
        firebase_web_api_key=source.firebase_web_api_key,
        firebase_web_auth_domain=source.firebase_web_auth_domain,
        firebase_web_app_id=source.firebase_web_app_id,
        firebase_web_messaging_sender_id=source.firebase_web_messaging_sender_id,
        firebase_web_measurement_id=source.firebase_web_measurement_id,
    )


def main() -> int:
    try:
        settings = firebase_settings()
        store = FirestoreStore(settings)
        database = store.db
        # A read-only probe confirms the database and credentials without polluting data.
        list(database.collection("_erp_connection_checks").limit(1).stream(timeout=10))
        from firebase_admin import storage

        bucket = storage.bucket(settings.firebase_storage_bucket, app=store.app)
        if not bucket.exists(timeout=10):
            raise RuntimeError("Configured Firebase Storage bucket was not found.")
    except Exception as error:
        print(json.dumps({"connected": False, "error": str(error)}, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {
                "connected": True,
                "project_id": settings.firebase_project_id,
                "database": "(default)",
                "storage_bucket": settings.firebase_storage_bucket,
                "operation": "read-only",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
