"""Raw file discovery and metadata extraction for the bronze layer."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import struct
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = {".xlsx", ".pdf", *IMAGE_EXTENSIONS}


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the content address of a file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def detect_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return "xlsx"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return None


def discover_files(raw_dir: Path) -> list[Path]:
    """Find supported source files in a stable order, ignoring placeholders."""

    if not raw_dir.exists():
        return []
    return sorted(
        (
            path
            for path in raw_dir.rglob("*")
            if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: path.as_posix().lower(),
    )


def json_value(value: Any) -> Any:
    """Make workbook cells and metadata JSON-safe without losing useful values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def workbook_rows(path: Path, source_hash: str, source_path: str) -> Iterable[dict[str, Any]]:
    """Yield non-empty worksheet rows as immutable bronze records."""

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        for worksheet in workbook.worksheets:
            for row_number, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                cells = [json_value(value) for value in row]
                if any(value is not None and value != "" for value in cells):
                    yield {
                        "source_hash": source_hash,
                        "source_path": source_path,
                        "sheet": worksheet.title,
                        "row_number": row_number,
                        "cells": cells,
                    }
    finally:
        workbook.close()


def extract_metadata(path: Path, source_hash: str, source_path: str, kind: str) -> dict[str, Any]:
    """Extract lightweight metadata suitable for every supported document type."""

    metadata: dict[str, Any] = {
        "source_hash": source_hash,
        "source_path": source_path,
        "source_name": path.name,
        "kind": kind,
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "size_bytes": path.stat().st_size,
    }
    if kind == "xlsx":
        metadata["sheets"] = workbook_metadata(path)
    elif kind == "pdf":
        metadata["page_count"] = pdf_page_count(path)
    elif kind == "image":
        metadata["dimensions"] = image_dimensions(path)
    return metadata


def workbook_metadata(path: Path) -> list[dict[str, Any]]:
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        return [
            {
                "name": worksheet.title,
                "max_row": worksheet.max_row,
                "max_column": worksheet.max_column,
            }
            for worksheet in workbook.worksheets
        ]
    finally:
        workbook.close()


def pdf_page_count(path: Path) -> int | None:
    """Count page objects without a heavyweight PDF dependency.

    This is intentionally metadata-only. Encrypted or malformed PDFs return
    ``None`` rather than blocking the complete ETL run.
    """

    try:
        with path.open("rb") as stream:
            content = stream.read()
        return len(re.findall(rb"/Type\s*/Page\b", content)) or None
    except OSError:
        return None


def image_dimensions(path: Path) -> dict[str, int] | None:
    """Read dimensions from common image headers without adding Pillow."""

    try:
        with path.open("rb") as stream:
            header = stream.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                width, height = struct.unpack(">II", header[16:24])
                return {"width": width, "height": height}
            if header[:6] in (b"GIF87a", b"GIF89a") and len(header) >= 10:
                width, height = struct.unpack("<HH", header[6:10])
                return {"width": width, "height": height}
            if header.startswith(b"BM") and len(header) >= 26:
                width, height = struct.unpack("<ii", header[18:26])
                return {"width": abs(width), "height": abs(height)}
            if header.startswith(b"\xff\xd8"):
                return jpeg_dimensions(stream, header)
    except OSError:
        return None
    return None


def jpeg_dimensions(stream: Any, initial_header: bytes) -> dict[str, int] | None:
    """Read JPEG Start Of Frame dimensions from a file stream."""

    stream.seek(2)
    while True:
        marker_start = stream.read(1)
        if not marker_start:
            return None
        if marker_start != b"\xff":
            continue
        marker = stream.read(1)
        while marker == b"\xff":
            marker = stream.read(1)
        if not marker or marker in {b"\xd8", b"\xd9"}:
            continue
        length_bytes = stream.read(2)
        if len(length_bytes) != 2:
            return None
        segment_length = struct.unpack(">H", length_bytes)[0]
        if segment_length < 2:
            return None
        marker_value = marker[0]
        if 0xC0 <= marker_value <= 0xC3 or 0xC5 <= marker_value <= 0xC7 or 0xC9 <= marker_value <= 0xCB or 0xCD <= marker_value <= 0xCF:
            frame = stream.read(5)
            if len(frame) != 5:
                return None
            height, width = struct.unpack(">HH", frame[1:5])
            return {"width": width, "height": height}
        stream.seek(segment_length - 2, 1)


def copy_content_addressed(source: Path, destination: Path) -> bool:
    """Copy a raw file once; the destination name is its SHA-256 hash."""

    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)
    return True


def write_json_if_changed(path: Path, payload: Any) -> bool:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return write_text_if_changed(path, serialized)


def write_jsonl_if_changed(path: Path, records: Iterable[dict[str, Any]]) -> bool:
    serialized = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )
    return write_text_if_changed(path, serialized)


def write_text_if_changed(path: Path, content: str) -> bool:
    """Atomically write only when deterministic content is different."""

    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return True
