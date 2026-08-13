"""ETL utilities for the Acai ERP data platform.

The package is deliberately independent from the API and Firebase repository.
It turns files placed in ``data/raw`` into reproducible bronze, silver and gold
artifacts that can be consumed by analytics or future integrations.
"""

from .models import ETLPaths, RunResult
from .pipeline import ETLPipeline, run_etl

__all__ = ["ETLPaths", "ETLPipeline", "RunResult", "run_etl"]
