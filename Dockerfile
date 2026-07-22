# UNVERIFIED — written without a local Docker runtime to build/test against.
# Before trusting this, actually build it and confirm cfgrib/eccodes, rasterio,
# and cdo all work: `docker build -t eclipse-weather . && docker run --rm
# eclipse-weather python -c "import cfgrib, rasterio; import subprocess;
# subprocess.run(['cdo', '--version'])"`

FROM python:3.12-slim

# eccodes (GRIB decoding, via cfgrib), gdal (rasterio's AEMET GeoTIFF path —
# modern rasterio wheels usually bundle GDAL themselves, but installing the
# system lib too is cheap insurance), cdo (ICON Global icosahedral remap).
# Package names below are a best-effort guess (Debian bookworm) — verify on
# an actual build; libeccodes-dev in particular may need a newer bookworm-
# backports or the eccodes tarball if the apt version is too old for cfgrib.
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

# data/raw and data/points.parquet must be a mounted volume in production —
# a missed run is unrecoverable (CLAUDE.md hard constraint #1), so the
# archive must outlive any single container's lifecycle.
VOLUME ["/app/data"]

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "src.scheduler.run"]
