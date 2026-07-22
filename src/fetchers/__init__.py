# Importing each fetcher module registers it (via @register in
# src/fetchers/registry.py) against its models.yaml `fetch:` key.
# scheduler/run.py does `from src import fetchers` purely for this side effect.
from src.fetchers import (  # noqa: F401
    aemet_geotiff_fetcher,
    dwd_bz2_fetcher,
    ecmwf_opendata_fetcher,
    herbie_fetcher,
    meteofrance_fetcher,
    open_meteo_fetcher,
)
