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
- [ ] **T12** *(optional, low priority)* GEM (Canada) / JMA / KMA / CMA via
      Open-Meteo — quick check only if T01–T09 leave spare time. Coarse
      global models, marginal value; skip if the calendar is tight.

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
      yet set to a real one). Smoke-tested against real `models.yaml` (dry run,
      no fetchers registered yet — correctly identified `gefs_extended` as due
      and attempted it). `Dockerfile`/`docker-compose.yml` are **unverified** —
      no Docker runtime available locally yet to build/test against.
- [ ] **T24** `sites.yaml` consumption: implement WNW-strip sampling
      (bearing/length/interval from `wnw_strip:` block) alongside the point
      extraction at each site.
- [ ] **T25** Reserve hosting per the deployment decision made 2026-07-22
      (box + hostname intentionally not named in this repo — see private ops
      notes). Own isolated directory/port; DNS + ingress live in a separate
      ops repo, not here. Do this once T23's scheduler exists; no need to
      stand up hosting before there's anything to run on it.

## Phase 1 — Jul 25–26 (T-18…-17d): prove the UI before real data exists

- [ ] **T16** Time-shift sim mode. Set `ECLIPSE_T` to a past 18:30 UTC;
      backfill full multi-model run history via Open-Meteo Previous-Runs API
      (needs T08's provenance flags). Gives a complete run-slider dataset
      immediately.
- [ ] **T15** Live-forward sim mode. Set `ECLIPSE_T` ≈ now+4d; run the real
      archiver end-to-end against it. This is the actual fetch/parse path
      test that T16 bypasses — do it before Jul 27, not after.
- [ ] **T30** Availability Gantt (`src/viz/`): one row per model from
      `models.yaml`, bar starts at `first_covering + publication_lag`, ticks
      at subsequent runs, encode step-density at T by opacity/color. Static
      SVG first (matplotlib), plotly later. Read models.yaml directly — no
      hardcoded dates.
- [ ] **T31** Run-evolution view: fixed valid time T, slider over run-init
      times. (a) Map layer — Iberia + chosen model/run, L/M/H toggleable,
      totality path + sites overlaid. (b) Per-site trajectory small-multiples
      — x=init time, y=cloud%, 3 lines (L/M/H) per model panel; ensemble
      models as spaghetti or 10–90% band. Build against the T16 backfill.
- [ ] **T32** Site ranking view: sort `sites.yaml` entries by latest
      P(cloud_low < 20%) across ensemble members / model spread.

## Phase 2 — Jul 27 onward: real data comes online

- [ ] Jul 27 18Z: confirm first real GFS run lands; archiver smoke test.
- [ ] Jul 29: ECMWF ENS + AIFS ingest live.
- [ ] Aug 3: HRES online; run T22's calibration check for real.
- [ ] Aug 5–8: ICON Global → UKMO → ICON-EU → ARPEGE come online in sequence
      — confirm each against its `first_covering` as it happens; flag drift.
- [ ] **T33** Totality path polygon (Besselian elements / Xavier Jubier KMZ).
      Overlay on T31's map; validate/refine `sites.yaml` against the real
      centerline before the Aug 9–10 site shortlist.
- [ ] Aug 10–11: HARMONIE + AROME ingest; fetch cadence to every-cycle.
- [ ] Aug 12: nowcast mode — Meteosat imagery + AEMET obs/radar alongside
      final NWP runs. Site call ~15 UTC.

## Deferred / not now

- Met Office DataHub key **[human]** — only pursue if T06 shows the
  Open-Meteo path insufficient.
- Deployment box + healthcheck account **[human]**.
- Final go/no-go site choice **[human]** — the tool informs it, doesn't make it.
