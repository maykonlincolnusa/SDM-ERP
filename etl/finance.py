"""Financial workbook transformations that exclude derived and personal sheets."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .transform import as_number, normalized_text


MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def normalize_financial_workbook(
    path: Path,
    source_hash: str,
    source_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract only transactional revenue and expenses from a financial workbook.

    Summary, payroll placeholder and personal sheets are deliberately omitted. The
    pipeline creates fact rows only when a real business date and positive value
    are present.
    """

    workbook = load_workbook(path, data_only=True, read_only=True)
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    try:
        for sheet in workbook.worksheets:
            if "pessoal" in normalized_text(sheet.title) or "funcion" in normalized_text(sheet.title):
                diagnostics.append({"sheet": sheet.title, "reason": "excluded_non_business_sheet"})
                continue
            rows = list(sheet.iter_rows(values_only=True))
            revenue, revenue_diagnostics = _revenue_rows(rows, source_hash, source_path, sheet.title)
            expenses, expense_diagnostics = _expense_rows(rows, source_hash, source_path, sheet.title)
            records.extend(revenue)
            records.extend(expenses)
            diagnostics.extend(revenue_diagnostics)
            diagnostics.extend(expense_diagnostics)
    finally:
        workbook.close()
    if not records:
        diagnostics.append({"sheet": "*", "reason": "no_transactional_rows_found"})
    return records, diagnostics


def _revenue_rows(
    rows: list[tuple[Any, ...]], source_hash: str, source_path: str, sheet_name: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    header_index, _ = _find_header(rows, {"data", "dinheiro"})
    if header_index is None:
        return [], []
    field_by_column = {column: normalized_text(value) for column, value in enumerate(rows[header_index])}
    money_column = _column_containing(field_by_column, "dinheiro")
    date_column = _date_column_for_revenue(field_by_column, money_column)
    if date_column is None:
        return [], [{"sheet": sheet_name, "reason": "revenue_date_column_not_found"}]
    payment_aliases = {
        "dinheiro": "Dinheiro",
        "cartao": "Cartão",
        "pix": "Pix",
        "ifood": "iFood",
        "99": "Delivery",
    }
    total_column = _column_containing(field_by_column, "total")
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        business_date = _as_date(row[date_column] if date_column < len(row) else None, sheet_name)
        if not business_date:
            continue
        payments: list[tuple[str, float]] = []
        for column, header in field_by_column.items():
            if column >= len(row):
                continue
            channel = next((label for token, label in payment_aliases.items() if token in header), None)
            amount = as_number(row[column])
            if channel and amount and amount > 0:
                payments.append((channel, amount))
        if not payments:
            continue
        total = as_number(row[total_column]) if total_column is not None and total_column < len(row) else None
        payment_total = round(sum(amount for _, amount in payments), 2)
        for channel, amount in payments:
            records.append(
                {
                    "record_type": "revenue_payment",
                    "source_hash": source_hash,
                    "source_path": source_path,
                    "source_sheet": sheet_name,
                    "source_row": row_number,
                    "business_date": business_date,
                    "payment_channel": channel,
                    "amount": round(amount, 2),
                    "day_total_reported": round(total, 2) if total is not None else payment_total,
                    "day_total_calculated": payment_total,
                    "quality_flags": ["total_mismatch"] if total is not None and round(total, 2) != payment_total else [],
                }
            )
    return records, []


def _expense_rows(
    rows: list[tuple[Any, ...]], source_hash: str, source_path: str, sheet_name: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    header_index, columns = _find_header(rows, {"data", "descricao", "valor"})
    if header_index is None:
        return [], []
    date_column = columns.get("data")
    description_column = columns.get("descricao")
    amount_column = columns.get("valor")
    if None in (date_column, description_column, amount_column):
        return [], [{"sheet": sheet_name, "reason": "expense_columns_not_found"}]
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        business_date = _as_date(row[date_column] if date_column < len(row) else None, sheet_name)
        description = row[description_column] if description_column < len(row) else None
        amount = as_number(row[amount_column] if amount_column < len(row) else None)
        if not business_date or not isinstance(description, str) or not description.strip() or not amount or amount <= 0:
            continue
        records.append(
            {
                "record_type": "expense",
                "source_hash": source_hash,
                "source_path": source_path,
                "source_sheet": sheet_name,
                "source_row": row_number,
                "business_date": business_date,
                "description": description.strip(),
                "amount": round(amount, 2),
                "quality_flags": [],
            }
        )
    return records, []


def _find_header(rows: list[tuple[Any, ...]], required: set[str]) -> tuple[int | None, dict[str, int]]:
    for row_index, row in enumerate(rows[:15]):
        columns: dict[str, int] = {}
        labels = [normalized_text(value) for value in row]
        for required_label in required:
            match = next((index for index, label in enumerate(labels) if required_label in label), None)
            if match is not None:
                columns[required_label] = match
        if len(columns) == len(required):
            return row_index, columns
    return None, {}


def _column_containing(columns: dict[int, str], token: str) -> int | None:
    return next((column for column, header in columns.items() if token in header), None)


def _date_column_for_revenue(columns: dict[int, str], money_column: int | None) -> int | None:
    """Find the date header closest to the revenue table's money column.

    The source workbook places expenses and sales side by side, both with a
    ``DATA`` header. Choosing the first date column would silently discard the
    sales rows because their date sits next to ``DINHEIRO`` on the right.
    """

    candidates = [column for column, header in columns.items() if "data" in header]
    if not candidates:
        return None
    if money_column is None:
        return candidates[0]
    preceding = [column for column in candidates if column < money_column]
    if preceding:
        return max(preceding)
    return min(candidates, key=lambda column: abs(column - money_column))


def _as_date(value: Any, sheet_name: str) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        day = int(value)
        period = _period_from_sheet_name(sheet_name)
        if period and 1 <= day <= 31:
            year, month = period
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None
    return None


def _period_from_sheet_name(sheet_name: str) -> tuple[int, int] | None:
    label = normalized_text(sheet_name)
    month = next((number for name, number in MONTHS.items() if name in label), None)
    year_match = re.search(r"\b(20\d{2}|\d{2})\b", label)
    if month is None or year_match is None:
        return None
    year_text = year_match.group(1)
    year = int(year_text) if len(year_text) == 4 else 2000 + int(year_text)
    return year, month
