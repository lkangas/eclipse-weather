from collections.abc import Callable
from datetime import datetime

from src.extract.base import PointRow

# Keyed by models.yaml's `fetch:` value, same convention as src/fetchers/registry.py
# - the raw file format an extractor reads correlates with how it was fetched.
# Signature: extract(model_name, model_config, run_init) -> list[PointRow]
EXTRACTORS: dict[str, Callable[[str, dict, datetime], list[PointRow]]] = {}


def register(fetch_key: str):
    def deco(fn):
        EXTRACTORS[fetch_key] = fn
        return fn

    return deco


def get_extractor(fetch_key: str) -> Callable[[str, dict, datetime], list[PointRow]]:
    if fetch_key not in EXTRACTORS:
        raise KeyError(
            f"No extractor registered for fetch type '{fetch_key}'. "
            f"Registered: {sorted(EXTRACTORS)}"
        )
    return EXTRACTORS[fetch_key]
