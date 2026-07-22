# CLAUDE.md — Eclipse Cloud Forecast Tool

Multi-model cloud forecast comparison for the total solar eclipse over Spain,
**2026-08-12, totality ≈ 18:25–18:33 UTC**. Working eclipse time
`ECLIPSE_T = 2026-08-12T18:30:00Z` (env var — never hardcode; UI must work with any T).

The tool archives every forecast run from ~12 NWP models, extracts low/mid/high
cloud for the eclipse valid hours over Iberia, and visualizes (a) when each model
first covers T, (b) how each model's eclipse forecast evolves run-over-run
(fixed valid time, slider over init times — "d(Prog)/dt" view).

## Hard constraints — read before writing any code

1. **Archiver first.** DWD keeps ~24 h of runs; AEMET keeps only the latest.
   A missed run is unrecoverable and the run-evolution view is the core product.
   Nothing else matters until the archiver is reliable.
2. **`config/models.yaml` is the single source of truth** for cycles, lengths,
   steps, URLs, params, lags. Fetchers, scheduler, Gantt, and UI all read it.
   Never duplicate model metadata in code.
3. **Provenance on every row.** Cloud levels are `native` (model outputs L/M/H)
   or `derived` (computed from humidity). The flag travels with every stored
   value into every chart.
4. **UTC everywhere.** ISO-8601, init times as `YYYYMMDDTHH`. No local times in
   data or code; only the UI may render Europe/Madrid for display.
5. **Deadline-driven.** First real data: GFS 18Z run, **Jul 27** (~23 UTC on
   disk). Model onboarding order and dates are in TASKS.md — the calendar, not
   preference, sets priorities.
6. **Verified vs unverified metadata.** Entries in models.yaml carry
   `status: confirmed | verify`. Research tasks (T01–T09) resolve `verify`
   items by fetching real index/sample files, then update models.yaml and flip
   the status. Do not build fetchers against unverified URL templates.

## Repo layout (T00 scaffolds this; starter files delivered flat → move in)

    config/models.yaml      # model registry (delivered as models.yaml)
    config/sites.yaml       # candidate viewing sites (delivered as sites.yaml)
    src/fetchers/           # per-source download (byte-range GRIB, DWD bz2, GeoTIFF, JSON)
    src/extract/            # GRIB/GeoTIFF -> xarray -> bbox slice + point rows
    src/derive/             # humidity -> L/M/H cloud (for models lacking native fields)
    src/viz/                # availability gantt, run-evolution charts, maps
    src/scheduler/          # systemd timer / cron generation from models.yaml
    data/raw/{model}/{initYYYYMMDDHH}/   # Iberia-box slices (GRIB2/GeoTIFF)
    data/points.parquet     # extracted point/strip values (append-only)
    TASKS.md                # ordered work queue — work top-down, tick boxes

## Data schema — `data/points.parquet`

    model:str  run_init:ts[UTC]  member:int(-1=det)  site:str  valid:ts[UTC]
    cloud_low:f32  cloud_mid:f32  cloud_high:f32  cloud_total:f32   # percent 0-100
    provenance:str(native|derived|total_only)  fetched_at:ts[UTC]

Archive valid times: **15, 18, 21 UTC on eclipse day** (18:30 is interpolated
between 18 and next step; 15 UTC supports trend + WNW-sightline views).
Iberia bbox: **36–44° N, 10° W–5° E**.

## Domain notes that affect design

- **Low sun:** at T the sun is ~10–12° high, azimuth ~285° (WNW). Cloud along
  the sightline matters as much as overhead → each site also gets a WNW strip
  sample (see sites.yaml: bearing 285°, 100 km, every 25 km).
- **Eclipse radiation:** models differ in whether their radiation scheme
  simulates the obscuration (affects low cloud/convection at 17–19 UTC).
  Findings from T09 get annotated per model in models.yaml `notes`.
- August-evening cumulus over Iberia decays toward sunset; disagreement between
  models on decay timing is expected and is part of what the tool shows.

## Stack & conventions

- Python 3.12, **uv** (`uv sync`, `uv run <script>`); ruff for lint.
- Libraries: herbie-data (idx byte-range subsetting — never download full GRIBs;
  a L/M/H Iberia slice is a few MB vs 500 MB), ecmwf-opendata, cfgrib + eccodes,
  xarray, wgrib2 (inspection), cdo (only if ICON global needs icosahedral
  remap — T04 decides), rasterio (AEMET GeoTIFF), polars + Parquet,
  matplotlib (static SVG first) then plotly.
- Fetch politeness: exponential backoff, ≤4 concurrent per host, honor
  Open-Meteo non-commercial limits. Every scheduled fetch pings a
  healthcheck URL (dead archiver during Aug 5–12 is the worst failure mode).
- Deployment target: an always-on Linux box with systemd timers (timer units
  generated from models.yaml: fire at init + publication_lag + margin).

## Human-in-the-loop items (Claude Code: flag, don't attempt)

- AEMET OpenData API key registration (T07).
- Met Office DataHub key — optional path, only if T06 shows Open-Meteo
  insufficient.
- Final site list sign-off after totality polygon check (T33).
- Choice of deployment box + healthcheck service account.

## Simulated-eclipse testing (build UI before real data exists)

Two modes, switched purely by env:
- **Time-shift:** set `ECLIPSE_T` to a past 18:30 UTC and backfill full
  multi-model run history from Open-Meteo Previous Runs API (T16). Full 16-day
  slider dataset available immediately.
- **Live-forward:** set `ECLIPSE_T` ≈ now+4 d and run the real archiver against
  it — end-to-end test of every fetch/parse path before Jul 27 (T15).
