"""Run the local file ETL pipeline.

Usage:
    python scripts/run_etl.py
    python scripts/run_etl.py --raw-dir data/raw --data-dir data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from etl import run_etl  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Executa o ETL de documentos e planilhas da loja.")
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / "data" / "raw", help="Diretorio de arquivos fonte")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help="Diretorio raiz das camadas de dados")
    args = parser.parse_args()

    result = run_etl(raw_dir=args.raw_dir, data_dir=args.data_dir)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    if result.failed_files:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
