# Eclipse Cloud Forecast Tool — Model Fetch Plan

**Target event:** Total solar eclipse, 2026-08-12, totality over Spain ≈ 18:25–18:33 UTC (working value **T = 18:30 UTC**).
**Today:** 2026-07-22 → T−21 days. First long-range model coverage begins in **5 days**.

Two physical facts that shape the whole tool:

1. **Low sun.** Totality in Spain happens ~1 h before sunset, sun elevation ≈ 10–12°, azimuth ≈ WNW. Cloud along the *sightline toward the west-northwest horizon* matters as much as overhead cloud. Design consequence: extract not just the site pixel but a small strip extending ~50–100 km WNW of each candidate site.
2. **Evening convection decay.** August afternoon cumulus over Iberia typically decays toward evening; the eclipse's own radiative cooling accelerates that. Models differ in whether they simulate the eclipse in their radiation scheme at all (research item #11) — a systematic reason forecasts may disagree on low cloud right at T.

---

## 1. Phase timeline — what to fetch when

| Phase | Dates (2026) | Lead | Models active | Purpose |
|---|---|---|---|---|
| 0. Baseline | now → Jul 27 | T−21…−16 d | GEFS 35-day (already covers T!), cloud climatology (eclipsophile / ERA5) | Prior probabilities per region; build & test pipeline |
| 1. Long range | Jul 27 → Aug 3 | T−16…−10 d | GFS, GEFS, ECMWF ENS (tcc), AIFS single + AIFS-ENS | Synoptic pattern: fronts, trough position, region-scale odds. Treat as probabilistic only |
| 2. Medium range | Aug 3 → Aug 7 | T−10…−5 d | + ECMWF HRES (tcc + derived L/M/H), ICON Global, UKMO Global | Deterministic pattern lock-in; region/lodging decision window |
| 3. High-res regional | Aug 7 → Aug 11 | T−5…−1.5 d | + ICON-EU, ARPEGE Europe, AROME (E half of path), AEMET HARMONIE | Terrain-resolved L/M/H; site shortlist; primary comparison window |
| 4. Nowcast | Aug 11 → T | T−36 h…0 | Every run of phase-3 models + Meteosat imagery, AEMET obs/radar | Final go/no-go and drive decision; NWP hands over to satellite trends ~T−6 h |

---

## 2. Master model table

Steps column = output cadence over lead-time ranges. "First covers T" = earliest run whose valid times reach Aug 12 18:00 UTC (18:30 falls between the 18:00 and next steps; interpolate). Add publication lag (~3–9 h, per notes) for wall-clock data-on-disk time. Dates marked ~ depend on cycle-length verification.

| Model | Provider | Grid | Steps | Max extent | Cycles (UTC) | First run covering T | Lead | Native L/M/H cloud? |
|---|---|---|---|---|---|---|---|---|
| GEFS ext. (31 mem) | NOAA | 0.5° (0.25° subset) | 3 h ≤240; 6 h ≥240 | 840 h (00Z only) | 00 (+06/12/18 to 384 h) | **Jul 9 00Z — already live** | T−35 d | TCDC yes; L/M/H = research #3 |
| GFS | NOAA | 0.25° | 1 h ≤120; 3 h ≤384 | 384 h | 00/06/12/18 | **Jul 27 18Z** | T−16 d | **Yes** (LCDC/MCDC/HCDC) |
| ECMWF ENS (50 mem) | ECMWF | 0.25° | 3 h ≤144; 6 h ≤360 | 360 h (00/12) | 00/06/12/18 (06/18→144 h) | **Jul 29 00Z** (tcc: 12Z run) | T−14.5 d | No — tcc only (12Z; verify #1) |
| AIFS single / AIFS-ENS | ECMWF | 0.25° | 6 h | 360 h | 00/06/12/18 (verify #2) | **~Jul 28 18Z–Jul 29 00Z** | T−15…−14.5 d | Likely yes (lcc/mcc/hcc; verify #2) |
| ECMWF HRES (oper) | ECMWF | 0.25° (native 9 km via Open-Meteo) | 3 h ≤144; 6 h ≤240 | 240 h (00/12) | 00/06/12/18 (06/18→90 h) | **Aug 3 00Z** | T−9.7 d | No — tcc (12Z, verify #1); L/M/H derivable from q on 9 p-levels |
| ICON Global | DWD | ~13 km icosahedral | 1 h ≤78; 3 h ≤180 | 180 h (00/12) | 00/06/12/18 (06/18→120 h) | **Aug 5 12Z** | T−7.3 d | **Yes** (CLCL/CLCM/CLCH) |
| UKMO Global | Met Office | ~10 km | 1 h ≤54; 3 h ≤144; 6 h ≤168 | ~168 h | 00/06/12/18 (lengths: verify #7) | **~Aug 6 00Z** | T−6.8 d | **Yes** (lcc/mcc/hcc) |
| ICON-EU | DWD | 0.0625° (~7 km) | 1 h ≤78; 3 h ≤120 | 120 h | 00/06/12/18 (+short 03/09/15/21) | **Aug 7 18Z** (hourly-at-T from Aug 9 18Z runs) | T−5 d | **Yes** (CLCL/CLCM/CLCH) |
| ARPEGE Europe | Météo-France | 0.1° | 1 h near, 3 h far (verify #6) | 102–114 h by cycle | 00/06/12/18 | **~Aug 8** (00Z or 12Z per cycle lengths) | T−4.3 d | **Yes** |
| AROME / AROME-HD | Météo-France | 0.025° / 0.01° | 1 h | 42–51 h by cycle | 00/03/06/12/18 (verify #6) | **~Aug 10–11** | T−2 d | **Yes** — but domain covers only NE Spain + Balearics (verify edge, #6) |
| AEMET HARMONIE-AROME | AEMET | 2.5 km (open product 0.025°) | 1 h (open cadence: verify #8) | 48 h | 00/06/12/18 | **Aug 10 18Z** | T−2 d | **No in open feed** — total "nubosidad" only (verify #8) |
| Open-Meteo (aggregator) | — | per model | 1 h (interpolated) | per model | per model | per model above | — | Serves L/M/H for all, but **derived from humidity where the source lacks it** — flag per model (#9) |

Excluded: MEPS (Nordic domain only), ICON-D2 (central Europe), HRRR/NBM (US). Optional extras via one Open-Meteo call if you want more independent opinions: GEM (Canada), JMA GSM, KMA, CMA — global, coarse, low marginal value.

## 3. Per-model notes

**GEFS extended** — the only NWP already covering T today. At day 20+ skill ≈ climatology; use it as "is the pattern signal drifting from climatology yet", nothing more. 0.5° beyond day 16.

**GFS** — your long-range workhorse: earliest deterministic coverage, native L/M/H, hourly output inside 120 h, permanent AWS archive (you can backfill old runs — unique among these sources). Known biases over Iberia in summer: tends to overdo convective cloud. Publication lag ≈ +3.5–5 h.

**ECMWF ENS** — best medium-range skill of anything here, but open data gives total cloud only, one cycle. Use the 50-member tcc distribution as your probability anchor (P(tcc < 30 %) per site) while GFS/ICON supply the level split. Lag ≈ +7–9 h.

**AIFS** — potentially the sleeper: ECMWF-grade skill *with* L/M/H params in the open feed (verify), 4 cycles/day. AI models have known smoothing of extremes; cloud fields tend toward the mean — compare, don't trust alone. Also unclear whether AIFS radiation "sees" the eclipse (#11).

**ECMWF HRES** — deterministic reference from T−10 d. The humidity-derived L/M/H module you build for it (Murphy & Koop style, from q at 1000–300 hPa) doubles as a validation tool: run it on GFS too and compare against GFS's native fields to calibrate trust in derived values.

**ICON Global / ICON-EU** — native L/M/H with clean layer definitions (surface–800 hPa, 800–400, 400–0). EU nest at 7 km resolves Iberian orography meaningfully; from Aug 7 it's a primary source. Global comes on the icosahedral grid — check for regular-grid files first (#4), else CDO remap. Lag ≈ +2.5–4 h. Files are per-parameter per-timestep .bz2 — many small downloads; batch them.

**UKMO Global** — independent model family (UM), decent 10 km grid. Open feed carries +4 h extra delay; DataHub free tier is 1 GB/month which is workable only with tight per-parameter orders (#7).

**ARPEGE Europe** — good mid-tier: 11 km, native L/M/H, cycle lengths vary confusingly by run hour. AWS open bucket (anonymous) avoids the Météo-France token dance; 14-day retention there.

**AROME** — only relevant if you're targeting the eastern half of the path (Zaragoza → Castellón → Balearics). If the domain edge checks out (#6), its 1.3–2.5 km orographic cloud handling is the best available for that stretch. Ignore for Galicia/Asturias/León.

**AEMET HARMONIE-AROME** — the home-team model, tuned for exactly this terrain, and AEMET explicitly rates it strongest on fog/low cloud over Spanish orography. Open feed is GeoTIFF (not GRIB) and total-cloud only, so treat it as a high-trust cross-check layer and fog specialist rather than a full L/M/H source — unless #8 turns up GRIBs. Also check AEMET's dedicated eclipse-2026 pages; they may publish exactly the product you're building.

**Climatology baseline** — before any model covers T, and as the reference all forecasts get compared against: eclipsophile.com's 2026 cloud climatology (Jay Anderson) + Aug-evening cloud stats from ERA5 (Open-Meteo historical API). The tool's phase-1 display is essentially "model minus climatology".

---

## 4. Sources & access

| Provider | Endpoint | Auth | Format | Retention (verify #10) |
|---|---|---|---|---|
| NOAA GFS/GEFS | AWS `noaa-gfs-bdp-pds` / `noaa-gefs-pds`; NOMADS filter CGI | none | GRIB2 + .idx | AWS: permanent; NOMADS ~10 d |
| ECMWF (IFS + AIFS) | `data.ecmwf.int/forecasts` + AWS/GCS/Azure mirrors; `ecmwf-opendata` pip | none | GRIB2 + .index | ~4 d rolling |
| DWD ICON | `opendata.dwd.de/weather/nwp/icon{-eu}/grib/<HH>/<param>/` | none | GRIB2 .bz2 | ~24 h (!) |
| Météo-France | AWS registry "meteo-france-models" bucket; or portail-api.meteofrance.fr | AWS: none; portal: token | GRIB2 | AWS ~14 d |
| Met Office | Open-Meteo `/v1/ukmo`; or DataHub atmospheric orders | Open-Meteo: none; DataHub: key | JSON / GRIB2 | — |
| AEMET | `opendata.aemet.es` (free key) + `aemet.es/es/api-eltiempo/modelos/download/harmonie/PB` | key (main API) | GeoTIFF/GeoJSON | latest run only (!) |
| Open-Meteo | `api.open-meteo.com/v1/forecast?models=…&hourly=cloud_cover_low,cloud_cover_mid,cloud_cover_high` + Previous Runs API + Historical Forecast API | none (non-commercial) | JSON | previous-runs archive: per-model, #9 |

The (!) rows are why the archiver comes first: DWD and AEMET keep essentially one day of runs. Miss a run, it's gone.

## 5. Software stack

- **Python 3.12+**, uv or venv
- **herbie-data** — idx-based byte-range subsetting for GFS/GEFS/ECMWF/GraphCast; avoids 500 MB full-file downloads (a L/M/H Iberia slice is a few MB)
- **ecmwf-opendata** — ECMWF + AIFS retrieval by MARS-style request
- **eccodes + cfgrib + xarray** — GRIB decode into labelled arrays
- **wgrib2** — quick inspection and `-small_grib` box subsetting of NOAA files
- **cdo** — only if ICON Global needs icosahedral→regular remap (#4)
- **rasterio** — AEMET GeoTIFF ingestion
- **httpx/requests + bz2** — DWD fetcher (many small files; parallelize politely)
- **pandas/polars + Parquet** (duckdb optional) — per-run per-site extractions
- **matplotlib/plotly** — Gantt + run-evolution charts; UI layer later (static site or FastAPI)
- **cron/systemd timers** on an always-on box (fresnel would do) + a dead-man's-switch ping (healthchecks.io style) — a silently dead archiver during Aug 5–12 is the worst failure mode

## 6. Architecture: archiver first, registry-driven

Single source of truth: `models.yaml` — per model: cycles, lengths per cycle, step layout, publication lag, source URL template, param names, native-vs-derived flag, domain polygon. The fetcher, the availability Gantt, and the UI all read this one file.

Per run, store two things:
1. **Iberia-box GRIB/GeoTIFF slices** (36–44° N, 10° W–5° E) for valid times 15, 18, 21 UTC Aug 12 (15 UTC supports the WNW-sightline and trend views) — KB–MB per run per model.
2. **Point extractions** for N candidate sites + their WNW strips → Parquet rows: `(model, run_init, member, site, valid, cloud_low, cloud_mid, cloud_high, cloud_total, source_flag)`.

Sites are config, not code — placeholder list along the centerline: Luarca, León, Burgos, Logroño, Zaragoza, Castellón, Palma. (Get the real totality polygon — research #12 — before finalizing.)

## 7. Availability research checklist

1. **ECMWF open-data params now:** pull one `.index` file each from `oper` and `enfo` 00Z and 12Z; grep for `tcc`, `lcc`, `mcc`, `hcc`. Settles which cycles carry cloud and whether L/M/H got added since the Nov announcement.
2. **AIFS:** same index-grep on `aifs-single` and `aifs-ens`; confirm cycles, step layout, 360 h on all runs, presence of lcc/mcc/hcc.
3. **GEFS cloud levels:** which product family carries LCDC/MCDC/HCDC (pgrb2a vs pgrb2b vs pgrb2s; 0.25° vs 0.5°), and whether they persist beyond 384 h in the extended run.
4. **ICON Global grid:** does opendata serve regular-lat-lon files for CLC*, or native icosahedral only (→ CDO + grid-description files)?
5. **ICON-EU-EPS:** is the ensemble on opendata.dwd.de with cloud fields? Members, resolution. Would be a nice mid-range probabilistic layer with real L/M/H.
6. **Météo-France:** exact AWS bucket layout; which package (SP2/HP1) holds L/M/H cloud; per-cycle lengths for ARPEGE (102/72/114/60?) and AROME (42/48/51?); AROME domain southern edge vs the totality strip.
7. **UKMO:** open-data cycle lengths; whether DataHub orders can subset by area (else 1 GB/month is tight); actual delay via Open-Meteo.
8. **AEMET:** register OpenData key; is HARMONIE available as GRIB anywhere or GeoTIFF only; full field list (any cloud-by-level?); update cadence of the api-eltiempo download; any official eclipse-2026 forecast products.
9. **Open-Meteo Previous Runs API:** which of these models × how far back; per-model whether cloud_cover_low/mid/high is native or humidity-derived (their docs state it) — store that flag with every row.
10. **Retention windows** per source, to finalize archiver priorities (DWD and AEMET first).
11. **Eclipse in the radiation scheme:** which models simulate the Aug 12 solar obscuration (ECMWF IFS reportedly has since ~2015; ICON/UM/GFS/AIFS unknown). Affects trust in low-cloud/convection evolution at 17–19 UTC.
12. **Totality path polygon** (Besselian elements; Xavier Jubier's KMZ) for map overlay and site validation.

## 8. Deferred actions

### 8a. Availability visualization

Horizontal timeline, Jul 25 → Aug 13: one row per model, bar begins at first *data-on-disk* moment covering T (init + publication lag), tick marks at each subsequent run, opacity or color encoding step-resolution at T (3 h vs 1 h), vertical line at T. Render straight from `models.yaml` with matplotlib (static SVG first, plotly later). Secondary readout: "runs remaining before T" counter per model — that's the number that tells you how many more chances each model gets to change its mind.

### 8b. Simulated eclipse time for UI testing

Make T a config value (`ECLIPSE_T` env var); nothing in the UI hardcodes the date. Two test modes:

- **Time-shift (available today):** set T_sim to a *past* 18:30 UTC (e.g., a week ago) and backfill the full multi-model run history from the Open-Meteo Previous Runs / Historical Forecast APIs. This gives you a complete 16-day, all-model dataset immediately — the whole run-slider can be built and exercised before Jul 27 without waiting for real data.
- **Live-forward:** set T_sim ≈ now + 3–5 d and let the real archiver run against it. This is the end-to-end test of every GRIB fetch/parse path (which mode 1 bypasses). Run it during Phase 0–1 so source-specific parsing bugs surface while stakes are low.

Switching sim → real is one env change.

### 8c. Run-evolution view (the 2024-style slider)

Forecasters call this d(Prog)/dt — fixed valid time, varying initialization. Design:

- **Fixed valid time = T**; primary control = slider over run init times (per model, or a global time axis where each model's runs snap to their cycle times).
- **View 1 — map:** Iberia, chosen model + run, L/M/H as three toggleable layers or an RGB-ish composite, totality path + sites overlaid. Data source: the archived Iberia-box slices.
- **View 2 — per-site trajectory (the money plot):** x = run init time, y = cloud %, three lines (low/mid/high) per model panel; small-multiples grid across models; ensemble models drawn as spaghetti or 10–90 % band around the median. A forecast that's *converging* across runs and *agreeing* across models is the signal you're hunting; this chart shows both at a glance.
- **Prerequisite:** every run archived from its first covering cycle — impossible to reconstruct later for DWD/AEMET/ECMWF sources. Hence archiver-first.
- Site selector doubles as the decision tool in Phase 3–4: sort sites by latest P(low cloud < 20 %).

## 9. Milestones

| Date | Deliverable |
|---|---|
| Jul 22–24 | Research checklist #1–#10 (each is one index-file or docs check); `models.yaml` v1; archiver skeleton; start GEFS-extended pulls + Open-Meteo previous-runs probe |
| Jul 25–26 | Time-shifted T_sim dataset backfilled; run-slider prototype against it; Gantt v1; GFS parse path tested on a dummy date |
| **Jul 27 (eve)** | **First real GFS run (18Z) covering T lands — real archive begins** |
| Jul 29 | ECMWF ENS + AIFS ingest live; derived-L/M/H module drafted |
| Aug 3 | HRES online; derived-vs-native calibration (GFS both ways) |
| Aug 5–8 | ICON Global (5th) → UKMO (6th) → ICON-EU (7th) → ARPEGE (8th) come online in sequence |
| Aug 9–10 | Run-slider on real archive; site shortlist from ensemble probabilities |
| Aug 10–11 | HARMONIE + AROME ingest; fetch cadence to every-cycle; #11 findings annotated in UI |
| Aug 12 | Nowcast mode: Meteosat loops + AEMET obs beside the final NWP runs; site call by ~15 UTC, on the road by ~16 UTC |
