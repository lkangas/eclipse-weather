# `points.parquet` rebuild — plan

Status: **draft, awaiting review. Nothing below is implemented yet** except
where explicitly marked "already written, uncommitted" — those are code
changes sitting in the working tree from before this plan was requested;
they match this document's schema/logic but are held pending your review of
this plan, not deployed or committed.

## Why a rebuild, not a patch

`points.parquet` (the numeric per-site extraction, separate from the raw
GRIB2/GeoTIFF archive and from the rendered map PNGs) accumulated several
real problems that a patch can't cleanly fix:

- Extraction only ever kept 3 valid times per run (15/18/21 UTC on the real
  eclipse day) — a scope built for one specific chart (Tool 2's run-evolution
  view) that quietly became the only thing the whole file could do, even
  though the raw fetch has covered each run's *full* forecast range since the
  2026-07-23 archiver consolidation.
- A bug (`mark_extracted()` fired even when an extractor legitimately wrote
  zero rows — e.g. a short-range model's run couldn't reach the real eclipse
  date yet) permanently blocked several models' runs from ever being
  re-extracted, including after the scope bug above got fixed.
- No temperature field at all, despite every model's 2 m temp already being
  fully researched (param/package/level) for the *map-rendering* path
  (`frame_renderer.py`) — point extraction never picked it up.
- The file carries dead rows under old, pre-2026-07-27 model names
  (`gfs_global`, `ecmwf_ifs025`, `om_icon_eu`, `om_icon_global`,
  `meteofrance_arpege_europe` — leftover T16 backfill-only ids) alongside the
  live ones, and a handful of sites from the old 7-site shortlist that the
  29-place list replaced.

None of the existing data is needed. The plan is to wipe it and rebuild under
corrected logic, from raw data already sitting on disk — no new fetching
required for the desktop side.

## 1. Schema

```
model:        str
run_init:     datetime[UTC]
member:       int             (-1 = deterministic; real ensemble member index otherwise)
site:         str
valid:        datetime[UTC]
cloud_low:    float | None    (percent 0-100)
cloud_mid:    float | None
cloud_high:   float | None
cloud_total:  float | None
temp_c:       float | None    (2 m air temperature, degrees C)
provenance:   str             (native | derived | total_only)
fetched_at:   datetime[UTC]
```

Same as today's schema plus `temp_c`. `PointRow` in `src/extract/base.py`
already carries this field (uncommitted change) — default `None` so it's
optional per-row, since several models genuinely have no temperature (see
below).

**Explicitly out of scope, by request:** rain. It exists in the map-rendering
layer (GFS only, an instantaneous-rate quirk — see `TODO.md`'s "Rain and
temperature" entry) but was never asked for here and isn't part of this
rebuild.

## 2. Which points

Unchanged: `all_sample_points()` — the 29 curated places in
`config/placenames.json` plus each place's 4-point WNW sightline strip
(bearing 285°, 25/50/75/100 km) = up to 145 sample points per model, fewer
where a model's grid doesn't reach a point (e.g. `arome_france`'s southern
edge).

**`aemet_harmonie` is excluded entirely, by explicit direction** — no rows
in `points.parquet` at all, not even `cloud_total`. Reasoning: it's already
the weakest signal in the registry (no L/M/H, and its "numeric" value is
recovered by inverting a rendered color-ramp legend, not read from a real
field), and giving it temperature would need a *second*, independently
uncertain color-ramp inversion (different file, different legend, unconfirmed
without live investigation) that isn't worth building. Its raw GeoTIFF fetch
and map rendering are unaffected — only `points.parquet` extraction skips it.

## 3. Model roster and temperature coverage

| model | in points.parquet? | native L/M/H | temp_c source | notes |
|---|---|---|---|---|
| gfs | yes | yes | `f{step}_temp.grib2` (TMP 2m) | |
| gefs_extended | yes | yes | `f{step}_c00_temp.grib2` | member = real GRIB `number` (0=control) |
| ecmwf_hres | yes | no (total only) | `temp_f{step}.grib2` (2t/t2m) | humidity-derived L/M/H deliberately not used (~0.35 correlation vs native total, see `ecmwf_extractor.py`) |
| ecmwf_ens | yes | no (total only) | **none** — `surface_temp.enabled: false` in `models.yaml`, a deliberate cost opt-out (~11 GB/day for a field only ever averaged) | `temp_c` will be `None` for every row unless that opt-out is reversed |
| aifs_single | yes | yes | `temp_f{step}.grib2` | |
| aifs_ens | yes | yes | **none**, same opt-out as ecmwf_ens | |
| icon_eu | yes | yes | `T_2M` (already fetched, just not previously extracted) | |
| icon_global | yes | yes | `T_2M`, via the same cdo remap as cloud | remap is the slow part of this model's extraction — see §7 |
| arome_france | yes | yes (no native total) | SP1 package `2t` (SP2, which cloud comes from, carries *skin* temp — confirmed wrong in an earlier pass, see `T45` in `models.yaml`) | |
| arpege_europe | yes | yes (no native total) | SP1 package `2t`, same as arome_france | |
| ukmo_global | yes | verify (Open-Meteo, no explicit native/derived doc statement) | `temperature_2m` via Open-Meteo | primary path is Open-Meteo's live endpoint, not raw GRIB |
| gem_global | yes | derived (RH-approximated, doc-confirmed) | `temperature_2m` via Open-Meteo | |
| jma_gsm | yes | native (doc-confirmed) | `temperature_2m` via Open-Meteo | |
| cma_grapes_global | yes | verify | `temperature_2m` via Open-Meteo | separately caveated: CMA's backend has been unreliable, real horizon often short of the documented 240h |
| aemet_harmonie | **no** | n/a | n/a | excluded entirely, see §2 |
| gfs_global, ecmwf_ifs025, om_icon_eu, om_icon_global, meteofrance_arpege_europe | **recommend: no** | — | — | dead T16 backfill-only ids, pre-dating the 2026-07-27 rename; no longer fetched. Proposal: drop from the rebuild rather than carry them forward. **Needs your confirmation** — if you want them kept for historical backfill reference, say so and they stay in scope. |

## 4. Desktop rebuild procedure

**Revised per explicit direction: NOT a historical backfill.** The rebuild
seeds `points.parquet` with only each model's single **newest** run, verified
against the VPS as ground truth — not "walk every run ever archived." Once
that seed is in, the file grows the normal way: the running scheduler
extracts each subsequent new run as it lands, same as it always has.

Why "newest on disk" isn't good enough by itself, demonstrated for real
today: the desktop archiver got stuck on GFS for ~18 hours (NOAA S3 503s/
connection resets, compounded by the scheduler's single-threaded fetch loop
blocking on the slow model), so "the newest run_init directory under
`data/raw/gfs/`" was quietly a stale run, not the actual newest one GFS had
published — the VPS had already fetched the real newest run while the
desktop was still behind. A rebuild step that just picks the newest local
directory would silently seed `points.parquet` from stale data and have no
way to know it. Same root problem, different shape, as the corrupted-run
picking bug found earlier today in the prototype scripts
(`point_timeseries.py`'s `_pick_run_init()`) — worth treating as a general
lesson, not just fixing here: **anywhere this codebase picks "the newest
run" for anything, it needs a real freshness check, not an assumption that
the newest thing on local disk is the newest thing that exists.**

Procedure:

1. Delete `data/points.parquet`.
2. Delete every `.extracted` marker under `data/raw/*/*/`.
3. For each model: ask the VPS what its own newest fetched run is (source of
   truth — see open question below), and compare against the desktop's
   newest run on disk.
   - If the desktop already has that same run_init fetched (and it's not
     corrupted/partial — reuse the good-fraction file-integrity check
     already built for this in the prototype scripts, not just "a directory
     exists"), extract it.
   - If the desktop is behind, **fetch that run first**, then extract it.
     Do not extract an older run just because it's what's currently on
     disk.
4. From that point on, the running scheduler (`src/scheduler/run.py`)
   extracts each new run automatically as it lands, same as it does today.

**Open question — how does the desktop actually query VPS status?** I don't
currently have a mechanism for this and don't want to guess one. Possible
answers: the VPS already exposes something (`src/pipeline/`'s
`rendered_index.json`/`pipeline_status.json` are mentioned in `TODO.md` as
already-existing data, just without a viewer page yet — if reachable from
the desktop, e.g. over the network or synced some other way, that could
serve directly) or this needs a small new piece of plumbing. Machine/network
specifics belong in private ops notes, not this doc — but I need at least
the shape of the answer (is there already a URL/file the desktop can read,
or does one need building?) before writing the "ask the VPS" step for real.

**Known coverage gap, not a bug:** temperature fetching itself (the raw
`temp_f*.grib2`/`T_2M`/SP1 files, as opposed to extracting it) was only
built starting 2026-07-27 (`TODO.md`'s "Rain and temperature" entry). A run
older than that has no temp file to read at all. Low risk now that the seed
is "newest run only" rather than a deep backfill, but worth knowing if a
model's newest run still somehow predates that.

**Also already fixed (uncommitted), still needed:** `mark_extracted()` in
`src/scheduler/run.py` no longer fires when an extractor returns zero rows.
Without this, several models' currently-stuck runs
(`ecmwf_hres`/`icon_eu`/`icon_global`/`arome_france`/`arpege_europe` all
have runs with real raw data on disk but were marked "done" with nothing
extracted, under the old 3-hour-only logic) stay stuck even after step 3
fetches/finds a genuinely current run - the *next* run after that would hit
the same bug again without this fix.

## 5. VPS deployment

**This is the part with a real open question — see the flag below before
implementing anything here.**

Per `TODO.md`'s already-decided shape (2026-07-27): the VPS is a *separate,
self-sufficient* fetch → render → extract → delete pipeline
(`src/pipeline/`, not `src/scheduler/run.py`). It does **not** receive a copy
of the desktop's `points.parquet` — it produces its own, independently, and
that copy (not the desktop's) is the operational one. This plan doesn't
change that shape; it just means the same schema/extraction-logic fixes need
to reach `src/pipeline/` too, not just the desktop scheduler.

Mechanically, this is simpler than it sounds: `src/pipeline/orchestrator.py`
already calls into the *same* `src/extract/registry.py` extractor functions
the desktop uses (`_maybe_extract()` → `extract_registry.get_extractor(...)`).
Landing the schema/extractor changes in `src/extract/*.py` and
`src/extract/base.py` (this repo's normal code, deployed to the VPS the same
way the rest of the codebase is) automatically gives the VPS pipeline
full-range + temperature extraction too — no VPS-specific extraction code to
write.

**The real open question:** `src/pipeline/orchestrator.py`'s own
"is this run ready to extract" gate (`verify.py`'s `extraction_ready`) is
*also* built around the old 3-eclipse-hour concept — it checks that the run
has reached those 3 specific archive hours, not "the full range is on disk."
That gate exists for a real reason unrelated to the desktop's bug: the VPS
**deletes raw incrementally as steps get rendered**, so by the time a run's
*last* step arrives, its *first* steps may already be gone. A full-range
extraction pass needs every step still present at once — which may not be
true under the VPS's own reclaim timing. Three ways to resolve this, not
mine to pick:

  a. Extract **incrementally, per step**, right after each step renders and
     before that step's raw is reclaimed — appending one valid time's worth
     of rows at a time instead of one run's worth at once.
  b. Extract full-range only for runs whose *entire* horizon fits inside the
     window before reclaim starts (i.e. tighten reclaim's timing, not
     extraction's).
  c. Keep the VPS on eclipse-hours-only extraction (its original, narrower
     purpose) and treat full-range curves as a desktop-only capability, per
     `TODO.md`'s own framing: *"The desktop keeps archiving raw purely as a
     convenience for the occasional 'what if I plotted X' ... a rare path,
     not an operational dependency."*

Also unaddressed on the VPS side, same bug as the desktop had:
`orchestrator.py`'s `_maybe_extract()` calls `mark_extracted()`
unconditionally too (line ~210) — needs the same zero-rows guard.

Sequencing, regardless of which option above gets picked: **VPS changes
don't start until the desktop rebuild is verified correct** (per your own
"once we are certain it works" from earlier) — this section is written now
so the open question is visible while reviewing, not because VPS work is
about to start.

## 6. Browser progress reporting

New. Follows the pattern this project already uses for other long-running
work (`src/viz/web/backfill_progress.html`, `render_status.html`): the
rebuild script writes a small JSON status file after every `(model, run)`
pair it finishes, and a plain HTML+JS page polls that file and renders it —
no server-side code beyond the existing local static file server
(`http.server`, viewed at `http://localhost:8734/...` per this project's own
convention; never `file://`).

Status file (e.g. `data/viz/points_rebuild_progress.json`):

```json
{
  "started_at": "...",
  "updated_at": "...",
  "total_runs": 412,
  "runs_done": 87,
  "current": {"model": "icon_global", "run_init": "2026-07-30T06:00:00Z"},
  "per_model": {
    "gfs": {"runs_done": 28, "rows_written": 733000, "errors": 0},
    "icon_global": {"runs_done": 3, "rows_written": 41000, "errors": 1}
  },
  "recent_errors": [
    {"model": "arome_france", "run_init": "...", "error": "..."}
  ]
}
```

Page: a title, an overall progress bar (`runs_done / total_runs`), a table
(one row per model: runs done, rows written, errors), and a scrolling recent-
errors log — refetches the JSON every 2-3 seconds. Matplotlib-prototype-ugly
on purpose, matching this project's stated viz approach: functional first,
no design polish until asked for.

This directly replaces the failure mode from earlier today (a background
extraction process with no visibility, minutes of silence, wrongly assumed
hung). The status file is real disk I/O the rebuild script does itself, not
dependent on any pipe/exec output-buffering chain between here and wherever
it's watched from.

## 7. Known risks / slow points (carried over from today's investigation)

- **`icon_global`** needs a `cdo` remap subprocess call per (parameter, step)
  — 5 params (4 cloud + temp) × up to ~93 steps ≈ 465 remap calls for one
  run. Since the rebuild now seeds only a single newest run per model (not a
  historical backfill), this is paid once, not once per archived run — but
  it's still the slowest single model in the seed pass by a wide margin. No
  fix proposed here; just sized so it's not mistaken for a hang.
- **Corrupted/partial raw files** from the 2026-08-03 disk-full incident:
  some runs have a mix of good and zero-byte files. Extraction already skips
  a broken step's read (logs a warning, continues) rather than failing the
  whole run. §4's freshness/integrity check before extracting is meant to
  catch the case where the *whole* newest run is bad, not just one step of
  an otherwise-good one.
- **`ecmwf_ens`/`aifs_ens` temperature stays `None` everywhere**, deliberately
  (see §3) — not a bug to chase.

## 8. Rollout sequence (once this plan is approved)

1. Resolve §4's open question (how the desktop reads VPS status) - blocks
   step 3 below specifically, not steps 1-2.
2. Finalize + commit the already-written extraction code (schema,
   full-range widening, `mark_extracted` fix, `aemet_harmonie` exclusion,
   the 4 Open-Meteo extractors' widening) — desktop-only at this point,
   nothing deployed differently yet.
3. Build the progress-JSON writer + browser page (§6).
4. Run the desktop rebuild (§4: wipe, then VPS-verified newest-run-per-model
   seed - fetching first wherever the desktop is behind), watching it in the
   browser instead of a terminal.
5. Verify results for real: spot-check a few (model, site) series, confirm
   row counts and temp_c coverage match §3's table, confirm dead models are
   actually gone if that's confirmed in scope.
6. Only then: decide §5's open question and start on VPS pipeline changes.

---
*Machine/deployment specifics (box names, hostnames, ports beyond localhost
dev convention) are intentionally not in this document — see private ops
notes.*
