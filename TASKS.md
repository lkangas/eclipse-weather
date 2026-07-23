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
- [x] **T15** Live-forward sim mode, done 2026-07-23. `ECLIPSE_T=2026-07-27
      T18:30:00Z` against a real live archiver (`festive_davinci` container,
      `/tmp/t15-soak-data`, started 2026-07-23 04:51 UTC) — soaked
      continuously for 4.5h+ across many scheduler ticks with zero crashes:
      75,754 real rows, all 12 registered models represented, multiple
      distinct `run_init`s accumulated per model, extraction firing cleanly
      after each fetch. Only errors seen are benign upstream 404s (DWD
      hasn't published every icon_global step yet at request time) — handled
      as designed (logged, skipped, loop continues). This is the real
      fetch/parse path test T16 bypasses; it passed. Container is still
      running — leave it, it's free ongoing validation.
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

- [ ] **1. Robust raw-archiving service (desktop, now).**
      `scripts/collect_full_range.py` (built 2026-07-23) checks every 15 min
      for newly-available runs across all 10 wired gridded models and
      fetches them into `data/raw_latest/` — fetch only, no rendering.
      Needs to actually be running continuously as a standing background
      service, not just smoke-tested once. This is the user's own service
      to start/own (a standing decision, not something to launch
      unilaterally per session) — a long-lived Docker container on this
      desktop. Old eclipse-archiver data (`data/raw/`, `points.parquet`,
      the icon_global cdo remap cache) was migrated off OneDrive to
      `E:\data\eclipse-weather\` the same day this was written, so both
      trees now live off OneDrive consistently.
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
      then delete the raw file once its renders exist. A genuinely
      different pipeline shape from `collect_full_range.py`'s current
      "fetch and keep" design for the desktop — do not deploy that script
      unmodified to production.
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

## Deferred / not now

- Met Office DataHub key **[human]** — only pursue if T06 shows the
  Open-Meteo path insufficient.
- Deployment box + healthcheck account **[human]**.
- Final go/no-go site choice **[human]** — the tool informs it, doesn't make it.
