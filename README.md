# eclipse-weather

Multi-model cloud forecast comparison for the 2026-08-12 total solar eclipse
over Spain (totality ≈ 18:25–18:33 UTC). See [CLAUDE.md](CLAUDE.md) for the
full brief and hard constraints, and [TASKS.md](TASKS.md) for the work queue.

## Layout

- `config/models.yaml` — NWP model registry (cycles, lengths, steps, URLs,
  params, lags): the single source of truth read by fetchers, scheduler,
  Gantt, and UI.
- `config/sites.yaml` — candidate viewing sites, including WNW sightline
  strip samples.
- `src/fetchers/` — per-source download (byte-range GRIB, DWD bz2, GeoTIFF,
  JSON), one module per `fetch:` value in `models.yaml`.
- `src/extract/` — GRIB2/GeoTIFF → xarray → Iberia bbox slice + point rows.
- `src/derive/` — humidity → low/mid/high cloud fraction, for models without
  native cloud-level fields.
- `src/viz/` — availability Gantt, run-evolution charts, maps.
- `src/scheduler/` — systemd timer generation from `models.yaml`.
- `data/raw/{model}/{initYYYYMMDDHH}/` — archived Iberia-box slices
  (gitignored — generated, not source).
- `data/points.parquet` — extracted point/strip values, append-only
  (gitignored).
- `docs/model-fetch-plan.md` — model fetch plan: phase timeline, master
  model table, per-model notes.

## Setup

```bash
uv sync
```

## Status

Pre-data scaffold. First real data lands with the GFS 18Z run, 2026-07-27.
