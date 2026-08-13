"""Data structures shared by the ETL pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ETLPaths:
    """Filesystem layout for the local medallion data lake."""

    raw: Path
    bronze: Path
    silver: Path
    gold: Path
    state: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "ETLPaths":
        data_dir = Path(data_dir)
        return cls(
            raw=data_dir / "raw",
            bronze=data_dir / "bronze",
            silver=data_dir / "silver",
            gold=data_dir / "gold",
            state=data_dir / "runtime" / "etl",
        )

    def ensure_directories(self) -> None:
        for directory in (
            self.raw,
            self.bronze / "files",
            self.bronze / "metadata",
            self.bronze / "xlsx",
            self.silver / "pricing",
            self.silver / "finance",
            self.silver / "market",
            self.silver / "workbook",
            self.gold,
            self.state / "runs",
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass
class RunResult:
    """Serializable outcome of one ETL execution."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    discovered_files: int = 0
    unique_files: int = 0
    processed_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    pricing_records: int = 0
    financial_records: int = 0
    market_records: int = 0
    document_records: int = 0
    quality_issues: int = 0
    gold_products: int = 0
    gold_updated: bool = False
    artifacts_written: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
