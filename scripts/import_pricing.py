"""Publish reviewed pricing recipes from a workbook into the ERP store.

Usage:
    python scripts/import_pricing.py data/raw/pricing/current/pricing.xlsx
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.repositories.store import Store, build_store
from etl.extract import sha256_file
from etl.transform import normalize_pricing_workbook


def import_workbook(
    path: Path,
    storage: Store | None = None,
    *,
    allow_price_without_fees: bool = False,
) -> tuple[int, int]:
    """Import recipes using the same transformation contract as the ETL.

    Existing stock balances and minimum levels are retained. A product without
    a price that explicitly includes fees remains out of the selling catalog
    unless the caller opts in after a business review.
    """

    source_path = Path(path)
    source_hash = sha256_file(source_path)
    records, _ = normalize_pricing_workbook(source_path, source_hash, source_path.as_posix())
    store = storage or build_store(get_settings())
    ingredients: dict[str, dict[str, Any]] = {}
    products: list[dict[str, Any]] = []

    for record in records:
        if record["record_type"] == "pricing_ingredient":
            inventory_id = _inventory_id(record["ingredient_id"])
            existing = store.get("inventory", inventory_id) or {}
            ingredients[inventory_id] = {
                "id": inventory_id,
                "name": record["ingredient_name"],
                "unit": record["unit"],
                "quantity": existing.get("quantity", 0),
                "minimum_quantity": existing.get("minimum_quantity", 0),
                "unit_cost": record["unit_cost"],
                "category": existing.get("category", "Importado da precificação"),
            }
            continue
        if record["record_type"] != "pricing_product" or record["suggested_price"] is None:
            continue
        if not allow_price_without_fees and not record["suggested_price_includes_fees"]:
            continue
        existing = store.get("products", record["product_id"]) or {}
        products.append(
            {
                "id": record["product_id"],
                "name": record["product_name"],
                "category": record["category"],
                "price": record["suggested_price"],
                "recipe": [
                    {"inventory_id": _inventory_id(item["ingredient_id"]), "quantity": item["quantity"]}
                    for item in record["recipe"]
                ],
                "active": existing.get("active", True),
                **({"image": existing["image"]} if existing.get("image") else {}),
            }
        )

    for item in ingredients.values():
        store.put("inventory", item["id"], item)
    for product in products:
        store.put("products", product["id"], product)
    return len(ingredients), len(products)


def _inventory_id(ingredient_id: str) -> str:
    """Keep imported recipes compatible with the ERP inventory identifier convention."""

    return f"stock-{ingredient_id.removeprefix('ingredient-')}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Importa fichas tecnicas revisadas para o ERP.")
    parser.add_argument("workbook", type=Path, help="Caminho da planilha .xlsx em data/raw/pricing/current")
    parser.add_argument(
        "--allow-price-without-fees",
        action="store_true",
        help="Publica tambem produtos cujo valor sugerido nao informa taxas.",
    )
    args = parser.parse_args()
    inventory_count, product_count = import_workbook(
        args.workbook,
        allow_price_without_fees=args.allow_price_without_fees,
    )
    print(f"Importacao concluida: {product_count} produtos e {inventory_count} insumos atualizados.")


if __name__ == "__main__":
    main()
