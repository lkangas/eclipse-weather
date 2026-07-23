# TASKS.md — work queue

Work top-down within a phase; phases are ordered by the calendar, not preference.
Every research task (T01–T12) ends by updating `config/models.yaml`
(`status: verify` → `confirmed`, fill in any corrected values) — never build a
fetcher against an unverified URL template. Tasks marked **[human]** need the
user, not Claude Code — surface them, don't attempt.

## Phase 0 — now → Jul 24 (T-21…-19d): scaffold + research sprint

- [x] **T00** Scaffold repo layout from CLAUDE.md §"Repo layout". Init `uv`
      project, ruff config, empty modules per directory.
- [x] **T01** ECMWF open-data cloud params. Fetch one `.index` file each for
      `oper` and `enfo`, both 00Z and 12Z. Grep for `tcc`/`lcc`/`mcc`/`hcc`.
      Update `models.ecmwf_ens.cloud` and `models.ecmwf_hres.cloud`.
      *Decides whether HRES/ENS need the derived-cloud path at all.*
      **Done 2026-07-22.** tcc present every cycle (not 12Z-only); no lcc/mcc/hcc
      in classic IFS oper/enfo at any cycle (that's aifs-ens's data — see T02).
- [x] **T02** AIFS cloud params + cycle lengths. Same index-grep on
      `aifs-single` and `aifs-ens`; confirm all 4 cycles reach 360h.
      Update `models.aifs_single`, `models.aifs_ens`.
      *If lcc/mcc/hcc present: AIFS becomes the best long-range native L/M/H
      source — re-rank it above GFS in the plan doc.*
      **Done 2026-07-22.** Native lcc/mcc/hcc confirmed on both, all 4 cycles
      reach 360h. `aifs_ens` promoted from optional to recommended.
- [x] **T03** GEFS cloud-level location. Check pgrb2a vs pgrb2b family for
      LCDC/MCDC/HCDC; confirm presence/absence beyond 384h in the extended run.
      Update `models.gefs_extended.cloud`, `.source`.
      **Done 2026-07-22.** L/M/H is param TCDC + level field in pgrb2b (not
      pgrb2a, not literal LCDC/MCDC/HCDC strings); unchanged through f840.
- [x] **T04** ICON Global grid. Check opendata.dwd.de for regular-lat-lon
      single-level files vs icosahedral-only. If icosahedral-only, fetch DWD
      grid-description files and prototype a `cdo` remap; cache weights.
      Update `models.icon_global.grid`, `.source`.
      **Done 2026-07-22.** Icosahedral-only confirmed, no regular-lat-lon
      variant exists. cdo remap is mandatory; grid-description + candidate
      prebuilt weight bundles identified, not yet prototyped.
- [x] **T05** Météo-France. (a) AWS registry bucket layout for ARPEGE Europe +
      AROME France — confirm bucket name, which package (SP2/HP1/other) holds
      lcc/mcc/hcc. (b) Exact per-cycle max lengths for both models (00/06/12/18,
      and 03Z for AROME). (c) AROME France domain southern edge vs 38–40°N —
      does it reach Zaragoza/Castellón/Palma? Update `models.arpege_europe`,
      `models.arome_france` fully; update `sites.yaml` AROME-relevant notes.
      **Done 2026-07-22, with a critical correction:** the configured AWS
      bucket (`mf-nwp-models`) was permanently shut down 2024-12-09 — it only
      holds 2019 static files, zero forecast data. Real access is
      portail-api.meteofrance.fr (**new [human] item** — free API key, added
      below) or an unauthenticated data.gouv.fr mirror (automation terms
      unconfirmed). Package SP2 confirmed for cloud; cycle lengths corrected
      (both models uniform per-cycle, AROME has 8 cycles/day not 5); AROME's
      domain corrected — covers virtually all of Spain, not just NE+Balearics.
      `sites.yaml` AROME notes not yet updated for the corrected domain —
      remaining work.
- [x] **T06** UKMO. (a) Confirm cycle lengths per run hour. (b) Confirm
      Open-Meteo model id `ukmo_global_deterministic_10km` (or find correct id)
      and measure actual delay vs DataHub direct. (c) Check whether DataHub
      atmospheric orders support area-subsetting (matters for the 1GB/month
      quota). Update `models.ukmo_global.source`.
      **Done 2026-07-22.** Model id confirmed. Cycle lengths corrected —
      06Z/18Z cap at 67h, not 168h like 00Z/12Z (config previously assumed
      uniform). DataHub subsetting confirmed to exist; exact bbox-vs-region
      schema unconfirmed. Cloud provenance (native vs derived) via Open-Meteo
      still unresolved — see T08's cross-reference.
- [x] **T07** AEMET. (a) **[human]** Register an OpenData API key —
      **done 2026-07-22**, key registered and confirmed live. (c) Public
      download-endpoint cadence — **done**, no auth needed, single-sample
      cadence only. (d) Eclipse-2026 product check — **done**, none exists yet
      (AEMET says a real forecast isn't knowable until "a few days before"
      Aug 12 — re-check in early August).
      **(b) done 2026-07-22** — full field catalog hunt via the registered API,
      adversarially double-checked. Negative result: AEMET has no low/mid/high
      cloud breakdown anywhere (confirmed against their own live OpenAPI spec,
      64 endpoints, zero numeric-model category). AEMET contributes
      total-cloud-only for good — T07 is now fully closed.
- [x] **T08** Open-Meteo Previous-Runs API. Confirm host/endpoint, per-model
      history depth, and — critically — read their docs for which models'
      `cloud_cover_low/mid/high` are native vs humidity-derived. Record the
      per-model flag in `models.open_meteo.model_ids_candidates` (add a
      `provenance` field per id). *Blocks T16 (sim backfill).*
      **Done 2026-07-22, with a critical correction:** the `_previous_dayN`
      history mechanism only works for total cloud_cover, NOT for
      low/mid/high (confirmed null for every model tested) — T16's backfill
      needs the separate Single-Runs API instead for true L/M/H run history,
      which only retains ~3.5 months (fine for T16, not for deep backtests).
      Native/derived flags recorded for 5 of 6 models; UKMO's remains unclear.
- [x] **T09** Eclipse-in-radiation-scheme survey. For each model family
      (IFS, ICON, UM, GFS, AIFS): does the radiation scheme simulate the
      Aug 12 solar obscuration? Add findings to each model's `notes` in
      models.yaml. *Informational — affects how much to trust 17–19 UTC
      low-cloud/convection evolution per model.*
      **Done 2026-07-22.** Only ECMWF's IFS (hres/ens) confirmed eclipse-aware
      (since Cycle 50r1, live 2026-05-12). AIFS, ICON, UKMO, GFS/GEFS all
      expected eclipse-blind. Flag this in the viz — see `eclipse:` block note
      in models.yaml.
- [x] **T10** Retention spot-check. Re-confirm the retention numbers already
      in models.yaml (DWD ~24h, AEMET latest-only, AWS permanent, ECMWF ~4d,
      Météo-France ~14d) are still accurate. Cheap; do alongside T01–T07.
      **Done 2026-07-22, with a correction:** ECMWF is actually ~2-3 days (12
      most recent runs, ~72h), not ~4 days — tightened in models.yaml. DWD/
      AEMET/NOAA confirmed as-is. Météo-France's ~14d figure is correct but
      only for the current data.gouv.fr platform, not the dead AWS bucket
      (see T05).
- [x] **T11** *(optional)* ICON-EU-EPS: does opendata.dwd.de serve a EU
      ensemble with cloud fields? Would add a mid-range probabilistic layer
      with real L/M/H. Add as `models.icon_eu_eps` only if it checks out.
      **Done 2026-07-22 — doesn't pan out.** The product exists (40 members,
      4 cycles/day, 120h) but carries only total cloud cover, no L/M/H. Not
      added to models.yaml per its own "skip if it doesn't pan out" framing.
- [x] **T12** *(optional, low priority)* GEM (Canada) / JMA / KMA / CMA via
      Open-Meteo — quick check only if T01–T09 leave spare time. Coarse
      global models, marginal value; skip if the calendar is tight.
      **Done 2026-07-23, run for real at the user's explicit request rather
      than skipped.** 3 of 4 pan out. GEM (`gem_global`) and JMA (`jma_gsm`)
      both confirmed via a live API call (Madrid + a fog-prone Iberian
      coastal site, 10-day window): real, non-null, varying low/mid/high
      cloud. Provenance differs between them per Open-Meteo's own docs, read
      verbatim rather than assumed: GEM's low/mid/high is stated to be
      "calculated from pressure level data" which is itself explicitly
      RH/Sundqvist-approximated for GEM → `provenance: derived`. JMA's
      surface low/mid/high carries no such caveat (only its separate
      pressure-level variable does) — same doc pattern already used for
      icon_global/icon_eu (T08) → `provenance: native`. CMA (`cma_grapes_
      global`) also returned real varying data live, but Open-Meteo's own
      CMA docs page currently carries a first-party reliability warning
      ("heavily overloaded ... nearly impossible to download forecasts
      reliably"), and live testing confirmed a real-world shortfall: only
      ~4.75 days of the documented 240h/10-day forecast actually came back
      non-null, for the identical request shape that returned a full 240h
      for GEM and JMA. Added anyway (real access, real fields) but flagged
      with a `reliability_caveat` and `provenance_via_open_meteo: verify` —
      not recommended for archiver scheduling without a re-check closer to
      Aug 12. **KMA doesn't pan out**: `kma_gdps` and `kma_seamless` both
      returned HTTP 200 with every field null — not just cloud, temperature
      too — at Madrid AND at Seoul (KMA's own home turf), ruling out an
      Iberia-coverage gap. Real model id, real endpoint, no live data behind
      it right now; not added to models.yaml. All three additions use the
      existing generic `open_meteo_json` fetch path unchanged — confirmed
      `src/fetchers/open_meteo_fetcher.py`'s `fetch()` needed no code
      changes, only `config/models.yaml` (3 new `models.*` entries + the
      `models.open_meteo` candidate/provenance tables) and
      `src/extract/open_meteo_extractor.py`'s `_PROVENANCE_BY_MODEL` dict.

## Phase 0 — build against confirmed metadata

- [x] **T20** Fetcher modules (`src/fetchers/`), built + tested in parallel
      2026-07-22 against real, live endpoints (not stubs): `herbie_fetcher.py`
      (GFS, GEFS extended — control member only, see below), `ecmwf_opendata_
      fetcher.py` (ENS/HRES/AIFS single+ens), `dwd_bz2_fetcher.py` (ICON
      Global/EU), `meteofrance_fetcher.py` (ARPEGE/AROME — discovered the real
      no-auth endpoint, see models.yaml), `aemet_geotiff_fetcher.py`,
      `open_meteo_fetcher.py` (UKMO + backfill stubs for T16). Each registers
      itself against `models.yaml`'s `fetch:` key via `src/fetchers/registry.py`;
      wired up in `src/fetchers/__init__.py`. Real archived files from testing
      are already sitting in `data/raw/` (gitignored).
      **Fixed during review**: HRES fetcher was missing temperature (derive
      needs it, would have raised KeyError); a real bug in `base.py`'s
      `steps_for_run` that ignored per-cycle max forecast length (caused
      gefs_extended's short 06/12/18Z cycles to request steps hundreds of
      hours past what they publish) — both fixed and re-verified against real
      data. **Known scope limits, not yet done**: gefs_extended only fetches
      the control member (31-member fan-out is a follow-up); AEMET's GeoTIFFs
      turned out to be rendered color-map images, not raw arrays — T21 will
      need a color-ramp-inversion step, inherently lossy.
- [x] **T21** Extract modules (`src/extract/`), built + tested in parallel
      2026-07-22 against real archived data (from T20's testing), one per
      `fetch:` key, wired in `src/extract/__init__.py`:
      `grib_regular_extractor.py` (GFS, GEFS extended — handles GFS's 0-360°
      longitude vs sites.yaml's -180..180, and GEFS's all-cloud-levels-share
      -shortName-`tcc` trap per T03), `ecmwf_extractor.py` (HRES/ENS/AIFS —
      percent-scaling read from models.yaml's `units:` field, not hardcoded),
      `icon_extractor.py` (ICON-EU direct; **ICON Global's icosahedral→regular
      remap solved** via DWD's prebuilt weight bundle + one `cdo remap+crop`
      call, cached locally), `meteofrance_extractor.py` (selects the right
      step out of Météo-France's multi-hour "group" GRIB2 files),
      `aemet_extractor.py` (parses the real GeoTIFF color-ramp legend,
      nearest-RGB match to a cloud-% bin — confirmed the "transparent pixel"
      case means genuinely <10% cloud, not missing data), `open_meteo_extractor.py`.
      **`ecmwf_hres` produces two PointRows per site/valid-time** (one
      `native` with `cloud_total` only, one `derived` with L/M/H only) since
      the schema allows one provenance per row — documented in the module,
      worth knowing before querying `points.parquet` for this model.
      **Bug found + fixed during review**: the archived `ecmwf_hres` pressure
      -level files predated the T20 temperature fix and were silently stale
      (fetcher's idempotency check skips existing files) — purged and
      re-fetched; derive path now confirmed producing real rows off the
      actual archive, not just a manual test file.
      **Scheduler wiring done 2026-07-23**: `src/scheduler/run.py` now calls
      extract → `append_points` after every successful fetch, plus picks up
      any already-fetched-but-never-extracted run (idempotent via a
      `.extracted` sentinel file, mirroring `already_fetched()`'s pattern —
      without it, re-extraction on every 5-minute tick would duplicate rows).
      Verified end-to-end in Docker across 9 live models: 7238 real rows in
      `points.parquet`, zero true duplicates.
      **Not done**: WNW-strip sampling (T24, next); AROME's real cadence is
      hourly through 102h per real data, though `models.yaml`'s `steps:` only
      asks for 3-hourly beyond 48h (harmless, just leaves resolution unused).
- [x] **T22** Derived-cloud module (`src/derive/humidity_to_cloud.py`), built
      + calibrated 2026-07-22: q,t → RH (Murphy & Koop, both water/ice
      formulas) → cloud fraction (Sundqvist 1989) → low/mid/high via max-
      overlap. Acceptance test run against a real, current GFS run (native
      L/M/H vs. derived-from-GFS-humidity): mean abs diff ~3-4.5pp per band,
      correlation 0.31 (low) / 0.77 (mid) / 0.71 (high) — low-band fit is the
      weakest, likely because only 3 discrete pressure levels under-resolve
      GFS's own boundary-layer scheme; worth another look once real HRES data
      is flowing. Reproducible via `scripts/calibrate_humidity_to_cloud.py`.
      Critical-RH constants are a single-sample tune — re-run the calibration
      once more real archived runs exist, don't trust long-term as-is.
- [x] **T23** Scheduler (`src/scheduler/`). **Redesigned 2026-07-22**: the
      deployment target changed to Docker, so this is an in-process scheduling
      loop (`src/scheduler/run.py`, the container's entrypoint) rather than
      generated systemd timer units — reads `models.yaml` cycles +
      `publication_lag_h` + margin, dispatches due fetches via the fetcher
      registry, pings a healthcheck URL every loop iteration (deadman's-switch
      style, so a crashed/stuck scheduler shows up as a missed ping, not
      silence — healthchecks.io URL itself is a `HEALTHCHECK_URL` env var, not
      yet set to a real one). `Dockerfile`/`docker-compose.yml` fully verified
      2026-07-22 (Docker installed in WSL Ubuntu): builds cleanly, cfgrib/
      rasterio/eccodes/cdo all work inside the container, and the full
      scheduler entrypoint runs correctly end-to-end against real data.
- [x] **T24** `sites.yaml` consumption, done 2026-07-23: `src/extract/base.py`
      adds `wnw_strip_points()` (great-circle destination formula) and
      `all_sample_points()` (each site + its 4 strip points at 25/25/75/100km,
      named e.g. `Luarca_wnw50km` — fits `PointRow`'s existing `site: str`
      field, no schema change needed). Wired into the 5 grid-based extractors
      (grib_regular, ecmwf, icon, meteofrance, aemet) — verified against real
      archived icon_eu data: 105 rows = 7 sites × 5 points × 3 valid times.
      **Deliberately not done for `ukmo_global`**: Open-Meteo is a point API,
      not a spatial grid, so strip sampling there needs `open_meteo_fetcher.py`
      to request the extra coordinates too, not just extraction — a follow-up
      if UKMO's WNW sightline signal is wanted.
- [ ] **T25** Reserve hosting per the deployment decision made 2026-07-22
      (box + hostname intentionally not named in this repo — see private ops
      notes). Own isolated directory/port; DNS + ingress live in a separate
      ops repo, not here. Do this once T23's scheduler exists; no need to
      stand up hosting before there's anything to run on it.

## Phase 1 — Jul 25–26 (T-18…-17d): prove the UI before real data exists

- [x] **T16** Time-shift sim mode, run for real 2026-07-23 (`scripts/
      backfill_open_meteo.py`, uncommitted `fetch_single_run` from earlier in
      the session made real and correct). T08 already found Previous-Runs API
      doesn't carry L/M/H, so this uses single-runs-api.open-meteo.com instead
      (per that task's own note) for 6 models: gfs_global, ecmwf_ifs025,
      om_icon_global, om_icon_eu, meteofrance_arpege_europe, ukmo_global.
      `ECLIPSE_T=2026-07-26T18:30:00Z` chosen deliberately close to the real
      run date (2026-07-23) rather than the real Aug 12 eclipse — a fixed
      future target only ~3 days out means even the oldest run_inits in a
      14-day backfill window only need a ~17-day lead at worst, and the most
      recent ones need almost none, maximizing how many of the 14-day×4-cycle
      window's real historical runs can actually reach it (the alternative,
      confirmed by T31(c)'s verification note, is picking a target so far out
      nothing reaches it at all).
      **Two real bugs found + fixed in the uncommitted `fetch_single_run`,
      neither previously run at scale:** (1) it copied `fetch()`'s
      `start_date`/`end_date` params, but single-runs-api rejects those
      outright (`HTTP 400 "Parameter 'start_date' must not be set"`, live-
      confirmed) — the real mechanism is `forecast_days` (a day-count *from
      `run` forward*), now computed from ECLIPSE_T's date with a +1-day margin
      (also live-verified necessary: without it a 06Z run's window falls one
      hour short of an 18:30Z target). (2) requesting more `forecast_days`
      than a run's real horizon is NOT an error — HTTP 200 with silently-null
      values past the true horizon — so `fetch_single_run` now checks the
      actual values at the wanted valid times and reports `not_yet_covering`
      instead of `ok` when they're all null, rather than writing a file
      `extract()` would turn into useless all-None rows.
      **A third real bug found in `scripts/backfill_open_meteo.py` itself**
      (not the uncommitted fetcher): its idempotency check reused `src.
      extract.base`'s `already_extracted()`/`.extracted` marker, which is
      keyed only by `(model, run_init)` — no target-date awareness. Since
      `ukmo_global`'s backfill label shares its raw-data directory with the
      live archiver's own primary Open-Meteo path for the same model name,
      and earlier testing (T31(c)) had already extracted 4 of these same
      run_inits against a *different* target date (2026-07-25), the naive
      marker check silently skipped real 2026-07-26 backfill work for them.
      Fixed by checking actual `points.parquet` contents for the current
      target date instead of trusting the marker file.
      **A fourth issue, not a logic bug but a real politeness gap:** a
      sustained ~324-request sequential run with zero delay produced 48
      transient connection failures (not literal HTTP 429s, but the same
      "back off and retry" character — manual retries of the identical calls
      succeeded immediately). Added exponential-backoff retry in
      `open_meteo_fetcher.py`'s new `_get_with_retry()` (covers connection
      errors/timeouts and 429) plus a flat 0.3s pacing gap between real HTTP
      calls in the backfill script — re-running with both fixes produced zero
      errors.
      **Also found blocking the very first dry run, unrelated to
      `fetch_single_run`:** `scripts/backfill_open_meteo.py`'s own imports
      (`from src.extract.base import ...`, `from src.extract.open_meteo_
      extractor import ...`) transitively ran `src/extract/__init__.py` and
      `src/fetchers/__init__.py`, which unconditionally import every GRIB-
      touching module — hard-crashing with `RuntimeError: Cannot find the
      ecCodes library` on this plain-Windows dev box (no Docker/WSL needed
      for anything else in this pipeline). Fixed both `__init__.py` files to
      import each submodule independently and log+skip ones that fail for a
      missing native dependency, instead of one crash taking down the whole
      package — no behavior change where ecCodes is present (Docker); only
      unlocks the GRIB-free modules (`open_meteo_fetcher`/`open_meteo_
      extractor`) importing standalone where it isn't, matching what open_
      meteo_extractor.py's own docstring already claimed should be possible.
      **Real numbers** (verified by loading `data/points.parquet` directly,
      not just trusting exit code 0 — 40,012 total rows now, up from 37,849
      pre-existing; zero duplicates across 3 backfill re-runs, confirmed by
      exact row-count arithmetic): gfs_global 1050 rows/50 run_inits,
      ecmwf_ifs025 567/27, om_icon_global 231/11, om_icon_eu 126/6,
      meteofrance_arpege_europe 63/3, ukmo_global 126/6 (zero nulls — the
      other 5 models each have exactly 7 null rows, all the same single
      run/valid-hour combination across all 7 sites: a run landing right at
      its forecast horizon boundary, 15h/18h real, 21h not yet reached — a
      real edge case, not a bug). Provenance matches `_PROVENANCE_BY_MODEL`
      exactly for all 6 (5× native, ecmwf_ifs025 derived); ukmo_global's
      `provenance_via_open_meteo: verify` caveat fired its warning as
      designed and is left open per T08/T12's existing framing, not
      re-litigated here. Every model landed multiple distinct run_inits
      (3–50) — the actual point of this task, a real run-evolution slider,
      not just one data point per model. Short-horizon models (arpege_europe
      ~4d, icon_eu ~5d, ukmo_global's 06/18Z 67h cap) correctly show fewer
      surviving run_inits than gfs_global/ecmwf_ifs025 (~16d/~15d horizons) —
      live-confirmed physical reality, not a bug: only 00Z/12Z cycles survive
      for ukmo_global, exactly matching T06's documented 06Z/18Z 67h-cap
      finding.
- [x] **T34** Tool 1: "latest run of every model" explorer, first real slice
      done 2026-07-23 (gfs + arome_france only — 13 more models are a
      follow-up). User asked for a general-purpose model-status explorer,
      deliberately decoupled from the eclipse date: one compact stacked row
      per model (like the Gantt, but each row shows that model's own
      latest-run steps, not eclipse coverage), a draggable time cursor
      spanning all rows highlighting each row's own nearest step, a model
      selector via clicking a row, a quantity selector (cloud L/M/H/total to
      start — combined-cloud+rain and surface temp are placeholders pending
      their own field-availability research, same rigor as T01-T12), and an
      adjustable right-extent slider so short-range/dense models can be
      scrubbed with more pixel precision once longer-range models fall off
      the edge. Iterated live with the user through several rounds on a
      mock-data prototype (`src/viz/web/tool1_prototype.html`) before any
      real wiring — fixed real bugs found each round: cursor/tick pixel
      misalignment (percentage-based positioning doesn't share a coordinate
      space with a differently-inset element), tick aliasing (sub-pixel
      percentages render inconsistently at 1px vs 2px — fixed by rounding to
      whole pixels, then had to redo that per-resize since a one-time round
      broke responsiveness), and multiple simultaneous "nearest tick"
      highlights on dense rows (was a fixed time-window, fixed to exactly one
      nearest step per row).
      **Real backend work**: this tool wants each model's FULL forecast
      range, not the 3 eclipse-hour steps the rest of the archiver fetches —
      a genuinely different fetch shape. Added `full_range_steps()` and
      `latest_available_run_init()` to `src/fetchers/base.py` (refactored
      `steps_for_run`'s cycle-capping logic into a shared `_available_steps_
      for_cycle()` first, no behavior change there), and a parallel
      `fetch_full_range()` entry point in `herbie_fetcher.py` (gfs) and
      `meteofrance_fetcher.py` (arome_france) alongside the existing
      eclipse-cropped `fetch()` — shared download-loop helpers extracted so
      the mechanics aren't duplicated. Writes to a NEW `DATA_RAW_LATEST`
      tree (`data/raw_latest/`, see `src/config.py`), deliberately separate
      from the eclipse archiver's `data/raw/` and its `.extracted`
      bookkeeping, per explicit user direction that dev-machine data doesn't
      need cleanup (disk isn't constrained here — see private notes) but
      shouldn't be conflated with the eclipse-specific tree either.
      `src/viz/tool1_renderer.py` renders one (model, run_init, step, field)
      PNG at a time, reusing existing extractor grid-read helpers (same
      reuse pattern as T31(c)); `scripts/generate_tool1_manifest.py` batch-
      renders every real step × 4 fields and writes a manifest.json the
      widget fetches directly (`src/viz/web/tool1_real.html`).
      **Verified for real, live endpoints**: gfs run 2026-07-23T00Z, 209/209
      steps reachable, 208 fetched cleanly (1 transient failure on f000,
      not investigated further yet); arome_france run 2026-07-23T03Z, 9/9
      group files, 52 hourly steps. 890MB raw GRIB2 for just these 2 models'
      single latest run — a real data point for the eventual production
      retention decision (T25), not acted on now per explicit "measure but
      don't gate desktop dev work on it" direction. 1044 real PNGs rendered
      (31MB), spot-checked for non-blank content (pixel std-dev, not just
      file existence), and the real widget verified end-to-end in-browser
      against them: correct per-model run_init labels, correct real image
      swap on model/field/cursor changes, correct extent auto-detected from
      the real max step (384h).
      **Extended to all 10 gridded models same day**: gefs_extended,
      arpege_europe (reused the already-generic fetch_full_range() -
      _MODEL_SPECS/spec["groups"] already covered them, only needed new
      tool1_renderer.py readers), plus ecmwf_hres/ecmwf_ens/aifs_single/
      aifs_ens (new fetch_full_range() in ecmwf_opendata_fetcher.py) and
      icon_eu/icon_global (new fetch_full_range() in dwd_bz2_fetcher.py,
      icon_global reusing the existing cdo remap weights). Deliberately
      still excludes ukmo_global/gem_global/jma_gsm/cma_grapes_global
      (Open-Meteo point API, no spatial grid) and aemet_harmonie (rendered
      color-ramp image) - same reasoning as T31(c)'s own _MODEL_READERS.
      **Added real cartography**: coastline + major/secondary roads
      (`src/viz/basemap.py`, new `geopandas` dependency) plus the totality
      band+centerline, all drawn stroke-only on top of the pcolormesh (not
      filled, unlike the sibling eclipse-dashboard project's own basemap
      styling - that project has nothing under its coastline layer, this
      one has real cloud data a filled coastline would hide). Natural Earth
      categories/resolution (1:50m land, 1:10m roads filtered to Major/
      Secondary Highway) matched to what that sibling project's own
      tools/build-data/basemap.mjs's/roads.mjs's research already
      established, not re-derived from scratch. Live-verified source URLs,
      both still live 2026-07-23.
      **Two more real bugs found verifying the expanded batch**: (1)
      full_range_steps() assumes step 0 is always published (true for most
      models, false for arome_france/arpege_europe's group-file layout, and
      it turned out GFS's own step 0 had also failed a transient fetch) -
      fixed by excluding any step where no field has real data, rather than
      showing a permanent "(no data)" tick. (2) the manifest only recorded
      an image URL per field, which always exists (render_frame writes a
      placeholder PNG even for a permanently-absent field like arome_
      france/arpege_europe's "total" or classic ecmwf_ens's entire low/mid/
      high split - T01's earlier finding, confirmed still true here) - so
      the widget couldn't tell a real map from a placeholder without
      inspecting pixels. Added a per-field has_data flag to the manifest
      schema (computed cheaply by calling the reader directly, not by
      re-rendering) and specific, honest UI messaging for both known
      permanent gaps instead of a generic "no data".
      **Added a per-model preload button** (warms the browser image cache
      across all of that model's steps x 4 fields before scrubbing) so
      dragging the time cursor doesn't stutter waiting on network fetches.
      **Not yet done**: contourf/smoothed rendering for coarse-resolution
      models (explicitly deferred by the user, "future improvement... but
      not now").
      **Real bug found and fixed 2026-07-23, after T35**: the shared cursor
      was "+Nh since each row's OWN run_init" - looked fine with only 2
      models wired (T34's original scope), but with all 10 real models
      archived it's a genuine correctness bug, not cosmetic. Publication lag
      varies a lot per model (ecmwf-opendata models lag hours behind gfs/
      icon), so different rows' "latest run" can be many hours apart in real
      wall-clock time - measured live: ecmwf_hres/ecmwf_ens/aifs_single/
      aifs_ens all still on their 00Z run while arome_france was already on
      09Z, a 9h spread. Under the old relative axis, dragging to "+12h" meant
      12:00Z for the 00Z-init models but 21:00Z for arome_france - same
      cursor position, 9 real hours apart. Ported the same fix already built
      for Tool 2 (see below): axis is now absolute calendar time, cursor is a
      Date, `nearestStep()` compares by real valid-time distance not raw
      hour offset, each row's own run_init tick is drawn bolder (now visibly
      NOT aligned across rows, which is the correct, honest picture), and a
      "now" line was added since inits can fall on either side of it. Logic
      verified by direct computation against the real measured drift (00Z/
      06Z/09Z-init mock rows correctly converge on the same real valid time
      at a shared cursor position) - live in-browser confirmation blocked by
      an unrelated Browser-pane tooling hang (navigation timing out); not a
      code issue, re-verify visually next session.
- [x] **T35** Archiver consolidation, done 2026-07-23. The "consolidation
      question" T34 raised (does the narrow eclipse-cropped `fetch()` still
      need to exist separately from `fetch_full_range()`?) was answered by
      the user same day: **"Yes, retire it, if everything can be done from
      the big archive."** Executed for all 10 gridded models (aemet_harmonie/
      ukmo_global deliberately untouched — image-snapshot/point-API shapes
      that were never split into narrow/full-range paths to begin with):
      - `herbie_fetcher.py`, `meteofrance_fetcher.py`, `ecmwf_opendata_fetcher.py`,
        `dwd_bz2_fetcher.py`: narrow `fetch()` and `fetch_full_range()` merged
        into one `fetch()` per module, using `full_range_steps()` +
        `raw_output_dir()` unconditionally. `steps_for_run()` kept — still
        used by extractors to pick which archived steps feed
        `points.parquet`'s eclipse-hour rows.
      - `src/scheduler/run.py`: removed the `steps_for_run()`-based
        reachability gate in `run_once()` — it now fetches every due run
        unconditionally, same as the (now-deleted) `collect_full_range.py`'s
        own simpler loop.
      - `src/config.py`/`src/fetchers/base.py`: `DATA_RAW_LATEST`/
        `raw_latest_output_dir()` removed entirely.
      - `tool1_renderer.py`/`generate_tool1_manifest.py`: switched to
        `DATA_RAW`; the manifest generator's fetch dispatch now goes through
        `src/fetchers/registry.py`'s shared `FETCHERS` dict (the same one
        the scheduler uses) instead of its own now-dead
        `fetch_full_range()`-keyed dict.
      **Production cutover**: stopped/removed the `eclipse-collector`
      container (running `collect_full_range.py`, started earlier the same
      day per rollout step 1), merged `E:\data\eclipse-weather\raw_latest\`'s
      20 run-init directories into `raw\` (zero collisions — verified per-
      model/run_init before moving), deleted `scripts/collect_full_range.py`,
      and started `eclipse-scheduler` (same `--restart unless-stopped`, same
      data mount, running the image's default `python -m src.scheduler.run`
      entrypoint — no separate command override needed, this was always the
      Dockerfile's CMD). Files under `raw_latest/` were root-owned (written
      by the Dockerized collector) and undeletable/unmovable as the plain
      WSL user — worked around by running `mv`/`rmdir` inside a throwaway
      root container rather than via `sudo` (no passwordless sudo available).
      **Verified for real** before cutover: rebuilt the Docker image, ran the
      4 consolidated fetch paths (herbie/http_grib/ecmwf-opendata/http_bz2)
      against real live endpoints — gfs (herbie) 208 real files, arome_france
      (http_grib) 9/9 group files, ecmwf_hres (ecmwf-opendata) 80 real files
      before the test was cut short (HRES's full 360h range makes a
      from-scratch test slow; already-real successful downloads were enough
      signal). http_bz2 (icon_eu/icon_global) not re-tested standalone —
      its download mechanics are byte-for-byte unchanged by this refactor
      and were separately proven live seconds before the cutover by the
      still-running `eclipse-collector`'s own logs. After cutover,
      `eclipse-scheduler` started cleanly, correctly logged-and-skipped
      point-extraction for merged runs missing their far-future steps
      (partial backfills from the just-stopped collector, not a bug), and
      began a real fresh fetch for a newly-due `aifs_ens` run within its
      first minute.
      **New finding, real disk-footprint numbers** (previous CLAUDE.md
      estimate — "well under 1GB" — assumed T21's crop-before-archive step,
      which was never built, AND assumed only 3 eclipse-hour steps per run,
      which full-range archiving no longer does): measured 48GB across just
      2 runs × 10 models post-merge. `aifs_ens` (50-member ensemble, 4
      cloud fields, ~60 steps) is ~16GB PER RUN — at its own 4-cycles/day
      cadence that's ~64GB/day if every run is kept indefinitely. Comfortably
      fine on this desktop (906GB free) per the explicit dev-phase "disk
      isn't constrained here" direction, but a hard new input for rollout
      step 4's production discard-pipeline sizing and for T25's box choice —
      a single in-flight `aifs_ens` run may not fit comfortably even
      transiently (fetch-before-discard) on a small production disk. Flagged
      to the user directly, not yet acted on (their call on whether e.g.
      ensemble member archiving should be subsetted — Tool 1's own renderer
      already only shows one representative member per model, so most of
      that 16GB/run is currently unused by anything downstream).
      CLAUDE.md updated: disk-footprint estimate replaced with these real
      numbers, `data/raw/` repo-layout comment corrected (it was never
      actually "Iberia-box slices" — herbie/ecmwf-opendata/dwd_bz2 subset by
      variable/step, not by area; spatial cropping only ever happened
      downstream in extract/viz, a pre-existing fact this consolidation
      didn't change but made newly obvious at scale), and the opening
      paragraph clarified: the archive itself is now full-range, extraction
      to `points.parquet` is still eclipse-hour-scoped.
- [x] **T36** Tool 2: single-model, stacked historical runs. Mock-data
      interaction prototype built 2026-07-23
      (`src/viz/web/tool2_prototype.html`), same "prototype first, wire real
      data after feedback" sequence as T34. Real archive checked first
      (user asked "does it have enough data for some models?" before
      committing to a build) — all 10 gridded models have 4-10
      distinct runs archived already (aifs_ens fewest, slowest/heaviest
      fetch; gfs/icon_eu/gefs_extended most, 10 each). Found and removed 2
      stray non-real run directories first (`gfs/2026072606` - a future-
      dated, empty artifact; `icon_eu/2020010100` - an empty leftover from
      early cdo remap-weight testing) - both verified empty before deleting.
      **Design, genuinely different from Tool 1**: rows are runs of ONE
      model (picked via a dropdown), not different models - so unlike Tool
      1 the axis was designed absolute-time from the start (each run has a
      different init, no shared relative clock makes sense here). Newest
      run on top; each run's own +0h tick drawn bolder; a "now" line;
      extent slider narrows the axis from the right (same convention as
      Tool 1). Verified via direct JS state inspection (screenshot tool in
      the Browser pane was unreliable/slow to read at the rendered scale,
      same lesson as T34's own verification passes): model-switch
      recomputes the axis span correctly per model (aifs_ens: 4 runs/6h
      cadence/360h reach; icon_eu: 9 runs/1h cadence/78h reach - both
      matched real config), row selection, cursor drag + nearest-step-by-
      valid-time, and the extent slider all behave correctly.
      **Not started yet (at mock stage)**: the real per-model manifest (every
      archived run, not just latest) and real rendering - waiting on user
      feedback on the interaction first, same gate T34 went through.
      **Side effect of this task**: user noticed while reviewing Tool 2's
      design that Tool 1 (T34, real widget) had the SAME relative-axis flaw
      Tool 2 was built to avoid from the start - see T34's own new note
      above for that fix.
      **Wired to real data 2026-07-23** (renamed `tool2_prototype.html` ->
      `tool2_real.html`). New `scripts/generate_tool2_manifest.py`: per
      model, per already-archived run_init (same `MAX_RUNS_PER_MODEL = 4`
      cap convention as T39's Tool 3 generator - "renderings aren't final
      anyway", user's own framing), renders EVERY step of each capped run
      (not just the one nearest a target time) across all 4 fields - the
      expensive generator of the three (thousands of renders per model).
      Explicitly framed by the user as ongoing/incremental work, same as
      fetching itself ("New ones only render once new model runs become
      available, and they need to be rendered regardless. Just don't delete
      the old ones") rather than a one-time cost to minimize - this task's
      own earlier instinct to scope the render job down out of concern for
      total image count was corrected on exactly this basis. Real manifest
      schema mirrors Tool 1/3's per-field `has_data` convention (image URLs
      always exist via a placeholder PNG; `has_data[field] === false` is the
      only way to tell a real frame from a placeholder). Bootstrap changed
      from the mock's static `populateModelSelect()` to a real
      `loadManifest().then(...)`; the preload button now actually warms the
      browser image cache (was a no-op `stopPropagation()` in the mock).
      **Caught one real correctness bug before it could waste render time on
      the ensemble models**: `tool1_renderer.py`'s `_read_ecmwf_grid()`
      (shared by all 3 tools) was picking one arbitrary representative
      member for `ecmwf_ens`/`aifs_ens` instead of computing the ensemble
      MEAN across all members - found by checking the render job's progress
      before it reached the ensemble models, fixed
      (`np.stack([...]).mean(axis=0)`), and the job restarted from a clean
      state rather than letting it render wrong ensemble frames first. See
      the aifs-ensemble-usage discussion (models.yaml `aifs_ens`/quantity
      dropdown) for why mean-per-quantity is the right default for map
      rendering specifically (point/ensemble-graph views still want full
      per-member spread).
      **Render job status**: a long-running background process
      (`docker exec -d` inside `eclipse-scheduler`, log at
      `/app/data/tool2_gen.log`) - not a blocker to closing this task, same
      as T39's own capped-render-job framing; it only needs to be allowed to
      finish (or re-run later to pick up newly-archived runs) before a full
      visual pass across all 10 models is possible. Real page wiring
      verified correct (`loadManifest()`/`init()` executed cleanly against
      the real manifest, zero errors) via the same non-visual verification
      method T39 used, for the same Browser-pane tooling limitation
      documented there (dynamically-inserted `<script>` tags don't execute
      in that environment) - a live pixel screenshot was not obtained this
      session, flagged rather than assumed.
- [x] **T37** Rain + surface-temp field research for Tool 3 (the eclipse
      valid-time explorer's planned combined-cloud+rain and surface-temp
      charts, currently placeholder-only in `tool1_real.html`'s Quantity
      dropdown). Same "verify via a real fetch, don't build against a docs
      guess" rule as T01-T09's original cloud-field research (CLAUDE.md
      constraint #6). Done entirely against the SAME sources/packages each
      model already fetches from - no new endpoints. Real findings,
      2026-07-23:
      - **gfs/gefs_extended (herbie/AWS idx)**: confirmed live via
        `Herbie(...).inventory()`. GFS f006 has `:TMP:2 m above ground:6
        hour fcst:` (clean, single message, no windowing ambiguity unlike
        cloud) plus THREE rain-related messages - `:PRATE:surface:6 hour
        fcst:` (instantaneous rate), `:PRATE:surface:0-6 hour ave fcst:`
        (windowed average), `:APCP:surface:0-6 hour acc fcst:`
        (accumulated total, x2 - GFS publishes this one twice, needs
        de-dup same as the cloud fetcher already does for TCDC). GEFS
        (atmos.5, same product as its total-cloud fetch) confirmed
        `:APCP:surface:0-6 hour acc fcst:ENS=low-res ctl:` and `:TMP:2 m
        above ground:6 hour fcst:ENS=low-res ctl:` - both ride along in the
        SAME product/file GEFS's cloud fetch already downloads.
      - **arome_france/arpege_europe (Meteo-France SP2)**: confirmed by
        inspecting group-window files ALREADY on disk (zero new network
        calls) via `cfgrib.open_datasets()`. arome_france: `t` (surface
        temperature, K) and `tirf` (time-integral of rain flux, kg/m² =
        accumulated mm - a real, usable rain field, cfgrib resolved the
        shortName cleanly). arpege_europe: `t` (surface temp, K, same as
        arome) confirmed by name; the precip field wasn't cleanly named by
        cfgrib (grouped under a generic "unknown" var due to a local eccodes
        table gap) - decoded directly via raw `eccodes` discipline/category/
        parameterNumber instead of trusting the shortName: found WMO-
        standard **0-1-6 = Total Precipitation** (stepType=accum) and a
        bonus **0-1-64 = Total Snowfall Rate Water Equivalent**, both
        already present in the same already-downloaded group file. Since
        both models share the same SP2 package family, adding these fields
        to `meteofrance_extractor.py` is a zero-new-fetch-cost extension of
        existing archived data for BOTH models.
      - **ecmwf_hres/ecmwf_ens/aifs_single (ecmwf-opendata)**: confirmed via
        real live `retrieve()` calls with `param: ["2t","tp"]`. All three
        returned clean single messages - `t2m` (2m temp, K, instant) and
        `tp` (total precipitation, accum - `m` water-equivalent for
        hres/ens, `kg/m²` for aifs_single, same physical quantity different
        units convention). `ecmwf_ens` confirmed across all 50 real `pf`
        members (same "zero cf messages" pattern already known from its
        cloud fields - not a new finding, consistent behavior).
        `aifs_ens` independently confirmed too (closed the gap right after
        first writing this note) - `t2m`/`tp` present across all 50 members,
        same shape pattern as its own cloud fields.
      - **icon_eu/icon_global (DWD opendata)**: confirmed live, HTTP 200 on
        the real bucket for both `T_2M` and `TOT_PREC` at the exact same
        URL convention (`{param_lower}/..._{PARAM}.grib2.bz2`) the cloud
        fields already use - a direct drop-in extension of
        `dwd_bz2_fetcher.py`'s existing `_cloud_params()` pattern.
      - **aemet_harmonie (geotiff)**: already fully known, zero new research
        needed - `aemet_geotiff_fetcher.py`'s own docstring already
        documents that the bundle contains temperature (product code `11`)
        and precipitation (code `61`, several accumulation windows:
        `_1HH`/`_3HH`/`_6HH`) rasters. Currently downloaded and discarded
        (only code `71`/Nubosidad is kept) - these would just need to be
        added to the extraction allow-list, no new fetch logic.
      - **ukmo_global (Open-Meteo)**: confirmed live via the real
        `/v1/forecast` endpoint - `temperature_2m` and `precipitation` both
        return real non-null hourly values alongside `cloud_cover`, same
        endpoint/mechanism `open_meteo_fetcher.py` already uses.
      **Bottom line**: every model this project archives already has real,
      verified rain and surface-temp fields available at the SAME source/
      package it already fetches from - no new endpoints, no new auth, and
      for arome_france/arpege_europe/gefs_extended specifically, the data is
      already sitting in already-downloaded files unused. Not yet done:
      actually adding these params to each fetcher/extractor and to
      `models.yaml`'s schema (a real structural change - new `cloud:`-
      sibling sections - not attempted here, this task was research only,
      same "confirm findings, then a separate scoping decision" gate T01-T09
      themselves went through before code was built against them).
- [x] **T38** Cloud-layer altitude/pressure boundary research - what "low/
      mid/high cloud" actually means per model family (informational: this
      project compares L/M/H across models side by side, so it matters
      whether "low cloud" is the same physical band everywhere or not -
      same "verify via primary sources, don't guess" rule as T01-T09/T37).
      Run 2026-07-23 as an 8-way parallel research fan-out (one agent per
      model family) plus a synthesis pass. Real, confidence-tagged findings:
      - **NCEP GFS/GEFS** (UPP `CLDRAD.f`): low >=642 hPa (~0-3.7 km), mid
        350-642 hPa (~3.7-8.1 km), high <350 hPa (~8.1 km+, no enforced
        upper bound in the live GFS branch despite an unused
        `PTOP_HIGH=150 hPa` constant). **Confirmed for GFS** (UPP User's
        Guide v4 quote + byte-for-byte cross-check against the live UPP
        source on GitHub). **GEFS inferred only** - same UPP codebase, not
        independently restated in any source found.
      - **ECMWF IFS/AIFS/ERA5** (sigma coordinate, sigma = p/p_surface): low
        1.0>sigma>0.8 (~0-2 km), mid 0.8>=sigma>0.45 (~2-6 km), high
        sigma<=0.45 (~6 km+). **Confirmed for IFS/ERA5** (ECMWF Parameter
        Database + Forecast User Portal Confluence FAQ, both direct quotes,
        agree exactly). AIFS emits lcc/mcc/hcc under the same param IDs and
        trains against this same ERA5/IFS-diagnosed label, but no ECMWF
        document states AIFS recomputes this sigma cutoff itself at
        inference - treat as "same definition by training-label
        provenance", not independently reverified for AIFS.
      - **DWD ICON** (CLCL/CLCM/CLCH): low sfc-800 hPa (~0-2 km), mid
        800-400 hPa (~2-7 km), high <400 hPa (~7 km+, open top).
        **Confirmed** - structure confirmed via DWD's own ICON Database
        Reference Manual (GRIB2 level-type codes); the literal 800/400 hPa
        numbers rest on secondary sources (DWD's product-catalog page 403'd
        automated fetch) plus an independent third-party mirror, both
        agreeing, plus the long-standing COSMO-inherited convention -
        matches models.yaml's pre-existing note, no change needed.
      - **Meteo-France ARPEGE/AROME** (NEBBAS/NEBMOY/NEBHAU): low >785 hPa
        (typically <2500 m **above model terrain**, not sea level - a
        separate ALTITUDE parameter is the real-terrain one, don't conflate
        them), mid 785-450 hPa (~2500-5000 m), high <450 hPa (~>5000 m).
        **Confirmed**, one shared boundary set for BOTH models per Meteo-
        France's own official glossary PDF - no separate ARPEGE-vs-AROME
        numeric split exists in this source.
      - **UK Met Office UM** (ukmo_global): consumer-site figures
        (weather.metoffice.gov.uk) state low <6,500 ft (~2 km), mid
        6,500-20,000 ft (~2-6.1 km), high >20,000 ft (~>6.1 km) - **"likely",
        NOT confirmed as the UM's actual internal diagnostic boundary**. The
        real UM STASH diagnostics (m01s09i203/204/205) are marked "not used
        - set to 0" (deprecated placeholders) in Met Office's own
        STASHmaster; the real, currently-used definition isn't published
        anywhere accessible that this research could find. Provenance stays
        native (own field, not RH-derived) but the altitude figure carries
        this caveat.
      - **GEM** (`gem_global`, via Open-Meteo): Open-Meteo's own docs state
        low 0-3 km / mid 3-8 km / high >8 km - but this is **Open-Meteo's
        own derived altitude band** (built from GEM's pressure-level
        fields, which are themselves RH/Sundqvist-approximated per T12's
        existing finding), **not an ECCC-published native GEM convention**.
        A candidate 680/440 hPa ECCC-adjacent figure surfaced in one
        paywalled 2025 paper but could not be confirmed as GEM's own
        diagnostic vs. an ISCCP satellite-simulator convention applied on
        top for that study - not used.
      - **JMA GSM** (`jma_gsm`): **unverified** - Open-Meteo's stated "3 km /
        8 km" text for JMA is confirmed to be **generic Open-Meteo
        boilerplate, byte-identical to the text on Open-Meteo's own GFS
        docs page**, not JMA-sourced. JMA's own official docs confirm the
        Cll/Clm/Clh fields exist natively but publish no numeric boundary in
        any source checked (2 official PDFs had non-extractable CJK tables
        - a residual gap, not a hard negative). Do not record 3 km/8 km as a
        JMA-confirmed figure.
      - **CMA GRAPES** (`cma_grapes_global`): **unverified**, same generic-
        boilerplate situation as JMA (identical text also appears on GFS's
        docs page). The one genuine CMA-authored data point found (Chen/Liu
        /Ma 2021, Acta Meteorologica Sinica) shows GRAPES' own cloud scheme
        splits low/mid/high by **model vertical-level index** (k<15/15-29/
        >=29), not a fixed km/hPa band - no altitude/pressure equivalent
        published for this. Several potentially load-bearing GRAPES papers
        were paywalled/unreachable, so this remains a genuine open gap, not
        just an absence of searching.
      - **This project's own derived ECMWF HRES bucketing** (not external
        research - `src/derive/humidity_to_cloud.py`'s
        `DEFAULT_LEVELS_HPA = (1000,925,850,700,500,300)`, calibrated T22):
        low={1000,925,850 hPa} ~ 0.11-1.46 km, mid={700,500 hPa} ~
        3.01-5.57 km, high={300 hPa} ~ 9.16 km (a single sampled level, not
        a two-sided band) - via the ICAO standard atmosphere. These same 6
        pressure levels feed `ecmwf_hres`'s derived cloud rows only, and are
        unrelated to ECMWF's own native sigma boundary above (that boundary
        has no native L/M/H product for HRES to apply it to - HRES only has
        `derived` levels in this registry, see its `cloud.levels.status`).
      **Bottom line**: only GFS, DWD ICON, and Meteo-France ARPEGE/AROME
      have a truly primary-sourced, model-specific numeric boundary;
      ECMWF/AIFS's is confirmed for the general IFS/ERA5 diagnostic but
      inferred (not independently reverified) for AIFS's own inference-time
      behavior; UKMO's is likely-but-unconfirmed; GEM's is Open-Meteo's own
      derived convention, not ECCC's; JMA and CMA have NO confirmed boundary
      at all despite Open-Meteo showing numbers for them - those numbers are
      reused boilerplate and must not be cited as model-specific. Full
      findings written up in `docs/models-reference.md`; concise per-model
      summaries + `task: T38` tags added to `config/models.yaml`'s
      `cloud.levels`/`cloud.altitude` blocks. Research fan-out: 8 parallel
      provider-specific agents + 1 synthesis agent, 2026-07-23.
- [x] **T39** Tool 3, 2026-07-23 (design revised same day, twice; wired to
      real data the same day). Original vision (way back when the 3 tools
      were first described): "eclipse valid-time weather, models stacked,
      slider over run-inits" - a multi-model view at a fixed valid time.
      **First revision** (same day): user observed that once the eclipse
      date falls inside Tool 2's window for a given model, Tool 3 "reduces
      to" Tool 2 with the time cursor parked on the eclipse valid time and
      the (T36) row/run cursor scrubbed vertically - so a first proposal
      was a deliberately smaller single-model dedicated page (model
      dropdown, quantity dropdown, one run slider, no draggable time axis).
      **Overruled the same day, final design**: back to a genuine multi-
      model page after all, but built from the SAME proven stacked-rows-
      plus-cursor pattern as Tool 1/Tool 2, not a from-scratch multi-model
      viz:
      - Rows = models (like Tool 1). Each row's x-axis = that model's own
        archived run-inits positioned at their real absolute calendar
        time (like Tool 2's axis, just one row per MODEL here instead of
        one row per RUN).
      - ONE shared vertical cursor across every row (confirmed explicitly:
        "vertical time init cursor should be the same for all models" -
        NOT independent per-row navigation). Dragging it moves all rows at
        once; each row snaps to its OWN nearest run-init tick to the
        cursor's position - exactly Tool 1's nearest-tick mechanic, just
        keyed on run-init distance instead of forecast-step distance. The
        valid time being displayed is always the fixed eclipse time, not
        the cursor position - the cursor only ever selects WHICH RUN each
        model shows, never which valid hour.
      - Quantity dropdown, same convention as Tool 1/2.
      - A toggle: "show all models at once" (a gallery - every row's
        currently-selected-run frame shown simultaneously, side by side,
        for real cross-model comparison at a glance) vs. "show just the
        clicked row" (one larger image, like Tool 1/2's existing single
        mapArea panel).
      **Mock prototype reviewed, 2 real bugs found and fixed same day**: the
      draggable cursor defaults to exactly "now", so the green now-line
      (originally a 1px line, same z-index scheme as Tool 1/2) was
      completely hidden underneath the opaque cursor line at load - fixed
      by making it a wider (5px) translucent band at a lower z-index, so it
      peeks out around the thinner cursor regardless of position. The
      target-time label was also overflowing past the stack's right edge
      when the marker sits near the axis end - fixed with a `.flip` class
      that switches the label to the line's left side once it's within
      90px of the edge.
      **Wired to real data same day** (renamed `tool3_prototype.html` ->
      `tool3_real.html`). New `scripts/generate_tool3_manifest.py`: per
      model, per already-archived run_init (capped to the 4 most recent -
      renderings aren't final, full backfill waits for the rendering
      approach itself to settle, per explicit user direction), renders one
      frame per field at the step nearest a fixed target valid time. Uses
      the project's own `ECLIPSE_T` env-var convention (not a new
      mechanism) - run as a one-off `docker exec` against the live
      `eclipse-scheduler` container with `ECLIPSE_T` overridden, never
      touching the container's own environment. Target picked from real
      current archive reach (`2026-07-25T11:00Z` - close to arome_france's
      own real 51h-reach boundary from its actual latest archived run,
      which turned out to be one cycle older than a first estimate assumed
      - a genuine, informative real-data finding, not a bug: publication
      timing doesn't always match "should be available by now" math).
      **Real result**: 9/10 models had all 4 recent runs covering the
      target; arome_france (shortest reach) had only 1/4 - exactly the
      realistic mixed-coverage picture the noCoverage tick styling exists
      to show. Also surfaced that several OLDER runs have `has_data: all
      false` despite `covers: true` for every field - the step is within
      the run's nominal reach but wasn't actually fully fetched (real,
      pre-existing archive gaps for those specific historical runs, not a
      new bug - the existing has_data mechanism catches and displays this
      honestly, exactly as designed).
      Verified by directly executing the real page's own
      `loadManifest()`/`init()` against the real generated manifest (10
      models, real image paths, correct has_data flags, zero errors) -
      live screenshot blocked by a Browser-pane restriction this session
      (dynamically-inserted `<script>` tags don't execute here), not a
      code issue.
- [x] **T15** Live-forward sim mode, done 2026-07-23. `ECLIPSE_T=2026-07-27
      T18:30:00Z` against a real live archiver (`festive_davinci` container,
      `/tmp/t15-soak-data`, started 2026-07-23 04:51 UTC) — soaked
      continuously for 4.5h+ across many scheduler ticks with zero crashes:
      75,754 real rows, all 12 registered models represented, multiple
      distinct `run_init`s accumulated per model, extraction firing cleanly
      after each fetch. Only errors seen are benign upstream 404s (DWD
      hasn't published every icon_global step yet at request time) — handled
      as designed (logged, skipped, loop continues). This is the real
      fetch/parse path test T16 bypasses; it passed.
      **Stopped 2026-07-23** (~8h runtime) — its job was done hours
      earlier (already verified above), continuing to run added no new
      information while still hitting live upstream APIs every 5 min, and
      it was testing the narrow eclipse-cropped `fetch()` path that the
      same-day archiver-consolidation discussion (rollout step 4) may
      retire anyway.
- [x] **T30** Availability Gantt, done 2026-07-23 (`src/viz/availability_gantt.py`).
      Deliberately simple/matplotlib per the user's explicit direction. Bar per
      model from `first_covering`, ticks per subsequent cycle that reaches T,
      colored by local step cadence (the originally-planned "misalignment from
      18:30Z" metric turned out to be a constant 0.5h for every model —
      structural, since every model steps on whole hours and T is :30 — so
      cadence is the actual encoded signal). Verified: correct staircase order
      (gefs_extended → gfs → AIFS/ECMWF → ICON/UKMO/ARPEGE → AROME/AEMET).
      **Found a real bug**: `aifs_ens` was missing `models.yaml`'s `fetch:` key
      entirely — the scheduler was silently never scheduling it. Fixed.
- [x] **T31(b)** Run-evolution small-multiples, done 2026-07-23
      (`src/viz/run_evolution.py`). T16 backfill doesn't exist yet, so this
      runs against whatever real run history is already archived (multiple
      real run_inits per model from T20-T24 testing) — designed to get richer
      automatically once T16 lands, not blocked on it. Fixed valid time, cloud%
      vs run_init, native L/M/H split from `ecmwf_hres`'s derived rows
      correctly (never averaged together). Ensembles shown as p10-90 band +
      median.
- [x] **T31(a)** Map layer, done 2026-07-23 (`src/viz/eclipse_map.py`) — pulled
      forward from blocked-on-T33 once T33 itself resolved (see below).
      Iberia bbox, totality band (N/S limits) + central line, 7 sites colored
      by a chosen model/field's latest archived value. Plain lat/lon axes, no
      cartopy — matches the "keep it simple" direction. No runtime L/M/H
      toggle (static images); call with a different `field` per image
      instead. Verified against real data for gfs/ecmwf_hres/icon_eu ×
      total/low (6 real maps, none hit the no-data fallback).
- [x] **T32** Site ranking, done 2026-07-23 (`src/viz/site_ranking.py`). Pooled
      -sample P(cloud_low<20%): every (model, member) row from that model's
      latest run counts as one Bernoulli sample, pooled across all
      contributing models — documented as a genuine, debatable design choice
      (an ensemble with many members could dominate a site's estimate over
      several one-vote deterministic models). Reports n_samples/n_models per
      site so this is visible, not hidden. Also surfaces each site's WNW-strip
      worst-case (T24) as a secondary annotation.
      **Backfill note**: none of T30/T31/T32 originally had real `points.parquet`
      to test against (only a 42-row orphan fixture) — regenerated it for real
      by re-running the actual, unmodified extractor registry against already
      -fetched real raw files in Docker (no new network calls except cached
      cdo weights): 37,849 real rows across 11 models, zero duplicates.
      Reconciled the `.extracted` idempotency markers afterward so the real
      scheduler won't re-append these same rows later.
- [x] **T31(c)** Gridded field comparison, done 2026-07-23
      (`src/viz/cloud_field_comparison.py`) — user asked why T31(a)'s map only
      showed 7 dots; this re-reads the raw archived GRIB2s directly (reusing
      each extractor's private grid-opening helpers rather than duplicating
      per-format parsing) and renders actual pcolormesh fields, small-multiples
      across gfs/ecmwf_hres/icon_eu/icon_global/arpege_europe, totality band +
      central line overlaid. Excludes aemet_harmonie (color-ramp image),
      ukmo_global (no spatial grid), and ensembles (stretch goal).
      **Found two real bugs while verifying against real archived data**:
      (1) `_latest_run_init` picked the lexicographically-newest run
      directory even when it was empty (a fetch still in flight) — fixed to
      skip dirs with no files. (2) the "(no data)" fallback text used data
      coordinates instead of `ax.transAxes`, so with the Iberia bbox's real
      lat/lon range it rendered far outside the visible panel — fixed.
      Also switched `pcolormesh` to `rasterized=True`: unrasterized SVG
      output was ~9.5MB per file (one vector shape per grid cell); rasterized
      is ~200KB with identical visual result.
      **Verification note**: with no `ECLIPSE_T` override, every model
      correctly reports no coverage — the real Aug 12 eclipse is still ~3
      weeks past every model's max forecast reach, so nothing currently
      archived can reach it yet (expected, not a bug; see Phase 2 below).
      Verified instead with `ECLIPSE_T=2026-07-25T18:30:00Z`, matching what
      was actually fetchable from the already-archived test runs: all 5
      models render real fields, zero unexpected no-data panels.

## Phase 2 — Jul 27 onward: real data comes online

- [ ] Jul 27 18Z: confirm first real GFS run lands; archiver smoke test.
- [ ] Jul 29: ECMWF ENS + AIFS ingest live.
- [ ] Aug 3: HRES online; run T22's calibration check for real.
- [ ] Aug 5–8: ICON Global → UKMO → ICON-EU → ARPEGE come online in sequence
      — confirm each against its `first_covering` as it happens; flag drift.
- [x] **T33 (polygon part)** done 2026-07-23, pulled forward from Aug 9-10 —
      turns out a validated Besselian-element calculation for this exact
      event already existed in a sibling project (`eclipse-calc` +
      `eclipse-dashboard`'s precomputed output), so no new calculation was
      needed. Copied into `config/totality_path.json` with provenance
      (centralLine/northLimit/southLimit, thinned to 5s resolution) and
      overlaid on T31(a)'s map. Quick check: all 7 current `sites.yaml`
      candidates fall inside the totality band with 68-184km margin from the
      nearest edge — a good sign, not a final answer.
      **Still human territory, unchanged**: final site-list sign-off — the
      tool informs it, doesn't make it (see CLAUDE.md).
- [ ] Aug 10–11: HARMONIE + AROME ingest; fetch cadence to every-cycle.
- [ ] Aug 12: nowcast mode — Meteosat imagery + AEMET obs/radar alongside
      final NWP runs. Site call ~15 UTC.

## Tool 1/2/3 rollout — desktop now, production later

A separate timeline from Phase 2 above — this is the "latest run" explorer
suite's (T34+) own path to production, not the core archiver's. Order
matters: each step assumes the one before it is genuinely done, not just
started. Laid out 2026-07-23 per explicit user direction.

- [x] **1. Robust raw-archiving service (desktop, now).** Done 2026-07-23,
      superseded same day by T35's consolidation. Originally
      `scripts/collect_full_range.py` running as `eclipse-collector`
      (started per the user's own explicit go-ahead), checking every 15 min
      for newly-available runs across the 10 wired gridded models and
      fetching them into a separate `data/raw_latest/` tree — fetch only, no
      rendering. Once T35 decided there's no longer a separate narrow/
      full-range split, this whole script became redundant with
      `src/scheduler/run.py` (the same module the production Dockerfile was
      always going to run) — merged `raw_latest/` into `raw/`, deleted
      `collect_full_range.py`, replaced `eclipse-collector` with
      `eclipse-scheduler` (same `--restart unless-stopped`, same
      `E:\data\eclipse-weather` mount, unified scheduler). One standing
      service now, not two. `Dockerfile` still bakes in `scripts/` (needed
      for `generate_tool1_manifest.py`).
- [ ] **2. Polish renderings (desktop, ongoing, no deadline of its own).**
      Basemap (coastline/roads) + totality-path overlay done (T34).
      Explicitly still open, not urgent: contourf/smoothed rendering for
      coarse-resolution models (gfs etc. — matplotlib pcolormesh currently
      shows visible grid cells), assorted UI polish (e.g. preload-button
      visibility at the current row height). Runs as long as it needs to
      against whatever step 1 has archived — steps 3 and 4 below don't
      start until the user is actually happy with how renderings look, not
      on any calendar trigger.
- [ ] **3. Migrate existing renderings to production** (once step 2 is
      approved, not before). Copy already-rendered output from this desktop
      to the production box (see private ops notes, not this repo),
      bootstrapping production with real historical renders instead of
      starting from zero — "up to a certain point at least" per the user;
      exact cutoff (all of it vs. a recent window) not yet decided. Needs
      T25 (hosting reserved) done first.
- [ ] **4. Production's own fetch → render → discard pipeline.** Unlike
      this desktop (which keeps raw data forever, per explicit direction —
      disk isn't constrained here, see private notes), production must NOT
      accumulate raw GRIB2/GeoTIFF indefinitely: fetch a run, render it,
      then delete the raw file — only once render success is confirmed,
      never speculatively (a failed/partial render must not lose the raw
      it would have needed to retry). A genuinely different pipeline shape
      from the desktop scheduler's current "fetch and keep forever" design
      (see T35's real disk-footprint numbers — `aifs_ens` alone is ~16GB/run
      — for why production categorically cannot run this desktop's
      unmodified keep-everything behavior).
      **Hard blocker, not just T25**: `points.parquet` (site-level numeric
      extraction — cloud_low/mid/high/total per site per run, what T30/
      T31/T32's Gantt/run-evolution/site-ranking views actually consume)
      is a second byproduct of the same fetch, same as rendered images —
      2026-07-23 discussion confirmed the current 7-site `sites.yaml` list
      is far too short to be the permanent extraction set. Once raw gets
      discarded after rendering, a site NOT in the extraction list at fetch
      time can never be added retroactively for that run. The site list
      must be made comprehensive BEFORE this step goes live — see the new
      placename-picker tool noted under Deferred below; do not enable
      delete-after-render until that's settled.
      **Consolidation question raised the same day — decided and executed
      (see T35)**: the user confirmed "retire it, if everything can be done
      from the big archive." The narrow eclipse-cropped `fetch()` path no
      longer exists; every fetcher always archives the full range into the
      one `data/raw/` tree, and point-extraction (`steps_for_run()`) runs
      against that same full-range archive rather than a separately-cropped
      fetch. This step's own "production must discard raw after render"
      requirement is now the ONLY reason a narrow fetch was ever
      contemplated in the first place — with the full-range archive as the
      single source, production's discard-after-render pipeline just needs
      to pick which steps to render (eclipse-hour steps first) and discard
      the rest, not maintain two parallel fetch paths.
- [ ] **5. Status/monitoring UI page.** What's been fetched, what's been
      rendered, any errors per model/run, and predicted next-run
      availability (derivable from `models.yaml`'s `cycles`/
      `publication_lag_h` — the same data T30's availability Gantt already
      reads, so this is likely a natural sibling view to that chart, not a
      from-scratch design).
- [ ] **6. Rendering-priority scheme as Aug 12 approaches.** Once render
      throughput can't keep up with everything due, prioritize "dense"
      runs first — short-range/high-resolution models (AROME, HARMONIE,
      ICON-EU) that become more informative/urgent the closer the eclipse
      gets, matching CLAUDE.md's own phased model-onboarding calendar —
      over coarser/longer-range models. Not needed while volume is low;
      becomes relevant once step 1's service has been running a while
      and/or step 4's production pipeline is live.

## Viewing Tool 1/2/3 locally (dev desktop)

`file://` fetches are NOT reliable for these widgets - same-directory
relative `fetch()` calls work in the embedded dev Browser pane used
during this project's own build sessions, but real browsers (confirmed
2026-07-23, user's own browser) can block even that. Don't fight this -
use the standing local HTTP server instead:

    python3 -m http.server 8734   # run from E:\data\eclipse-weather\viz\tool1_frames\

Then open, from any real browser (WSL2 forwards localhost to Windows
automatically):

    http://localhost:8734/index.html          # Tool 1
    http://localhost:8734/tool2_index.html    # Tool 2
    http://localhost:8734/tool3_index.html    # Tool 3

All three served copies live in that one directory alongside their real
manifests (`manifest.json`, `tool2_manifest.json`, `tool3_manifest.json`)
and rendered PNGs - never edit these directly, they're synced copies of
the real source files in `src/viz/web/` (`tool1_real.html`,
`tool2_real.html`, `tool3_real.html`) after each edit.

## Deferred / not now

- Met Office DataHub key **[human]** — only pursue if T06 shows the
  Open-Meteo path insufficient.
- Deployment box + healthcheck account **[human]**.
- Final go/no-go site choice **[human]** — the tool informs it, doesn't make it.
- ~~Mobile support for Tool 1/2/3's cursors~~ — done, see **T42** below.
- ~~Placename-picker tool for the extraction site list~~ — done, see **T41**
  below. Still blocks rollout step 4 in a different sense now: T41's real
  finding is that Natural Earth alone likely isn't sufficient for a truly
  comprehensive list (only 16 places found) - a follow-up data-source
  decision, not further tool-building, is what's actually still open here.

## Post-real-data build, 2026-07-23/24

- [x] **T40** Probability-of-clear map quantity. New `prob_clear` field:
      per-grid-cell P(cloud_low < 20%) across an ensemble's members, the
      map analogue of `site_ranking.py`'s (T32) own pooled point metric,
      same threshold. `src/viz/tool1_renderer.py` gained
      `_read_ecmwf_grid_prob_clear()` (scale applied BEFORE thresholding,
      unlike the plain ensemble mean, which commutes with a post-average
      scale) and wired it into `_aifs_field`. Only `aifs_ens` has genuine
      per-member native low-cloud data to compute a real probability from
      (`aifs_single` also renders - degrades to a binary 0%/100% per-cell
      map, a single deterministic run, not a special case); every other
      model returns `has_data: false` through the same mechanism already
      used for other known field gaps - verified against all 10 gridded
      models directly (zero crashes, only aifs_single/aifs_ens produce
      real output). Sanity-checked against real archived data: correlation
      between `prob_clear` and mean low-cloud is **-0.94** - exactly the
      expected relationship, not just "didn't crash." Wired into all 3
      tools' quantity dropdowns + `KNOWN_FIELD_GAPS` messaging, verified
      end-to-end in-browser for both a qualifying model (aifs_ens: real
      108KB rendered frame) and a non-qualifying one (gfs: honest gap
      message, not a broken image).
- [x] **T41** Placename-picker tool
      (`scripts/generate_placename_data.py` + `src/viz/web/
      placename_picker.html`, served as `placename_picker_index.html`).
      Real Natural Earth fields verified by inspection, not guessed:
      `NAME`/`NAMEASCII` (label), `POP_MAX` (population), `SCALERANK`
      (0-10 editorial significance rank), `RANK_MAX`, `ADM0NAME`. Clipped
      to the totality band (`northLimit`+reversed `southLimit` from
      `config/totality_path.json`, same polygon-from-two-bounding-lines
      construction, via `gpd.clip()` reusing `basemap.py`'s own private
      download/cache/clip helpers directly rather than duplicating them).
      Two live sliders (population, significance), plain-SVG band/
      centerline overlay + absolutely-positioned dots (no mapping
      library, matches Tool 1/2/3's own no-framework convention).
      **Real, load-bearing finding**: only 16 real places fall inside the
      totality band from this source, all in Spain, Bilbao down to
      Guadalajara - Natural Earth's populated-places layer is a globally
      curated significance-filtered ~7,300-place set, not an exhaustive
      gazetteer, so no actual villages show up at any threshold. This
      tool does its job (fast browsing/decision support), but this data
      source alone probably won't be enough for the eventual comprehensive
      extraction-list effort (rollout step 4's blocker) - a follow-up data
      -source question, not more tool-building.
- [x] **T42** Mobile/touch support for Tool 1/2/3. Added real
      `touchstart`/`touchmove`/`touchend` handling alongside the existing
      mouse events (same underlying drag functions, no duplicated logic),
      page scroll blocked only while an actual drag is in progress (not
      globally), a `<meta name="viewport">` tag (missing from all three),
      and touch-friendly tap targets for the smallest controls. Built via
      3 parallel agents, one per file, given an identical spec - kept
      genuinely consistent (same numeric constants, same approach) rather
      than 3 independent interpretations. **Real, honestly-reported
      finding**: only Tool 2 actually has a hit-zone/tolerance concept
      (its two-handle time-cursor-vs-row-selection design, `CURSOR_HIT_PX`
      /`ROW_HIT_PAD_PX`) - it got real touch-specific tolerances
      (20px/16px vs the existing 8px/6px mouse values, gated by a new
      `isTouch` flag). Tool 1 and Tool 3 have no hit-zone concept at all
      (click/tap anywhere jumps the cursor directly, confirmed by reading
      both files rather than assumed) - correctly left alone rather than
      inventing tolerance constants that don't apply to their actual
      interaction model. Verified for real: a dispatched `TouchEvent` 12px
      from Tool 2's cursor line correctly grabs the time cursor (within
      its 20px touch tolerance) while an identical `MouseEvent` at the
      same distance correctly falls back to row-selection (outside its
      8px mouse tolerance) - the full event-wiring chain, not just the
      underlying logic.
