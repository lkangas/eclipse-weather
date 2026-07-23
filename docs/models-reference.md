# Models Reference — Eclipse Cloud Forecast Tool

Replaces the original pre-research `model-fetch-plan.md` (2026-07-22), which
described a narrower eclipse-cropped architecture and several access paths
that turned out to be wrong or dead (the Météo-France AWS bucket, "cron/
systemd" deployment) before any real fetcher was built against them. This
document instead reflects what T01–T39's real research and the archiver's
own build actually found, as of 2026-07-23.

**This is a narrative overview, not the source of truth.** Every exact
number here (cycles, step cadence, publication lag, URLs, param names,
altitude boundaries) lives in **`config/models.yaml`** — CLAUDE.md hard
constraint #2 requires that file stay the single place those numbers are
edited. If a figure here and in `models.yaml` ever disagree, `models.yaml`
is correct; this doc describes it wrong, not the other way round. Full
research trails (citations, live-test evidence, dead ends) are in
`TASKS.md`'s `T01`–`T39` entries — this doc summarizes, it doesn't re-derive.

## 1. Model roster

15 models across 5 provider families, plus one aggregator (Open-Meteo, used
as 3 models' primary path and as every model's sim/backfill path) and one
reference baseline (climatology). "First covers T" = earliest run whose
forecast reaches 2026-08-12T18:30Z at today's `first_covering` values in
`models.yaml` — these shift slightly as cycle-length/lag corrections land,
treat the file as current, this table as illustrative ordering.

| Model | Provider | Kind | Resolution | Cycles → max reach | First covers T | Native L/M/H? |
|---|---|---|---|---|---|---|
| `gefs_extended` | NOAA | ensemble (31) | 0.5°/0.25° | 00Z→840h, 06/12/18Z→384h | **already live** (Jul 9) | Yes (pgrb2b TCDC) |
| `gfs` | NOAA | deterministic | 0.25° | all cycles→384h | Jul 27 18Z | Yes (LCDC/MCDC/HCDC) |
| `ecmwf_ens` | ECMWF | ensemble (50) | 0.25° | 00/12Z→360h, 06/18Z→144h | Jul 29 00Z | **No** — tcc only |
| `aifs_single` | ECMWF (AI) | deterministic | 0.25° | all cycles→360h | Jul 28 18Z | Yes (lcc/mcc/hcc) |
| `aifs_ens` | ECMWF (AI) | ensemble (~50) | 0.25° | all cycles→360h | Jul 28 18Z | Yes (lcc/mcc/hcc) |
| `ecmwf_hres` | ECMWF | deterministic | 0.25° (native 9km) | 00/12Z→240h, 06/18Z→90h | Aug 3 00Z | **No** — derived from humidity |
| `icon_global` | DWD | deterministic | ~13km icosahedral | 00/12Z→180h, 06/18Z→120h | Aug 5 12Z | Yes (CLCL/CLCM/CLCH) |
| `icon_eu` | DWD | deterministic | 0.0625° (~7km) | all cycles→120h | Aug 7 18Z | Yes (CLCL/CLCM/CLCH) |
| `ukmo_global` | Met Office | deterministic | ~10km | 00/12Z→168h, 06/18Z→67h | Aug 6 00Z | Yes (provenance: verify) |
| `arpege_europe` | Météo-France | deterministic | 0.1° | all cycles→102h | Aug 8 18Z | Yes (SP2: NEBBAS/MOY/HAU) |
| `arome_france` | Météo-France | deterministic | 0.025° (HD 0.01°) | 8 cycles/day→51h | Aug 10 18Z | Yes (SP2) |
| `aemet_harmonie` | AEMET | deterministic | 2.5km | all cycles→48h | Aug 10 18Z | **No** — total only |
| `gem_global` | ECCC (via Open-Meteo) | deterministic | ~15km | 00/12Z→240h | Aug 3 00Z | **No** — Open-Meteo-derived |
| `jma_gsm` | JMA (via Open-Meteo) | deterministic | ~55km | all cycles→264h | Aug 2 00Z | Yes (per doc pattern) |
| `cma_grapes_global` | CMA (via Open-Meteo) | deterministic | ~15km | all cycles→240h nominal | Aug 3 00Z | Unknown (verify) |

`open_meteo` isn't a model — it's `ukmo_global`'s primary path, `gem_global`/
`jma_gsm`/`cma_grapes_global`'s only path, every other model's sim-mode
backfill path (T16), and the generic JSON fetch mechanism all four
Open-Meteo-only models share (`open_meteo_fetcher.py`/`open_meteo_json`,
no per-model code).

## 2. Cloud L/M/H: native vs. derived

Three genuinely different situations, easy to conflate if you only look at
whether `cloud_cover_low/mid/high` comes back non-null:

- **Native, from the model itself**: `gfs`/`gefs_extended` (LCDC/MCDC/HCDC,
  param TCDC by level), `icon_global`/`icon_eu` (CLCL/CLCM/CLCH),
  `arpege_europe`/`arome_france` (SP2 package NEBBAS/NEBMOY/NEBHAU),
  `aifs_single`/`aifs_ens` (lcc/mcc/hcc), `jma_gsm` (per Open-Meteo's own
  doc pattern — no RH-derivation caveat on the surface field, unlike its
  pressure-level variant). `ukmo_global`'s is *probably* native (own field,
  not RH-derived by Open-Meteo) but the provenance flag is formally still
  `verify` — see `models.yaml`'s `ukmo_global.cloud.levels.provenance_note`.
- **Derived by THIS project's own code**: `ecmwf_hres` only. HRES's open
  data has no native L/M/H (only `tcc`), so `src/derive/humidity_to_cloud.py`
  computes it from `q`/`t` on 6 pressure levels (Murphy & Koop RH →
  Sundqvist 1989 cloud fraction → max-overlap), calibrated against GFS's own
  native split (T22).
- **Derived by Open-Meteo, upstream of this project**: `gem_global` (GEM's
  own pressure-level cloud is explicitly RH/Sundqvist-approximated per
  Open-Meteo's docs — the surface low/mid/high this project stores is
  transitively humidity-derived, never native GEM output).
  `cma_grapes_global`'s provenance is undocumented either way — flagged
  `verify`, not assumed derived or native.
- **Absent entirely**: `ecmwf_ens` (classic ENS) has no lcc/mcc/hcc anywhere
  in open data at any cycle — confirmed T01. `aemet_harmonie`'s open GeoTIFF
  feed carries one blended "nubosidad" total field only, confirmed T07(b)
  against AEMET's own live OpenAPI spec (zero numeric-model category).

## 3. Cloud-layer altitude/pressure boundaries (T38, 2026-07-23)

What "low cloud" physically means is NOT the same band across providers.
Confidence varies a lot and several widely-repeated numbers turned out to
be generic boilerplate rather than model-specific facts — see each model's
`cloud.levels.altitude` (or `cloud.altitude` for the ECMWF family) block in
`models.yaml` for full citations; summary:

| Model(s) | Low | Mid | High | Confidence |
|---|---|---|---|---|
| `gfs` | 0–3.7km | 3.7–8.1km | ≥8.1km (no enforced top) | **Confirmed** (NCEP UPP source + user guide) |
| `gefs_extended` | same as gfs | same | same | Inferred (shared UPP codebase, not independently restated) |
| `aifs_single`/`aifs_ens` | 0–2km | 2–6km | ≥6km | Confirmed for the shared IFS/ERA5 diagnostic; inferred that AIFS itself applies it (data-driven emulator, trained on the label, not shown to recompute it) |
| `icon_global`/`icon_eu` | 0–~2km | ~2–7km | ≥~7km (open top) | Confirmed (DWD ICON Database Reference Manual + corroborating mirror) |
| `arpege_europe`/`arome_france` | <2500m *above model terrain* | 2500–5000m | >5000m | Confirmed (official Météo-France glossary PDF — one shared spec for both models, no separate split published) |
| `ukmo_global` | 0–2.0km | 2.0–6.1km | ≥6.1km | **Likely, not confirmed** — sourced from Met Office's consumer website, not the UM's internal diagnostic (the real STASH fields are deprecated placeholders) |
| `gem_global` | 0–3km | 3–8km | ≥8km | Confirmed only as **Open-Meteo's own derived band** — not an ECCC-published GEM convention |
| `jma_gsm` | — | — | — | **Unverified** — Open-Meteo's "3km/8km" text for JMA is byte-identical generic boilerplate also on Open-Meteo's GFS page, not JMA-sourced. JMA's own docs confirm the fields exist but publish no boundary. |
| `cma_grapes_global` | — | — | — | **Unverified** — same boilerplate situation as JMA. GRAPES' own published cloud-scheme paper splits by model vertical-level index, not altitude/pressure. |
| `ecmwf_hres` (derived rows only) | ~0.11–1.46km | ~3.01–5.57km | ~9.16km (single level) | Not external research — this project's own `DEFAULT_LEVELS_HPA` bucketing, ICAO-standard-atmosphere converted |

Practical takeaway for anything that compares L/M/H across models
side-by-side (Tool 1/2/3, `run_evolution.py`, `site_ranking.py`): treat
cross-model "low cloud" agreement/disagreement as informative but not as
an apples-to-apples band comparison — GFS's "low" (up to 3.7km) is a
noticeably taller band than ECMWF's (up to 2km) or Météo-France's (up to
2.5km above *model* terrain, not sea level).

## 4. Rain + surface-temp fields (T37, 2026-07-23)

Every model this project archives already has real, verified rain and 2m-
temperature fields available at the exact same source/package it already
fetches cloud from — no new endpoints or auth needed for any of them. Not
yet wired into fetchers/extractors/`points.parquet` (a real, deliberate
follow-up scoping decision, same "confirm first, build separately" gate
T01–T09 went through for cloud). See each model's `rain`/`surface_temp`
blocks in `models.yaml` for the exact param names/units per model — the
one recurring gotcha worth flagging here: **units are not consistent
across providers even for the identical physical quantity** (ECMWF HRES/ENS
report precipitation in meters water-equivalent, AIFS in kg/m² — same
quantity, different scale factor; ECMWF's `tcc` is a [0,1] fraction on
HRES/ENS but [0,100] percent on AIFS — this exact "same variable name,
different units" trap already burned the cloud-field work once, don't
assume it can't happen again for rain/temp).

## 5. Known permanent field gaps

Every one of the 3 viz tools (`tool1_real.html`/`tool2_real.html`/
`tool3_real.html`) hardcodes the same `KNOWN_FIELD_GAPS` table so the UI can
tell "genuinely never available" apart from "not fetched yet" — worth
keeping in sync here rather than only in three separate JS files:

- `arome_france`/`arpege_europe`: no native **total** cloud field in the
  SP2 package this project fetches (SP1 has NEBUL/total but isn't fetched;
  only SP2's low/mid/high). Total for these two is simply absent, not a bug.
- `ecmwf_ens` (classic ENS): no low/mid/high split at all — total (`tcc`)
  only. The L/M/H split lives on `aifs_ens`, a different ECMWF product.
- `ecmwf_hres`: no native low/mid/high either — only `tcc` native; its
  low/mid/high is the derived path described in §2/§3.
- `aemet_harmonie`: total only, no low/mid/high in the open GeoTIFF feed.

## 6. Eclipse-aware radiation schemes (T09, 2026-07-22)

Only **ECMWF's IFS** (`ecmwf_hres`/`ecmwf_ens`, IFS Cycle 50r1, operational
since 2026-05-12) is confirmed to simulate the Aug 12 eclipse's solar
obscuration in its own radiation scheme (documented local 2m cooling up to
7°C, reduced boundary-layer wind, strongest in fair-weather/high-sun
conditions — ECMWF Newsletter #181). `aifs_single`/`aifs_ens` (no explicit
radiation scheme, pure data-driven), `icon_global`/`icon_eu`, `ukmo_global`,
and `gfs`/`gefs_extended` are all expected eclipse-blind — absence of
evidence, moderate-high confidence, not literature-proven negatives.
Practical effect: expect a genuine, physically-driven low-cloud/cooling
signal from ECMWF's two models around 18:30Z that no other model in this
registry is expected to independently reproduce — a known physics-inclusion
difference, not a forecast-skill disagreement, and worth flagging directly
in any UI that shows cross-model spread at T. DWD has twice shipped
one-off, non-operational eclipse test products in the past (2015, Oct
2022) — worth re-checking `opendata.dwd.de`'s news page in early Aug 2026.

## 7. Ensemble usage: maps vs. point views

`aifs_ens` (and, once its own L/M/H situation changes, `ecmwf_ens`) serves
two structurally different views, deliberately handled differently:

- **Maps** (Tool 1/2/3, `tool1_renderer.py`): rendered as the **ensemble
  mean** across all members, substituted in wherever a single
  representative field would otherwise go — this applies uniformly to
  whichever quantity (total/low/mid/high) is currently selected, it is not
  a separate dropdown entry. A genuinely new quantity, **`prob_clear`**
  (P(cloud_low < 20%) per grid cell, same threshold as `site_ranking.py`'s
  own pooled point metric), is built and live in all 3 tools' dropdowns —
  only `aifs_ens` has real per-member native low-cloud data to compute it
  from; every other model shows an honest "needs an ensemble with native
  low cloud" gap message rather than a broken image.
- **Point/ensemble-graph views** (`run_evolution.py`, `site_ranking.py`):
  full per-member spread is kept — p10–90 band + median for run-evolution
  trajectories, pooled Bernoulli samples across all contributing
  models/members for `site_ranking.py`'s P(cloud_low<20%) site ranking.

## 8. Retention windows (why the archiver comes first)

Hard Constraint #1 exists because of this table — a missed run for the two
(!) rows below is gone forever, no re-fetch possible:

| Source | Retention |
|---|---|
| NOAA (GFS/GEFS, AWS) | permanent |
| ECMWF (HRES/ENS/AIFS) | ~2–3 days (12 most recent runs, ~72h) |
| Météo-France (data.gouv.fr mirror) | ~14 days |
| DWD (ICON Global/EU) | **~24h (!)** — empirically confirmed, no DWD-published SLA |
| AEMET (HARMONIE) | **latest run only (!)** — no historical archive exposed at all |

## 9. Real disk footprint (T35, measured 2026-07-23)

The original CLAUDE.md estimate ("well under 1GB") assumed a crop-before-
archive step that was never built, and only 3 eclipse-hour steps per run.
Neither held once the archiver was consolidated onto full-range fetching
for every model (§10): **measured 48GB across just 2 runs × 10 gridded
models**. `aifs_ens` alone (50-member ensemble × 4 cloud fields × ~60
steps) is ~16GB **per run** — ~64GB/day at its own 4-cycles/day cadence if
kept indefinitely. Comfortably fine on the dev desktop (900GB+ free,
explicitly not disk-constrained during development), but a hard input for
the production box's discard-after-render pipeline sizing (see
`TASKS.md`'s "Tool 1/2/3 rollout" step 4) and for how much of an in-flight
`aifs_ens` run needs to fit even transiently before its raw file can be
deleted.

## 10. Architecture: unified full-range archiver (T35)

Every fetcher now archives the **full available forecast range** for every
due run into one `data/raw/{model}/{run_init}/` tree — not just the eclipse
day's 15/18/21 UTC steps. This replaced an earlier narrow "eclipse-cropped"
fetch path that ran in parallel; it was retired 2026-07-23 once it became
clear the full archive alone was sufficient for everything (Tool 1/2/3's
own "every step, every run" needs made the narrow path pure duplication).
Point-extraction into `data/points.parquet` (the Gantt/run-evolution/site-
ranking numeric pipeline) remains eclipse-hour-scoped — `steps_for_run()`
still picks which archived steps feed that table; only the *raw archive*
became unconditionally full-range. `config/models.yaml` is what every
fetcher, the scheduler, and both pipelines read for cycles/steps/lag/URLs —
see hard constraint #2.

## 11. Viz suite (Tool 1/2/3)

Three widgets share one rendering/manifest pattern
(`src/viz/tool1_renderer.py`, `render_frame()`), differing only in which
(model, run, step) combinations they render:

- **Tool 1** — every model's latest run, every step. "What's each model
  saying right now."
- **Tool 2** — one model's several most-recent runs, every step of each.
  "How has this one model's own view evolved run-over-run."
- **Tool 3** — every model's several most-recent runs, one step each (the
  one nearest the eclipse valid time), one shared cursor across all rows
  selecting which run each model shows. "Cross-model agreement at T, as of
  each model's various recent runs."

See `TASKS.md`'s "Viewing Tool 1/2/3 locally (dev desktop)" section for how
to actually open them (a local HTTP server, never `file://`).

## Where to look for more

- **`config/models.yaml`** — every exact number, every citation-bearing
  `note:`/`altitude:` field, per-model `status: confirmed|verify`.
- **`TASKS.md`** — the full chronological research/build log (T00–T39),
  including every dead end and correction that led to the numbers above.
- **`CLAUDE.md`** — hard constraints, repo layout, human-in-the-loop items.
