from collections.abc import Callable
from datetime import datetime

from src.fetchers.base import FetchResult

FETCHERS: dict[str, Callable[[str, dict, datetime], FetchResult]] = {}


def register(fetch_key: str):
    """Decorator: registers a fetch(model_name, model_config, run_init) -> FetchResult
    function under models.yaml's `fetch:` value (e.g. "herbie", "http_bz2")."""

    def deco(fn):
        FETCHERS[fetch_key] = fn
        return fn

    return deco


def get_fetcher(fetch_key: str) -> Callable[[str, dict, datetime], FetchResult]:
    if fetch_key not in FETCHERS:
        raise KeyError(
            f"No fetcher registered for fetch type '{fetch_key}'. "
            f"Registered: {sorted(FETCHERS)}"
        )
    return FETCHERS[fetch_key]
