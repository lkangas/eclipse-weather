# CLAUDE.md — Eclipse Cloud Forecast Tool

Multi-model cloud forecast comparison for the total solar eclipse over Spain,
**2026-08-12, totality ≈ 18:25–18:33 UTC**. Working eclipse time
`ECLIPSE_T = 2026-08-12T18:30:00Z` (env var — never hardcode; UI must work with any T).

The tool archives every forecast run from ~12 NWP models — the full forecast
range each run publishes, not just eclipse-day hours (unified 2026-07-23; see
TASKS.md's archiver-consolidation note) — extracts low/mid/high cloud for the
eclipse valid hours over Iberia into `points.parquet`, and visualizes (a) when
each model first covers T, (b) how each model's eclipse forecast evolves
run-over-run (fixed valid time, slider over init times — "d(Prog)/dt" view),
plus (c) a general-purpose "latest run of every model" explorer (Tool 1) that
isn't tied to the eclipse date at all.

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
    src/scheduler/          # in-process loop (Docker entrypoint) - reads models.yaml,
                            # computes due fetches from cycles + publication_lag + margin
    data/raw/{model}/{initYYYYMMDDHH}/   # full-range GRIB2/GeoTIFF, native/global
                                          # extent (NOT cropped to Iberia at fetch
                                          # time - see Stack & conventions' disk
                                          # footprint note)
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
  xarray, wgrib2 (inspection), cdo (ICON Global remap — T04 confirmed
  icosahedral-only, no regular-lat-lon variant exists, so this is mandatory,
  not conditional), rasterio (AEMET GeoTIFF), polars + Parquet,
  matplotlib (static SVG first) then plotly.
- Fetch politeness: exponential backoff, ≤4 concurrent per host, honor
  Open-Meteo non-commercial limits. Every scheduled fetch pings a
  healthcheck URL (dead archiver during Aug 5–12 is the worst failure mode).
- Deployment target: an always-on Linux box, archiver runs in **Docker**
  (decided 2026-07-22 — a single long-running container with an in-process
  scheduler loop, not systemd timers; see `src/scheduler/run.py` and the
  `Dockerfile`/`docker-compose.yml`, verified 2026-07-23 against real live
  endpoints). `data/` must be a mounted volume, never ephemeral container
  storage — a missed run is unrecoverable. Own directory, own port, isolated
  from any other services on the box. Box chosen 2026-07-22 — see private ops
  notes, not this repo.
  **Disk footprint (measured, not estimated, 2026-07-23):** since the
  2026-07-23 archiver consolidation, `data/raw/` holds every fetcher's FULL
  forecast range (not just 3 eclipse-hour steps) — this is much larger than
  originally assumed and NOT cropped to the Iberia bbox at fetch time (that
  only happens downstream in `src/extract/`/`src/viz/`; T21's crop-before-
  archive step was never built and archiving full-range makes it moot
  anyway). Real numbers from 2 real runs across 10 gridded models: **48 GB**
  total, dominated by full-ensemble products — `aifs_ens` alone is **~16 GB
  per run** (50 members × 4 cloud fields × ~60 steps), `ecmwf_ens` **~1.5–2.3
  GB per run**; deterministic single-message models (gfs, icon_eu/global,
  arome/arpege) are a few hundred MB to ~1 GB per run. At aifs_ens's own
  4-cycles/day cadence that's ~64 GB/day for that one model if every run is
  kept — comfortably fine on this desktop (906 GB free as of 2026-07-23,
  per explicit "disk isn't constrained here" dev-phase direction) but a real
  constraint for production's much smaller disk (see T25/private ops notes)
  and a hard input to rollout step 4's fetch→render→discard design (a single
  in-flight aifs_ens run's raw files alone may not fit comfortably alongside
  everything else on a small production disk even transiently, before
  discard).

## Human-in-the-loop items (Claude Code: flag, don't attempt)

- ~~AEMET OpenData API key registration (T07a)~~ — done 2026-07-22.
- **Météo-France API key registration** at portail-api.meteofrance.fr
  (new, found by T05) — the AWS bucket this project originally assumed as
  the fetch source was permanently shut down 2024-12-09; the real API path
  needs a free key/OAuth registration, same category as AEMET. Needed before
  `arpege_europe`/`arome_france` fetchers can use the `mf_api` path (the
  unauthenticated data.gouv.fr mirror is a possible alternative — its
  automation terms are unconfirmed, see T05/T10 notes in models.yaml).
- Met Office DataHub key — optional path, only if T06 shows Open-Meteo
  insufficient.
- Final site list sign-off — the totality polygon check itself is done
  (T33, 2026-07-23, `config/totality_path.json`; all 7 current candidates
  fall inside the band with 68-184km margin), but the actual go/no-go
  shortlist call is still a human decision, not this tool's.
- ~~Choice of deployment box~~ — decided 2026-07-22 (see private ops notes,
  not this repo). Healthcheck service account still open.

## Simulated-eclipse testing (build UI before real data exists)

Two modes, switched purely by env:
- **Time-shift:** set `ECLIPSE_T` to a past 18:30 UTC and backfill full
  multi-model run history from Open-Meteo Previous Runs API (T16). Full 16-day
  slider dataset available immediately.
- **Live-forward:** set `ECLIPSE_T` ≈ now+4 d and run the real archiver against
  it — end-to-end test of every fetch/parse path before Jul 27 (T15).
