"""Safely copy the local demo records to Firestore before the first live run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.repositories.store import FirestoreStore

COLLECTIONS = ("products", "inventory", "orders", "expenses", "inventory_movements")


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


def load_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def migrate(source: dict[str, list[dict[str, Any]]], target: FirestoreStore, *, overwrite: bool) -> dict[str, int]:
    created = skipped = 0
    max_order_number = 1000
    for collection in COLLECTIONS:
        for record in source.get(collection, []):
            if collection == "orders":
                max_order_number = max(max_order_number, int(record.get("number", 1000)))
            if not overwrite and target.get(collection, record["id"]):
                skipped += 1
                continue
            target.put(collection, record["id"], record)
            created += 1

    sequence = target.get("system", "order_sequence") or {}
    current_number = int(sequence.get("value", 1000))
    if overwrite or current_number < max_order_number:
        target.put(
            "system",
            "order_sequence",
            {"id": "order_sequence", "value": max(current_number, max_order_number)},
        )
    return {"created": created, "skipped": skipped, "order_sequence": max(current_number, max_order_number)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate local demo records to Firestore.")
    parser.add_argument("--source", type=Path, default=Path("data/runtime/demo_store.json"))
    parser.add_argument("--apply", action="store_true", help="Write records to Firestore.")
    parser.add_argument("--overwrite", action="store_true", help="Replace documents that already exist.")
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"Source file not found: {args.source}")
    source = load_source(args.source)
    planned = {collection: len(source.get(collection, [])) for collection in COLLECTIONS}
    if not args.apply:
        print(json.dumps({"dry_run": True, "collections": planned, "hint": "Run again with --apply to write."}))
        return 0

    result = migrate(source, FirestoreStore(firebase_settings()), overwrite=args.overwrite)
    print(json.dumps({"dry_run": False, "collections": planned, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
