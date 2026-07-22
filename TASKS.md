# TASKS.md — work queue

Work top-down within a phase; phases are ordered by the calendar, not preference.
Every research task (T01–T12) ends by updating `config/models.yaml`
(`status: verify` → `confirmed`, fill in any corrected values) — never build a
fetcher against an unverified URL template. Tasks marked **[human]** need the
user, not Claude Code — surface them, don't attempt.

## Phase 0 — now → Jul 24 (T-21…-19d): scaffold + research sprint

- [x] **T00** Scaffold repo layout from CLAUDE.md §"Repo layout". Init `uv`
      project, ruff config, empty modules per directory.
- [ ] **T01** ECMWF open-data cloud params. Fetch one `.index` file each for
      `oper` and `enfo`, both 00Z and 12Z. Grep for `tcc`/`lcc`/`mcc`/`hcc`.
      Update `models.ecmwf_ens.cloud` and `models.ecmwf_hres.cloud`.
      *Decides whether HRES/ENS need the derived-cloud path at all.*
- [ ] **T02** AIFS cloud params + cycle lengths. Same index-grep on
      `aifs-single` and `aifs-ens`; confirm all 4 cycles reach 360h.
      Update `models.aifs_single`, `models.aifs_ens`.
      *If lcc/mcc/hcc present: AIFS becomes the best long-range native L/M/H
      source — re-rank it above GFS in the plan doc.*
- [ ] **T03** GEFS cloud-level location. Check pgrb2a vs pgrb2b family for
      LCDC/MCDC/HCDC; confirm presence/absence beyond 384h in the extended run.
      Update `models.gefs_extended.cloud`, `.source`.
- [ ] **T04** ICON Global grid. Check opendata.dwd.de for regular-lat-lon
      single-level files vs icosahedral-only. If icosahedral-only, fetch DWD
      grid-description files and prototype a `cdo` remap; cache weights.
      Update `models.icon_global.grid`, `.source`.
- [ ] **T05** Météo-France. (a) AWS registry bucket layout for ARPEGE Europe +
      AROME France — confirm bucket name, which package (SP2/HP1/other) holds
      lcc/mcc/hcc. (b) Exact per-cycle max lengths for both models (00/06/12/18,
      and 03Z for AROME). (c) AROME France domain southern edge vs 38–40°N —
      does it reach Zaragoza/Castellón/Palma? Update `models.arpege_europe`,
      `models.arome_france` fully; update `sites.yaml` AROME-relevant notes.
- [ ] **T06** UKMO. (a) Confirm cycle lengths per run hour. (b) Confirm
      Open-Meteo model id `ukmo_global_deterministic_10km` (or find correct id)
      and measure actual delay vs DataHub direct. (c) Check whether DataHub
      atmospheric orders support area-subsetting (matters for the 1GB/month
      quota). Update `models.ukmo_global.source`.
- [ ] **T07** AEMET. (a) **[human]** Register an OpenData API key at
      opendata.aemet.es. (b) Full field catalog for HARMONIE-AROME via the
      registered API — specifically hunt for any cloud-by-level field beyond
      "nubosidad" (GRIB endpoint, not just the GeoTIFF map product). (c) Update
      cadence of the api-eltiempo download endpoint. (d) Check for a dedicated
      AEMET eclipse-2026 forecast product. Update `models.aemet_harmonie` fully.
- [ ] **T08** Open-Meteo Previous-Runs API. Confirm host/endpoint, per-model
      history depth, and — critically — read their docs for which models'
      `cloud_cover_low/mid/high` are native vs humidity-derived. Record the
      per-model flag in `models.open_meteo.model_ids_candidates` (add a
      `provenance` field per id). *Blocks T16 (sim backfill).*
- [ ] **T09** Eclipse-in-radiation-scheme survey. For each model family
      (IFS, ICON, UM, GFS, AIFS): does the radiation scheme simulate the
      Aug 12 solar obscuration? Add findings to each model's `notes` in
      models.yaml. *Informational — affects how much to trust 17–19 UTC
      low-cloud/convection evolution per model.*
- [ ] **T10** Retention spot-check. Re-confirm the retention numbers already
      in models.yaml (DWD ~24h, AEMET latest-only, AWS permanent, ECMWF ~4d,
      Météo-France ~14d) are still accurate. Cheap; do alongside T01–T07.
- [ ] **T11** *(optional)* ICON-EU-EPS: does opendata.dwd.de serve a EU
      ensemble with cloud fields? Would add a mid-range probabilistic layer
      with real L/M/H. Add as `models.icon_eu_eps` only if it checks out.
- [ ] **T12** *(optional, low priority)* GEM (Canada) / JMA / KMA / CMA via
      Open-Meteo — quick check only if T01–T09 leave spare time. Coarse
      global models, marginal value; skip if the calendar is tight.

## Phase 0 — build against confirmed metadata

- [ ] **T20** Fetcher modules (`src/fetchers/`): herbie-based (GFS, GEFS,
      AIFS), `ecmwf-opendata`-based (ENS, HRES), `http_bz2` (DWD ×2),
      `http_grib` (Météo-France ×2), `geotiff` (AEMET), `open_meteo_json`
      (aggregator + UKMO primary path). One module per `fetch:` value in
      models.yaml — don't hardcode URLs, read the template from the registry.
- [ ] **T21** Extract module (`src/extract/`): GRIB2/GeoTIFF → xarray → Iberia
      bbox slice (raw archive) + per-site/per-strip point rows → append to
      `data/points.parquet` per the schema in CLAUDE.md. Tag every row with
      `provenance` (native/derived/total_only).
- [ ] **T22** Derived-cloud module (`src/derive/`): humidity (q, pressure
      levels) → RH (Murphy & Koop) → low/mid/high cloud fraction. Acceptance
      test: run it on GFS (which has native L/M/H) and compare
      derived-from-GFS-humidity against GFS-native as a calibration check
      before trusting it on ECMWF HRES.
- [ ] **T23** Scheduler (`src/scheduler/`): generate systemd timer units from
      `models.yaml` cycles + publication_lag + margin. Wire a healthcheck
      ping (e.g. healthchecks.io) into every scheduled fetch — a silent
      failure Aug 5–12 is the worst outcome.
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
