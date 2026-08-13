"""Centralized Firebase Admin SDK initialization for trusted server code."""

from typing import Any

from app.config import Settings


def get_firebase_app(settings: Settings) -> Any:
    """Return the process-wide Admin app using ADC or an explicit key file."""

    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise RuntimeError("firebase-admin is required for Firebase mode") from error

    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    options = {
        key: value
        for key, value in {
            "projectId": settings.firebase_project_id,
            "storageBucket": settings.firebase_storage_bucket,
        }.items()
        if value
    }
    if settings.firebase_service_account_path:
        credential = credentials.Certificate(settings.firebase_service_account_path)
        return firebase_admin.initialize_app(credential, options=options or None)
    return firebase_admin.initialize_app(options=options or None)
