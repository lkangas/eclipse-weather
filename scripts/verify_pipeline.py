"""Verification for the production reclaim pipeline, against synthetic
fixtures in a throwaway directory - NEVER against real archived data.

This is the test that has to pass before anyone runs
`python -m src.pipeline.run --apply` on a box holding real runs. It exercises
the two properties that, if wrong, destroy unrecoverable data:

  * the completeness check (what counts as "rendered", precisely), and
  * the dry-run default (a plan must delete nothing).

It runs without pytest (the image ships no dev dependencies):

    .venv/bin/python -m scripts.verify_pipeline

It points ECLIPSE_DATA_ROOT at a fresh temp directory BEFORE importing any
src module, because src.config binds DATA_ROOT at import time and
src.extract.base copies it. That ordering is what guarantees this script
cannot touch data/raw/ no matter what it does.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="eclipse_pipeline_verify_"))
os.environ["ECLIPSE_DATA_ROOT"] = str(_TMP_ROOT)

from src.config import get_model  # noqa: E402
from src.fetchers.base import full_range_steps, raw_file_present  # noqa: E402
from src.pipeline import chunking, journal, raw_layout, reclaim, verify  # noqa: E402
from src.pipeline.settings import Settings  # noqa: E402
from src.viz import frame_renderer  # noqa: E402

# Redirect the rendered-frame tree into the same throwaway root.
frame_renderer.OUTPUT_DIR = _TMP_ROOT / "viz" / "frames"

RAW = _TMP_ROOT / "raw"
NOW = datetime.now(UTC)
OLD = NOW - timedelta(hours=6)  # everything the fixtures write is "old enough"

SETTINGS = Settings(
    reclaim_enabled=True,
    min_file_age_seconds=120,
    min_no_data_observations=2,
    require_extraction_for_eclipse_steps=True,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048 + b"IEND\xaeB`\x82"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(label)


def write_raw(model: str, run_init: datetime, name: str, size: int = 4096,
              age_s: int = 3600) -> Path:
    d = RAW / model / run_init.strftime("%Y%m%d%H")
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\x00" * size)
    stamp = NOW.timestamp() - age_s
    os.utime(p, (stamp, stamp))
    return p


def write_frame(model: str, run_init: datetime, step: int, fld: str,
                truncated: bool = False) -> Path:
    p = verify.frame_path(model, run_init, step, fld)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_PNG[:1500] if truncated else _PNG)
    return p


def mark_extracted(model: str, run_init: datetime) -> None:
    (RAW / model / run_init.strftime("%Y%m%d%H") / ".extracted").touch()


def decisions(plan: reclaim.RunPlan) -> dict[str, str]:
    return {c.path.name: c.decision for c in plan.candidates}


_USED_INITS: set[datetime] = set()


def unique_init(hours_back: int, cycle_hours: int) -> datetime:
    """A run_init `hours_back` in the past, snapped down to this model's cycle
    grid, guaranteed distinct from every init already handed out.

    Fixtures share the real data root per model, so two tests landing on the
    same (model, run_init) would see each other's files. Snapping to a cycle
    grid makes that possible at some wall-clock times and not others, which is
    exactly the kind of test that passes all afternoon and fails at 03:00.
    """
    init = (NOW - timedelta(hours=hours_back)).replace(minute=0, second=0, microsecond=0)
    init = init.replace(hour=(init.hour // cycle_hours) * cycle_hours)
    while init in _USED_INITS:
        init -= timedelta(hours=cycle_hours)
    _USED_INITS.add(init)
    return init


# --------------------------------------------------------------------------


def test_raw_layout_step_parsing() -> None:
    print("\n[1] raw_layout: filename -> steps, for every fetcher convention")
    init = datetime(2026, 7, 26, 0, tzinfo=UTC)
    gfs, arome = get_model("gfs"), get_model("arome_france")
    cases = [
        ("gfs", gfs, "f012_cloud.grib2", {12}),
        ("gefs_extended", get_model("gefs_extended"), "f390_c00_levels.grib2", {390}),
        ("aifs_ens", get_model("aifs_ens"), "cloud_f036.grib2", {36}),
        ("ecmwf_hres", get_model("ecmwf_hres"), "pl_f009.grib2", {9}),
        ("ecmwf_ens", get_model("ecmwf_ens"), "tcc_f006.grib2", {6}),
        ("icon_eu", get_model("icon_eu"),
         "icon-eu_europe_regular-lat-lon_single-level_2026072600_024_CLCL.grib2", {24}),
        ("icon_global", get_model("icon_global"),
         "icon_global_icosahedral_single-level_2026072600_003_CLCT.grib2", {3}),
        ("gfs", gfs, "f012_cloud.grib2.5b7b6.idx", {12}),  # cfgrib sidecar -> parent's step
    ]
    for model, cfg, name, want in cases:
        got = raw_layout.steps_in_file(model, cfg, init, name)
        check(f"{name} -> {sorted(want)}", got == want, f"got {got}")

    group = raw_layout.steps_in_file("arome_france", arome, init, "arome_france_SP2_00H06H.grib2")
    check("arome group 00H06H -> steps 0..6",
          group == frozenset({0, 1, 2, 3, 4, 5, 6}), f"got {sorted(group or [])}")
    check("unrecognised filename -> None (never reclaimable)",
          raw_layout.steps_in_file("gfs", gfs, init, "mystery.bin") is None)
    check("marker files -> None",
          raw_layout.steps_in_file("gfs", gfs, init, ".extracted") is None)


def test_completeness_and_dry_run() -> None:
    print("\n[2] completeness check + dry-run (aifs_ens, one file per step)")
    model, cfg = "aifs_ens", get_model("aifs_ens")
    init = unique_init(12, 6)
    steps = full_range_steps(cfg, init)[:4]
    fields = verify.expected_fields(model)
    check("aifs_ens expected fields are the composites",
          fields == ["hml_composite", "prob_hml_composite"], f"got {fields}")

    for s in steps:
        write_raw(model, init, f"cloud_f{s:03d}.grib2", size=8192)
    # step[0]: fully rendered. step[1]: one field missing. step[2]: truncated
    # PNG. step[3]: nothing rendered at all.
    for fld in fields:
        write_frame(model, init, steps[0], fld)
    write_frame(model, init, steps[1], fields[0])
    write_frame(model, init, steps[2], fields[0])
    write_frame(model, init, steps[2], fields[1], truncated=True)
    mark_extracted(model, init)

    plan = reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW)
    d = decisions(plan)
    check("fully rendered step -> reclaim",
          d[f"cloud_f{steps[0]:03d}.grib2"] == reclaim.RECLAIM, str(d))
    check("one field missing -> hold",
          d[f"cloud_f{steps[1]:03d}.grib2"] == reclaim.HOLD_NOT_RENDERED, str(d))
    check("truncated PNG counts as NOT rendered -> hold",
          d[f"cloud_f{steps[2]:03d}.grib2"] == reclaim.HOLD_NOT_RENDERED, str(d))
    check("nothing rendered -> hold",
          d[f"cloud_f{steps[3]:03d}.grib2"] == reclaim.HOLD_NOT_RENDERED, str(d))

    before = {p.name: p.stat().st_size for p in verify.raw_files(model, init)}
    plan2 = reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW)
    after = {p.name: p.stat().st_size for p in verify.raw_files(model, init)}
    check("planning twice deletes nothing (dry-run is read-only)", before == after)
    check("plan reports the bytes it would free",
          plan2.bytes_to_reclaim == before[f"cloud_f{steps[0]:03d}.grib2"],
          f"{plan2.bytes_to_reclaim}")

    print("\n[3] young files are never reclaimed")
    fresh_init = unique_init(36, 6)
    for s in steps[:1]:
        write_raw(model, fresh_init, f"cloud_f{s:03d}.grib2", age_s=5)
        for fld in fields:
            write_frame(model, fresh_init, s, fld)
    mark_extracted(model, fresh_init)
    dp = decisions(reclaim.plan_run(model, cfg, fresh_init, SETTINGS, now=NOW))
    check("file written 5s ago -> hold",
          dp[f"cloud_f{steps[0]:03d}.grib2"] == reclaim.HOLD_TOO_YOUNG, str(dp))

    print("\n[4] unknown filenames are never reclaimed")
    write_raw(model, init, "something_new.grib2")
    d3 = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
    check("unknown layout -> hold", d3["something_new.grib2"] == reclaim.HOLD_UNKNOWN_LAYOUT,
          str(d3))
    return model, cfg, init, steps, fields


def test_extraction_gate() -> None:
    print("\n[5] eclipse-hour raw is held until points.parquet extraction")
    model, cfg = "gfs", get_model("gfs")
    # A run whose forecast reaches the eclipse valid hours, so steps_for_run()
    # returns real targets rather than None.
    from src.fetchers.base import eclipse_t, steps_for_run

    init = eclipse_t().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    targets = {h[0] for h in steps_for_run(cfg, init).values() if h is not None}
    check("fixture run really does cover eclipse valid hours", bool(targets), str(targets))
    eclipse_step = sorted(targets)[0]
    other_step = full_range_steps(cfg, init)[1]
    fields = verify.expected_fields(model)

    write_raw(model, init, f"f{eclipse_step:03d}_cloud.grib2")
    write_raw(model, init, f"f{other_step:03d}_cloud.grib2")
    for s in (eclipse_step, other_step):
        for fld in fields:
            write_frame(model, init, s, fld)

    d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
    check("eclipse-hour file held while .extracted is absent",
          d[f"f{eclipse_step:03d}_cloud.grib2"] == reclaim.HOLD_AWAITING_EXTRACTION, str(d))
    check("non-eclipse file of the same run is still reclaimable",
          d[f"f{other_step:03d}_cloud.grib2"] == reclaim.RECLAIM, str(d))

    mark_extracted(model, init)
    d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
    check("after extraction the eclipse-hour file is reclaimable",
          d[f"f{eclipse_step:03d}_cloud.grib2"] == reclaim.RECLAIM, str(d))
    return model, cfg, init


def test_structural_gap_vs_corruption() -> None:
    print("\n[6] structural no-data (arome +0h) vs suspected corruption")
    model, cfg = "arome_france", get_model("arome_france")
    init = unique_init(12, 3)
    fields = verify.expected_fields(model)
    name = "arome_france_SP2_00H06H.grib2"
    write_raw(model, init, name, size=16384)
    covered = sorted(raw_layout.steps_in_file(model, cfg, init, name))
    for s in covered[1:]:  # every step but +0h renders fine
        for fld in fields:
            write_frame(model, init, s, fld)
    mark_extracted(model, init)

    d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
    check("group file held while +0h has no frames and no no-data evidence",
          d[name] == reclaim.HOLD_NOT_RENDERED, str(d))

    for _ in range(SETTINGS.min_no_data_observations):
        journal.record_render_pass(model, init, {0: dict.fromkeys(fields, False)})
    d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
    check("released once +0h is confirmed no-data AND siblings rendered",
          d[name] == reclaim.RECLAIM, str(d))

    # The same evidence on a one-file-per-step model must NOT release it:
    # there is no readable sibling, so "no data" is a corruption suspicion.
    m2, cfg2 = "aifs_ens", get_model("aifs_ens")
    init2 = unique_init(60, 6)
    s2 = full_range_steps(cfg2, init2)[3]
    f2 = f"cloud_f{s2:03d}.grib2"
    write_raw(m2, init2, f2)
    mark_extracted(m2, init2)
    for _ in range(SETTINGS.min_no_data_observations + 1):
        journal.record_render_pass(
            m2, init2, {s2: dict.fromkeys(verify.expected_fields(m2), False)}
        )
    plan = reclaim.plan_run(m2, cfg2, init2, SETTINGS, now=NOW)
    d = decisions(plan)
    check("unrenderable single-step file -> hold, flagged for corrupt-file re-fetch",
          d[f2] == reclaim.HOLD_UNREADABLE, str(d))
    check("...and it is surfaced in needs_attention", bool(plan.needs_attention))


def test_apply_and_tombstone(model: str, cfg: dict, init: datetime) -> None:
    print("\n[7] --apply deletes, tombstones, and stops the top-up re-download")
    plan = reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW)
    targets = [c.path for c in plan.to_reclaim]
    check("there is something to reclaim in this fixture", bool(targets))
    expected_bytes = plan.bytes_to_reclaim

    freed = reclaim.apply_plan(plan, SETTINGS, now=NOW)
    check("apply_plan reports the bytes it freed", freed == expected_bytes,
          f"{freed} != {expected_bytes}")
    check("the files are gone", all(not p.exists() for p in targets))
    check("a tombstone records every deleted file",
          journal.reclaimed_filenames(model, init) == {p.name for p in targets})
    check("raw_file_present() is still True for a reclaimed file - a top-up "
          "fetch will NOT re-download it", all(raw_file_present(p) for p in targets))

    from src.fetchers.base import already_fetched
    check("already_fetched() still True after reclaim (markers keep the dir alive)",
          already_fetched(model, init))
    check("reclaimed steps are reported as done, not missing",
          bool(journal.reclaimed_steps(model, init)))

    # Idempotency: a second plan/apply over the same run must be a no-op.
    plan2 = reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW)
    check("second pass finds nothing left to reclaim from those files",
          all(c.path.name not in journal.reclaimed_filenames(model, init)
              for c in plan2.to_reclaim))

    disabled = Settings(reclaim_enabled=False)
    check("reclaim.enabled=false makes apply_plan a no-op",
          reclaim.apply_plan(plan, disabled, now=NOW) == 0)


def test_chunking() -> None:
    print("\n[8] chunking bounds the in-flight footprint")
    init = datetime(2026, 7, 26, 0, tzinfo=UTC)
    cfg = get_model("aifs_ens")
    caps = chunking.chunk_caps(cfg, init, 6)
    widths, prev = [], None
    for cap in caps:
        widths.append(len(chunking.steps_in_chunk(cfg, init, cap, prev)))
        prev = cap
    check("aifs_ens 6h windows are one step each", set(widths) == {1}, str(sorted(set(widths))))
    check("windows cover the whole published range",
          caps[-1] == full_range_steps(cfg, init)[-1], f"{caps[-1]}")
    covered, prev = [], None
    for cap in caps:
        covered += chunking.steps_in_chunk(cfg, init, cap, prev)
        prev = cap
    check("no step is skipped or duplicated across windows",
          covered == full_range_steps(cfg, init))

    gfs = get_model("gfs")
    caps = chunking.chunk_caps(gfs, init, 24)
    widths, prev = [], None
    for cap in caps:
        widths.append(len(chunking.steps_in_chunk(gfs, init, cap, prev)))
        prev = cap
    check("gfs 24h windows hold 24 hourly steps at the dense end",
          max(widths) == 24, str(max(widths)))

    narrowed = chunking.narrow_config(cfg, init, 24)
    check("narrow_config caps the run's own cycle only",
          full_range_steps(narrowed, init)[-1] == 24
          and narrowed["cycles"] != cfg["cycles"]
          and cfg["cycles"]["00"] == 360,  # original dict untouched
          str(narrowed["cycles"]))
    check("non-chunkable fetch kinds report one window (whole run)",
          len(chunking.chunk_caps(get_model("arome_france"), init, 6)) == 1)


def test_never_reclaims_unrenderable_models() -> None:
    print("\n[9] models with no renderer are never reclaimed")
    # aemet_harmonie USED to be in this list. It gained a reader on 2026-07-27
    # (the ESCALA colour-legend inversion), so it is renderable now and its raw
    # is legitimately reclaimable once rendered - keeping it here asserted the
    # opposite and failed the moment the reader landed. ukmo_global stays: it is
    # an Open-Meteo point model with no grid to render at all.
    for model in ("ukmo_global",):
        cfg = get_model(model)
        init = datetime(2026, 7, 26, 0, tzinfo=UTC)
        write_raw(model, init, "ukmo_global_20260726T190000Z.json")
        mark_extracted(model, init)
        plan = reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW)
        check(f"{model}: nothing reclaimable",
              not plan.to_reclaim and all(
                  c.decision == reclaim.HOLD_NOT_RENDERABLE for c in plan.candidates))

    # The other half of the same rule: a model that DOES have a reader must not
    # be treated as unrenderable. Without this, deleting a reader would silently
    # move a model into the "hold everything forever" branch and no test would
    # notice - which is how aemet_harmonie's own entry above went stale.
    check("aemet_harmonie is renderable (has a reader)",
          verify.expected_fields("aemet_harmonie") == ["total"],
          str(verify.expected_fields("aemet_harmonie")))


def test_lookback_successor_rule() -> None:
    """Rain is not implemented, but the deletion rule it requires is. With a
    lookback of 1 registered for a field, step n's raw must survive until
    step n+1 has rendered - and a frame whose lookback input is already gone
    must not pin its own raw forever."""
    print("\n[10] differenced-field (rain) lookback: hold predecessor until "
          "successor renders")
    from src.pipeline import fields as field_deps

    model, cfg = "aifs_ens", get_model("aifs_ens")
    flds = verify.expected_fields(model)
    # A young run: still inside the top-up window (48 h), so "the successor
    # has not rendered yet" genuinely means "wait", not "it will never
    # arrive". Offset by a whole day from every other fixture's run_init so
    # the two can never collide at some wall-clock times but not others.
    init = unique_init(32, 6)
    steps = full_range_steps(cfg, init)[:3]
    for s in steps:
        write_raw(model, init, f"cloud_f{s:03d}.grib2")
        for fld in flds:
            write_frame(model, init, s, fld)
    mark_extracted(model, init)
    check("lookback fixture run is still inside the top-up window (not sealed)",
          not verify.verify_run(model, cfg, init, now=NOW).sealed)

    saved = dict(field_deps.LOOKBACK_STEPS)
    try:
        # Baseline: self-contained fields, everything rendered -> all released.
        d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
        check("lookback 0: every fully-rendered step released",
              all(v == reclaim.RECLAIM for v in d.values()), str(d))

        # Now pretend one of the model's fields differences against step n-1.
        field_deps.LOOKBACK_STEPS[flds[0]] = 1
        # Remove the last step's frames: its raw is unrendered, so the step
        # BEFORE it must now be held as that frame's lookback input.
        for fld in flds:
            verify.frame_path(model, init, steps[2], fld).unlink()
        d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
        check("predecessor of an unrendered step is held, not deleted",
              d[f"cloud_f{steps[1]:03d}.grib2"] == reclaim.HOLD_LOOKBACK_SUCCESSOR, str(d))
        check("the unrendered step itself is held too",
              d[f"cloud_f{steps[2]:03d}.grib2"] == reclaim.HOLD_NOT_RENDERED, str(d))
        check("an earlier step whose successor IS rendered is still released",
              d[f"cloud_f{steps[0]:03d}.grib2"] == reclaim.RECLAIM, str(d))

        # Re-render the last step -> the chain unblocks.
        for fld in flds:
            write_frame(model, init, steps[2], fld)
        d = decisions(reclaim.plan_run(model, cfg, init, SETTINGS, now=NOW))
        check("once the successor renders, the predecessor is released",
              d[f"cloud_f{steps[1]:03d}.grib2"] == reclaim.RECLAIM, str(d))

        # A run past the top-up window will never gain another step, so
        # holding a predecessor for a successor that never arrived would pin
        # it forever - it is released instead.
        old_init = unique_init(32 + 24 * 4, 6)
        old_steps = full_range_steps(cfg, old_init)[:2]
        write_raw(model, old_init, f"cloud_f{old_steps[0]:03d}.grib2")
        for fld in flds:
            write_frame(model, old_init, old_steps[0], fld)
        mark_extracted(model, old_init)
        d = decisions(reclaim.plan_run(model, cfg, old_init, SETTINGS, now=NOW))
        check("sealed run: predecessor released even though the successor "
              "never arrived",
              d[f"cloud_f{old_steps[0]:03d}.grib2"] == reclaim.RECLAIM, str(d))

        # Deadlock guard: successor frame missing AND its lookback input
        # already reclaimed -> unproducible, so the successor's own raw is
        # not pinned forever waiting for a frame that can never exist.
        write_raw(model, old_init, f"cloud_f{old_steps[1]:03d}.grib2")
        journal.record_reclaimed(
            model, old_init,
            {f"cloud_f{old_steps[0]:03d}.grib2": {"steps": [old_steps[0]], "bytes": 1}},
        )
        (RAW / model / old_init.strftime("%Y%m%d%H")
         / f"cloud_f{old_steps[0]:03d}.grib2").unlink()
        v = verify.verify_run(model, cfg, old_init, now=NOW)
        check("fixture run is past the top-up window (sealed)", v.sealed)
        check("frame whose lookback input was discarded is UNPRODUCIBLE, not missing",
              v.verdicts[old_steps[1]].unproducible_fields == [flds[0]],
              str(v.verdicts[old_steps[1]]))
        check("a self-contained field on the same step stays genuinely missing "
              "(its own raw can still re-render it)",
              v.verdicts[old_steps[1]].genuinely_missing == [flds[1]],
              str(v.verdicts[old_steps[1]]))
    finally:
        field_deps.LOOKBACK_STEPS.clear()
        field_deps.LOOKBACK_STEPS.update(saved)


def test_unfetched_steps_are_not_evidence() -> None:
    """A step with no raw must not be journaled as "this field has no data".

    The bug this pins down, measured on the live VPS 2026-07-29: Meteo-France
    publishes AROME in 6-hour group files, the fetch landed with only
    SP1/SP2_00H06H on disk, and steps 7..51 - which had no raw AT ALL - were
    rendered anyway, reported has_data=False, journaled as no-data, and then
    excused by completeness.py. The run was declared finished with 6 of its 51
    cloud frames and the top-up never ran again. Three AROME runs lost most of
    their range that way before anyone looked.
    """
    from src.pipeline import render as pipeline_render

    print("\n[11] steps with no raw are not journaled as no-data")
    model, cfg = "arome_france", get_model("arome_france")
    init = unique_init(hours_back=6, cycle_hours=3)
    declared = full_range_steps(cfg, init)
    fetched, unfetched = [s for s in declared if s <= 6], [s for s in declared if s > 6]
    check("fixture has both fetched and unfetched steps",
          bool(fetched) and bool(unfetched), f"{len(fetched)}/{len(unfetched)}")

    # Only the first group file landed - exactly the live case.
    write_raw(model, init, "arome_france_SP2_00H06H.grib2")
    write_raw(model, init, "arome_france_SP1_00H06H.grib2")

    have = pipeline_render._steps_with_raw(model, init, declared)
    check("steps inside the fetched group are renderable", set(fetched) <= have,
          str(sorted(have)[:8]))
    check("steps with no raw are excluded", not (set(unfetched) & have),
          str(sorted(s for s in unfetched if s in have)[:8]))

    # ...and therefore cannot be excused: the run must still read incomplete,
    # which is what keeps the top-up fetching the rest of the range.
    from src.pipeline import completeness
    frames = {f: set(fetched) for f in verify.expected_fields(model)}
    check("run with an unfetched range is NOT complete",
          not completeness.is_complete(model, init, declared, frames,
                                       list(frames), NOW),
          str({k: len(v) for k, v in frames.items()}))

    # Fail-safe: a filename raw_layout cannot parse must not make every step
    # look unfetched - that would stop the journal recording anything at all.
    other = unique_init(hours_back=7, cycle_hours=3)
    write_raw(model, other, "something_new.grib2")
    check("unparseable filenames fall back to rendering every step",
          pipeline_render._steps_with_raw(model, other, declared) == set(declared))

    # A run whose raw is entirely gone (reclaimed, tombstone only) renders
    # nothing rather than journaling its whole range as no-data.
    empty = unique_init(hours_back=8, cycle_hours=3)
    (RAW / model / empty.strftime("%Y%m%d%H")).mkdir(parents=True, exist_ok=True)
    (RAW / model / empty.strftime("%Y%m%d%H") / ".reclaimed.json").write_text("{}")
    check("fully reclaimed run renders nothing",
          pipeline_render._steps_with_raw(model, empty, declared) == set())


def test_never_published_step_is_not_declared() -> None:
    """A step the source never distributes at all must not appear in the
    declared range in the first place - excusing it after the fact needs a
    no-data OBSERVATION, and this kind of gap can never produce one.

    The bug this pins down, found 2026-07-30: aemet_harmonie's bundle starts
    at run_init+1h and never carries the analysis hour, no request is ever
    made for it, and no fetch failure is ever recorded for it - so once
    render_steps() stopped rendering steps with no raw (the AROME/GEFS fix
    just above), step 0 could never be journaled as no-data either, and
    completeness.py declared every run of this model incomplete forever.
    2026-07-29T12Z sat reporting "fetched" for hours with all 48 of its
    fetchable frames already rendered.

    `first_step_h` (models.yaml) is the fix: it drops the floor in
    _available_steps_for_cycle() before anything downstream ever sees it, so
    there is no gap left to excuse.
    """
    print("\n[12] a step the source never distributes is not a declared step")
    model, cfg = "aemet_harmonie", get_model("aemet_harmonie")
    init = unique_init(hours_back=9, cycle_hours=6)
    declared = full_range_steps(cfg, init)
    check("first_step_h keeps step 0 out of the declared range", 0 not in declared,
          str(declared[:3]))
    check("every OTHER step is still declared", declared == list(range(1, 49)),
          f"{declared[0]}..{declared[-1]}, n={len(declared)}")

    from src.pipeline import completeness
    frames = {f: set(declared) for f in verify.expected_fields(model)}
    check("a run with every fetchable step rendered reads complete",
          completeness.is_complete(model, init, declared, frames, list(frames), NOW),
          str({k: len(v) for k, v in frames.items()}))


def test_wholly_excused_field_still_reads_complete() -> None:
    """A field that is genuinely and permanently absent from one specific run
    - every declared step independently confirmed no-data - must still let
    that run read complete, and the orchestrator's fetch-skip decision and
    coverage.py's dashboard must reach the SAME verdict from it.

    The bug this pins down, found 2026-07-31: arome_france 2026-07-30T15Z
    fetched its raw fine and rendered temp 52/52, but every one of
    hml_composite's 52 steps came back no-data (a real, one-off loss in that
    cycle's cloud package - not a fetch or code problem). The orchestrator's
    own per-run listing already built an explicit empty set for hml_composite
    (its directory exists globally, from every other run), so its
    completeness check correctly said done and stopped re-fetching. But
    coverage.py's dashboard listing only ever creates a run's key when it
    finds a matching FILE, so hml_composite had no key there at all -
    is_complete()'s missing-key guard then read the run as incomplete
    forever. The pipeline had already finished; the dashboard just never
    found out, and reported "fetched" - implying outstanding work - for a run
    with none left.
    """
    print("\n[13] a field with zero real frames, but every step excused, "
          "still completes - and the dashboard agrees with the fetcher")
    from src.pipeline import completeness
    from src.pipeline.orchestrator import _frames_complete

    model, cfg = "arome_france", get_model("arome_france")
    init = unique_init(hours_back=10, cycle_hours=6)
    declared = full_range_steps(cfg, init)
    fields = verify.expected_fields(model)
    check("fixture model has a second field to leave wholly absent",
          len(fields) >= 2, str(fields))
    present_field, absent_field = fields[0], fields[1]

    for step in declared:
        write_frame(model, init, step, present_field)
    # absent_field: no frame ever written, but every step independently
    # confirmed no-data (the render journal's OWN mechanism - see
    # completeness.py's MIN_NO_DATA_OBSERVATIONS), which is what excuses it.
    for step in declared:
        journal.record_render_pass(model, init, {step: {absent_field: False}})
        journal.record_render_pass(model, init, {step: {absent_field: False}})

    # coverage.py's own listing shape: a run's key only ever appears when a
    # FILE is found, so the wholly-absent field has no key at all here.
    per_field_as_coverage_finds_it = {present_field: set(declared)}
    check("dashboard's own listing has no key for the absent field",
          absent_field not in per_field_as_coverage_finds_it)

    backfilled = completeness.backfill_known_fields(
        model, fields, per_field_as_coverage_finds_it)
    check("backfill adds the absent field once its directory exists",
          absent_field in backfilled and backfilled[absent_field] == set())
    check("dashboard verdict, after backfill: complete",
          completeness.is_complete(model, init, declared, backfilled, fields, NOW))

    check("orchestrator verdict: complete (must agree with the dashboard)",
          _frames_complete(model, init, NOW))

    check("WITHOUT the backfill the dashboard used to disagree - regression guard",
          not completeness.is_complete(
              model, init, declared, per_field_as_coverage_finds_it, fields, NOW))


def main() -> int:
    print(f"fixture root: {_TMP_ROOT}")
    try:
        test_raw_layout_step_parsing()
        test_completeness_and_dry_run()
        model, cfg, init = test_extraction_gate()
        test_structural_gap_vs_corruption()
        test_apply_and_tombstone(model, cfg, init)
        test_chunking()
        test_never_reclaims_unrenderable_models()
        test_lookback_successor_rule()
        test_unfetched_steps_are_not_evidence()
        test_never_published_step_is_not_declared()
        test_wholly_excused_field_still_reads_complete()
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
