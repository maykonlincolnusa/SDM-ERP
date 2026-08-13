"""Idempotent bronze/silver/gold ETL orchestration."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .extract import (
    copy_content_addressed,
    detect_kind,
    discover_files,
    extract_metadata,
    sha256_file,
    write_json_if_changed,
    write_jsonl_if_changed,
    workbook_rows,
)
from .finance import normalize_financial_workbook
from .market import normalize_market_workbook
from .models import ETLPaths, RunResult
from .transform import TRANSFORMATION_VERSION, normalize_pricing_workbook


MANIFEST_VERSION = 1
SILVER_DOMAINS = {"pricing", "finance", "market", "workbook"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_domain(source_path: str, kind: str) -> str:
    """Classify a source using its raw-zone location, never sheet heuristics."""

    if kind != "xlsx":
        return "document"
    parts = source_path.replace("\\", "/").lower().split("/")
    root = parts[0] if parts else ""
    if root == "finance":
        return "finance"
    if root == "market_research":
        return "market"
    if root == "pricing":
        return "pricing"
    # Keeps standalone pricing imports compatible while refusing to infer facts
    # from arbitrary workbooks dropped into raw.
    filename = parts[-1] if parts else ""
    if "pricing" in filename or "precific" in filename:
        return "pricing"
    return "workbook"


def _is_active_pricing_source(source_path: str) -> bool:
    """Use the current pricing folder for management metrics, not references."""

    parts = source_path.replace("\\", "/").lower().split("/")
    if not parts or parts[0] != "pricing":
        return len(parts) == 1 and ("pricing" in parts[0] or "precific" in parts[0])
    return len(parts) < 2 or parts[1] != "reference"


class ETLPipeline:
    """Local, deterministic ETL pipeline based on content-addressed sources."""

    def __init__(self, paths: ETLPaths):
        self.paths = paths

    def run(self) -> RunResult:
        self.paths.ensure_directories()
        started_at = utc_now()
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        result = RunResult(run_id=run_id, started_at=started_at)
        manifest = self._load_manifest()
        descriptors = self._source_descriptors()
        result.discovered_files = len(descriptors)
        grouped = self._group_by_hash(descriptors)
        result.unique_files = len(grouped)
        active_hashes: set[str] = set()
        execution_files: list[dict[str, Any]] = []

        for source_hash, group in grouped.items():
            descriptor = group[0]
            active_hashes.add(source_hash)
            paths = sorted(item["source_path"] for item in group)
            try:
                status, written, record_counts, diagnostics, entry = self._process_source(
                    descriptor=descriptor,
                    source_paths=paths,
                    manifest_entry=manifest["files"].get(source_hash),
                )
                manifest["files"][source_hash] = entry
                result.artifacts_written += written
                result.pricing_records += record_counts["pricing"]
                result.financial_records += record_counts["finance"]
                result.market_records += record_counts["market"]
                if status == "processed":
                    result.processed_files += 1
                else:
                    result.skipped_files += 1
                execution_files.append(
                    {
                        "source_hash": source_hash,
                        "kind": descriptor["kind"],
                        "domain": entry["domain"],
                        "source_paths": paths,
                        "status": status,
                        "artifacts_written": written,
                        "records": record_counts,
                        "quality_issues": len(diagnostics),
                    }
                )
            except Exception as error:  # A bad source must not block healthy files.
                result.failed_files += 1
                message = str(error)
                result.errors.append({"source_hash": source_hash, "source_path": descriptor["source_path"], "error": message})
                manifest["files"][source_hash] = self._failed_entry(
                    descriptor=descriptor,
                    source_paths=paths,
                    previous=manifest["files"].get(source_hash),
                    error=message,
                )
                execution_files.append(
                    {
                        "source_hash": source_hash,
                        "kind": descriptor["kind"],
                        "source_paths": paths,
                        "status": "failed",
                        "error": message,
                    }
                )

        gold_products, gold_updated, document_records, quality_issues = self._build_gold(active_hashes, grouped)
        result.gold_products = gold_products
        result.document_records = document_records
        result.quality_issues = quality_issues
        result.gold_updated = gold_updated
        result.artifacts_written += int(gold_updated)
        result.finished_at = utc_now()
        manifest["schema_version"] = MANIFEST_VERSION
        manifest["updated_at"] = result.finished_at
        manifest["last_run_id"] = run_id
        write_json_if_changed(self.paths.state / "manifest.json", manifest)
        self._write_run_metadata(result, execution_files)
        return result

    def _source_descriptors(self) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for path in discover_files(self.paths.raw):
            kind = detect_kind(path)
            if kind is None:
                continue
            stat = path.stat()
            descriptors.append(
                {
                    "path": path,
                    "source_path": path.relative_to(self.paths.raw).as_posix(),
                    "source_name": path.name,
                    "source_hash": sha256_file(path),
                    "kind": kind,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        return descriptors

    @staticmethod
    def _group_by_hash(descriptors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for descriptor in descriptors:
            grouped[descriptor["source_hash"]].append(descriptor)
        return dict(sorted(grouped.items()))

    def _process_source(
        self,
        descriptor: dict[str, Any],
        source_paths: list[str],
        manifest_entry: dict[str, Any] | None,
    ) -> tuple[str, int, dict[str, int], list[dict[str, str]], dict[str, Any]]:
        source_hash = descriptor["source_hash"]
        kind = descriptor["kind"]
        domain = _source_domain(descriptor["source_path"], kind)
        artifacts = self._artifact_paths(descriptor, domain)
        can_skip = self._artifacts_exist(artifacts) and self._has_current_transform(manifest_entry, kind)
        now = utc_now()
        previous = manifest_entry or {}
        entry = {
            "source_hash": source_hash,
            "kind": kind,
            "domain": domain,
            "size_bytes": descriptor["size_bytes"],
            "source_paths": source_paths,
            "first_seen_at": previous.get("first_seen_at", now),
            "last_seen_at": now,
            "last_modified_at": max(item["modified_at"] for item in [descriptor]),
            "status": "processed",
            "transformation_version": TRANSFORMATION_VERSION if kind == "xlsx" else None,
            "artifacts": {name: path.relative_to(self.paths.bronze.parent).as_posix() for name, path in artifacts.items()},
        }
        if can_skip:
            counts = {"pricing": 0, "finance": 0, "market": 0}
            counts[domain] = self._silver_record_count(artifacts.get("silver")) if domain in counts else 0
            return "skipped", 0, counts, self._read_diagnostics(artifacts.get("diagnostics")), entry

        written = 0
        raw_copy = artifacts["raw_copy"]
        if copy_content_addressed(descriptor["path"], raw_copy):
            written += 1
        metadata = extract_metadata(
            path=descriptor["path"],
            source_hash=source_hash,
            source_path=descriptor["source_path"],
            kind=kind,
        )
        metadata["source_paths"] = source_paths
        if write_json_if_changed(artifacts["metadata"], metadata):
            written += 1
        record_counts = {"pricing": 0, "finance": 0, "market": 0}
        diagnostics: list[dict[str, str]] = []
        if kind == "xlsx":
            bronze_rows = workbook_rows(descriptor["path"], source_hash, descriptor["source_path"])
            if write_jsonl_if_changed(artifacts["bronze_rows"], bronze_rows):
                written += 1
            if domain == "pricing":
                records, diagnostics = normalize_pricing_workbook(descriptor["path"], source_hash, descriptor["source_path"])
            elif domain == "finance":
                records, diagnostics = normalize_financial_workbook(descriptor["path"], source_hash, descriptor["source_path"])
            elif domain == "market":
                records, diagnostics = normalize_market_workbook(descriptor["path"], source_hash, descriptor["source_path"])
            else:
                records = []
                diagnostics = [{"sheet": "*", "reason": "unclassified_workbook"}]
            if domain in record_counts:
                record_counts[domain] = len(records)
            if "silver" in artifacts and write_jsonl_if_changed(artifacts["silver"], records):
                written += 1
            if "diagnostics" in artifacts and write_json_if_changed(artifacts["diagnostics"], diagnostics):
                written += 1
        entry["record_counts"] = record_counts
        entry["quality_issues"] = len(diagnostics)
        return "processed", written, record_counts, diagnostics, entry

    def _artifact_paths(self, descriptor: dict[str, Any], domain: str) -> dict[str, Path]:
        source_hash = descriptor["source_hash"]
        suffix = descriptor["path"].suffix.lower()
        artifacts = {
            "raw_copy": self.paths.bronze / "files" / f"{source_hash}{suffix}",
            "metadata": self.paths.bronze / "metadata" / f"{source_hash}.json",
        }
        if descriptor["kind"] == "xlsx":
            silver_domain = domain if domain in SILVER_DOMAINS else "workbook"
            artifacts.update(
                {
                    "bronze_rows": self.paths.bronze / "xlsx" / f"{source_hash}.jsonl",
                    "silver": self.paths.silver / silver_domain / f"{source_hash}.jsonl",
                    "diagnostics": self.paths.silver / silver_domain / f"{source_hash}.diagnostics.json",
                }
            )
        return artifacts

    @staticmethod
    def _has_current_transform(entry: dict[str, Any] | None, kind: str) -> bool:
        if not entry or entry.get("status") != "processed":
            return False
        return kind != "xlsx" or entry.get("transformation_version") == TRANSFORMATION_VERSION

    @staticmethod
    def _artifacts_exist(artifacts: dict[str, Path]) -> bool:
        return all(path.exists() for path in artifacts.values())

    def _failed_entry(
        self,
        descriptor: dict[str, Any],
        source_paths: list[str],
        previous: dict[str, Any] | None,
        error: str,
    ) -> dict[str, Any]:
        now = utc_now()
        return {
            "source_hash": descriptor["source_hash"],
            "kind": descriptor["kind"],
            "domain": _source_domain(descriptor["source_path"], descriptor["kind"]),
            "size_bytes": descriptor["size_bytes"],
            "source_paths": source_paths,
            "first_seen_at": (previous or {}).get("first_seen_at", now),
            "last_seen_at": now,
            "status": "failed",
            "error": error,
            "transformation_version": TRANSFORMATION_VERSION if descriptor["kind"] == "xlsx" else None,
            "artifacts": {},
        }

    @staticmethod
    def _silver_record_count(path: Path | None) -> int:
        if path is None or not path.exists():
            return 0
        with path.open(encoding="utf-8") as stream:
            return sum(1 for line in stream if line.strip())

    @staticmethod
    def _read_diagnostics(path: Path | None) -> list[dict[str, str]]:
        if path is None or not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def _build_gold(
        self,
        active_hashes: set[str],
        grouped: dict[str, list[dict[str, Any]]],
    ) -> tuple[int, bool, int, int]:
        products: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
        ingredient_rows = 0
        active_pricing_sources = 0
        financial_records: list[dict[str, Any]] = []
        market_records: list[dict[str, Any]] = []
        document_catalog: list[dict[str, Any]] = []
        quality_issues: list[dict[str, str]] = []

        for source_hash in sorted(active_hashes):
            descriptor = grouped[source_hash][0]
            kind = descriptor["kind"]
            domain = _source_domain(descriptor["source_path"], kind)
            source_paths = sorted(item["source_path"] for item in grouped[source_hash])
            metadata_path = self.paths.bronze / "metadata" / f"{source_hash}.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            except (json.JSONDecodeError, OSError):
                metadata = {}
            source_zone = descriptor["source_path"].split("/", 1)[0]
            document_catalog.append(
                {
                    "source_hash": source_hash,
                    "source_paths": source_paths,
                    "source_name": descriptor["source_name"],
                    "source_zone": source_zone,
                    "kind": kind,
                    "domain": domain,
                    "mime_type": metadata.get("mime_type"),
                    "size_bytes": descriptor["size_bytes"],
                    "page_count": metadata.get("page_count"),
                    "dimensions": metadata.get("dimensions"),
                    "sheets": metadata.get("sheets"),
                    "access_level": "restricted" if source_zone == "legal" else "internal",
                    "review_required": source_zone in {"legal", "receipts"},
                }
            )
            if kind != "xlsx":
                continue

            artifacts = self._artifact_paths(descriptor, domain)
            for diagnostic in self._read_diagnostics(artifacts.get("diagnostics")):
                quality_issues.append(
                    {
                        "source_hash": source_hash,
                        "source_path": descriptor["source_path"],
                        "source_sheet": str(diagnostic.get("sheet", "*")),
                        "category": "transformation",
                        "reason": str(diagnostic.get("reason", "unknown")),
                    }
                )
            records = self._read_jsonl(artifacts["silver"])
            for record in records:
                for flag in record.get("quality_flags", []):
                    quality_issues.append(
                        {
                            "source_hash": source_hash,
                            "source_path": descriptor["source_path"],
                            "source_sheet": str(record.get("source_sheet", "*")),
                            "category": "record",
                            "reason": str(flag),
                        }
                    )

            if domain == "pricing" and _is_active_pricing_source(descriptor["source_path"]):
                active_pricing_sources += 1
                for record in records:
                    if record["record_type"] == "pricing_ingredient":
                        ingredient_rows += 1
                        continue
                    if record["record_type"] != "pricing_product":
                        continue
                    # Newer source wins; hash/sheet make ties deterministic.
                    ranking = (descriptor["modified_at"], f"{source_hash}:{record['source_sheet']}")
                    existing = products.get(record["product_id"])
                    if existing is None or ranking > existing[0]:
                        products[record["product_id"]] = (ranking, record)
            elif domain == "finance":
                financial_records.extend(records)
            elif domain == "market":
                market_records.extend(records)

        catalog = [
            {
                "product_id": record["product_id"],
                "product_name": record["product_name"],
                "category": record["category"],
                "suggested_price": record["suggested_price"],
                "suggested_price_includes_fees": record["suggested_price_includes_fees"],
                "recipe_cost": record["recipe_cost"],
                "gross_margin_percent": record["gross_margin_percent"],
                "ingredient_count": record["ingredient_count"],
                "recipe": record["recipe"],
                "source_hash": record["source_hash"],
                "source_path": record["source_path"],
                "source_sheet": record["source_sheet"],
            }
            for _, record in sorted(products.values(), key=lambda item: (item[1]["product_name"].lower(), item[1]["product_id"]))
        ]
        margins = [record["gross_margin_percent"] for record in catalog if record["gross_margin_percent"] is not None]
        pricing_summary = {
            "active_source_count": active_pricing_sources,
            "product_count": len(catalog),
            "ingredient_record_count": ingredient_rows,
            "average_gross_margin_percent": round(sum(margins) / len(margins), 2) if margins else None,
        }
        daily_revenue, finance_summary, expenses = self._finance_gold(financial_records)
        market_summary = self._market_gold(market_records)
        quality_payload = self._quality_payload(quality_issues)

        changed = [
            write_jsonl_if_changed(self.paths.silver / "document_catalog.jsonl", sorted(document_catalog, key=lambda item: item["source_hash"])),
            write_jsonl_if_changed(self.paths.gold / "pricing_catalog.jsonl", catalog),
            write_json_if_changed(self.paths.gold / "pricing_summary.json", pricing_summary),
            write_jsonl_if_changed(self.paths.gold / "daily_revenue.jsonl", daily_revenue),
            write_jsonl_if_changed(self.paths.gold / "expenses.jsonl", expenses),
            write_json_if_changed(self.paths.gold / "finance_summary.json", finance_summary),
            write_json_if_changed(self.paths.gold / "market_summary.json", market_summary),
            write_json_if_changed(self.paths.gold / "data_quality.json", quality_payload),
        ]
        return len(catalog), any(changed), len(document_catalog), len(quality_issues)

    @staticmethod
    def _finance_gold(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        daily: dict[str, dict[str, Any]] = {}
        expenses: list[dict[str, Any]] = []
        revenue_total = 0.0
        expense_total = 0.0
        payment_count = 0

        for record in records:
            if record.get("record_type") == "revenue_payment":
                business_date = str(record.get("business_date", ""))
                amount = float(record.get("amount") or 0)
                channel = str(record.get("payment_channel") or "Unspecified")
                if not business_date or amount <= 0:
                    continue
                day = daily.setdefault(
                    business_date,
                    {"business_date": business_date, "total_revenue": 0.0, "payment_channels": {}, "source_record_count": 0},
                )
                day["total_revenue"] += amount
                day["payment_channels"][channel] = day["payment_channels"].get(channel, 0.0) + amount
                day["source_record_count"] += 1
                revenue_total += amount
                payment_count += 1
            elif record.get("record_type") == "expense":
                amount = float(record.get("amount") or 0)
                if amount <= 0:
                    continue
                expenses.append(record)
                expense_total += amount

        daily_rows = [
            {
                "business_date": day["business_date"],
                "total_revenue": round(day["total_revenue"], 2),
                "payment_channels": [
                    {"channel": channel, "amount": round(amount, 2)}
                    for channel, amount in sorted(day["payment_channels"].items())
                ],
                "source_record_count": day["source_record_count"],
            }
            for _, day in sorted(daily.items())
        ]
        expense_rows = sorted(
            expenses,
            key=lambda record: (
                str(record.get("business_date", "")),
                str(record.get("source_hash", "")),
                int(record.get("source_row") or 0),
            ),
        )
        summary = {
            "revenue_total": round(revenue_total, 2),
            "expense_total": round(expense_total, 2),
            "net_cash_flow": round(revenue_total - expense_total, 2),
            "revenue_day_count": len(daily_rows),
            "revenue_payment_count": payment_count,
            "expense_count": len(expense_rows),
        }
        return daily_rows, summary, expense_rows

    @staticmethod
    def _market_gold(records: list[dict[str, Any]]) -> dict[str, Any]:
        by_social: dict[str, int] = defaultdict(int)
        by_product: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("record_type") != "market_observation":
                continue
            social = str(record.get("social_class") or "Unknown")
            product = str(record.get("product_normalized") or "unknown")
            product_raw = str(record.get("product_raw") or "Unknown")
            placement = int(record.get("placement") or 0)
            by_social[social] += 1
            if placement > 0:
                entry = by_product.setdefault(product, {"placements": [], "raw_variants": set()})
                entry["placements"].append(placement)
                entry["raw_variants"].add(product_raw)
        products = [
            {
                "product_normalized": product,
                "product_variants": sorted(entry["raw_variants"], key=str.lower),
                "observation_count": len(entry["placements"]),
                "average_placement": round(sum(entry["placements"]) / len(entry["placements"]), 2),
                "best_placement": min(entry["placements"]),
            }
            for product, entry in sorted(by_product.items(), key=lambda item: (-len(item[1]["placements"]), item[0]))
        ]
        return {
            "observation_count": sum(by_social.values()),
            "social_class_counts": {name: by_social[name] for name in sorted(by_social)},
            "products": products,
        }

    @staticmethod
    def _quality_payload(issues: list[dict[str, str]]) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for issue in issues:
            counts[f"{issue['category']}:{issue['reason']}"] += 1
        return {
            "issue_count": len(issues),
            "issue_counts": {key: counts[key] for key in sorted(counts)},
            "issues": sorted(
                issues,
                key=lambda item: (item["source_path"], item["source_sheet"], item["category"], item["reason"]),
            ),
        }

    def _load_manifest(self) -> dict[str, Any]:
        path = self.paths.state / "manifest.json"
        if not path.exists():
            return {"schema_version": MANIFEST_VERSION, "files": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"schema_version": MANIFEST_VERSION, "files": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
            return {"schema_version": MANIFEST_VERSION, "files": {}}
        return payload

    def _write_run_metadata(self, result: RunResult, files: list[dict[str, Any]]) -> None:
        payload = {
            "pipeline": "acai-erp-etl",
            "run": result.as_dict(),
            "files": files,
        }
        write_json_if_changed(self.paths.state / "runs" / f"{result.run_id}.json", payload)
        write_json_if_changed(self.paths.state / "latest_run.json", payload)


def run_etl(raw_dir: Path | None = None, data_dir: Path | None = None) -> RunResult:
    """Run the default pipeline, optionally overriding the raw/data roots."""

    project_root = Path(__file__).resolve().parents[1]
    data_dir = Path(data_dir) if data_dir is not None else project_root / "data"
    paths = ETLPaths.from_data_dir(data_dir)
    if raw_dir is not None:
        paths = ETLPaths(
            raw=Path(raw_dir),
            bronze=paths.bronze,
            silver=paths.silver,
            gold=paths.gold,
            state=paths.state,
        )
    return ETLPipeline(paths).run()
