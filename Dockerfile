# Verified 2026-07-22 in WSL Ubuntu Docker: builds cleanly, and
# cfgrib/rasterio/eccodes import + `cdo --version` all work inside the
# container. Turns out eccodes' PyPI wheel (eccodeslib) bundles its own
# shared library — the apt eccodes packages below aren't actually load-
# bearing for that import, but gdal/cdo are real system dependencies.

FROM python:3.12-slim

# gdal (rasterio's AEMET GeoTIFF path), cdo (ICON Global icosahedral remap).
# libeccodes0/-dev kept for now even though eccodeslib's bundled lib already
# covers the Python import path, in case any non-Python tooling wants it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes0 libeccodes-dev \
    gdal-bin libgdal-dev \
    cdo \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY config/ config/
COPY src/ src/
COPY scripts/ scripts/

# data/raw and data/points.parquet must be a mounted volume in production —
# a missed run is unrecoverable (CLAUDE.md hard constraint #1), so the
# archive must outlive any single container's lifecycle.
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

# Invoke the venv's own python directly — the venv was already built during
# the image build, so going through `uv run` again at container start just
# adds a needless resolve/sync step (and a network dependency) on every start.
CMD ["/app/.venv/bin/python", "-m", "src.scheduler.run"]
