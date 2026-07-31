"""When is a run finished? One rule, used by the fetcher and the dashboard.

There were two copies of this question with two different answers: a hardcoded
0.9 in orchestrator._frames_complete() and COMPLETE_FRACTION in coverage.py.
Both said "a run is done when 90% of its declared steps have frames", and that
tolerance silently lost data.

WHY A FRACTION WAS WRONG. The 10% existed because some steps legitimately
produce no frame - gfs f000 publishes no cloud, so a healthy run lands at
208/209 and equality would re-fetch it forever. But 10% of a run is not a
small number of steps, and for gefs_extended the ENTIRE extended range
(+385..+840h) fits inside it. Measured on the live VPS 2026-07-28:
gefs_extended 2026-07-27T00Z had 111 of 121 cloud steps = 91.7%, so it read
"done", the orchestrator skipped it with fetch=False, and the ten missing
long-lead steps were never fetched - while the files sat on AWS. The tolerance
was hiding exactly the thing it was covering for.

THE RULE HERE. A step needs a frame unless something says it cannot have one:

  - the fetch is DEAD (src/pipeline/failures.py): tried enough times, past the
    model's publication horizon, still absent upstream. gfs f000's cloud
    products are this.
  - the render CONFIRMED NO DATA (the render journal): the raw was readable
    and simply had nothing for that step/field, seen enough times to not be a
    transient read failure. This is reclaim.py's own "structurally absent"
    test, reused rather than restated.

Everything else must be there. No fraction, no tolerance, and nothing quietly
excused - each exclusion is a recorded, inspectable fact about that step.

PER FIELD, not per step. gfs f000 has 2m temperature but no cloud, so excusing
the whole step would forgive a genuinely missing temp frame.

Callers pass in the frames they already listed. The two of them count frames
very differently - the orchestrator lists one run's directories, the dashboard
builds one index for every run of a model - and forcing either to re-list would
undo an optimisation that exists for a reason.
"""

from __future__ import annotations

from datetime import datetime

# Consecutive no-data render observations before "this step has nothing" is
# believed. Same threshold reclaim.py uses for the same judgement; one
# observation could just as easily be a transient read failure.
MIN_NO_DATA_OBSERVATIONS = 2


def no_data_steps(model_id: str, run_init: datetime, field: str,
                  min_observations: int = MIN_NO_DATA_OBSERVATIONS) -> set[int]:
    """Steps whose raw was readable but held nothing for THIS field."""
    from src.pipeline import journal
    j = journal.load_render_journal(model_id, run_init)
    out: set[int] = set()
    for step, per_field in (j.get("no_data") or {}).items():
        try:
            if int((per_field or {}).get(field, 0)) >= min_observations:
                out.add(int(step))
        except (TypeError, ValueError):
            continue
    return out


def excused_steps(model_id: str, run_init: datetime, field: str, now: datetime,
                  rendered: set[int] | None = None) -> set[int]:
    """Steps that may be absent for `field` without the run being incomplete.

    `rendered` is this field's steps that DO have frames. Passing it lets a
    single no-data observation count, provided the field renders fine elsewhere
    in the same run - which proves the raw is readable and the fetch worked, so
    "nothing here" is structural rather than a transient read failure. That is
    reclaim.py's own siblings-rendered test, reused.

    Without it the threshold is 2 observations, and that deadlocked: every
    arome_france run sat at exactly 1 for its +0h cloud (a real structural gap,
    T34 - the SP2 group file is named 00H06H but carries no +0h cloud message),
    so no run ever counted complete and the pipeline re-fetched 48 hours of
    them, 8 cycles a day, forever. A completeness rule must not depend on a
    counter that can stall.
    """
    from src.pipeline import failures
    dead = failures.dead_steps(model_id, run_init, now)
    if rendered:
        # This field demonstrably renders from this run's raw, so one
        # observation of "nothing at this step" is enough.
        return dead | no_data_steps(model_id, run_init, field, min_observations=1)
    return dead | no_data_steps(model_id, run_init, field)


def missing_steps(model_id: str, run_init: datetime, declared: list[int],
                  frames_by_field: dict[str, set[int]], now: datetime,
                  ) -> dict[str, set[int]]:
    """{field: steps that should have a frame and do not}. Empty == complete."""
    out: dict[str, set[int]] = {}
    for field, have in frames_by_field.items():
        required = set(declared) - excused_steps(
            model_id, run_init, field, now, rendered=have)
        gap = required - have
        if gap:
            out[field] = gap
    return out


def backfill_known_fields(model_id: str, fields: list[str],
                          present: dict[str, set[int]]) -> dict[str, set[int]]:
    """Add an explicit empty set for a field this model HAS rendered before
    (its output directory exists) but that produced nothing for this
    particular run - as opposed to a field that has never rendered for any
    run of this model at all, which is_complete()'s missing-key guard below
    exists to catch and must keep catching.

    Without this, a field that is genuinely and permanently absent from one
    specific run - every declared step correctly excused by dead_steps()/
    no_data_steps() - could still never read complete, for the same reason a
    field that has not STARTED rendering cannot: both have zero files on
    disk, and nothing here could tell them apart.

    Found 2026-07-31: orchestrator._frames_complete() built its own per-run
    listing that already did this (an inline `if not d.is_dir(): return
    False` per field, else `frames[fld] = steps` unconditionally - so an
    empty set from a real listing still became a key). coverage.py's
    _frame_index() builds its listing differently - one pass per field
    across every run of the model, only ever creating a run's key when it
    finds a matching file - so a field with zero frames for one run got no
    key there at all. The two disagreed: the orchestrator had correctly
    stopped re-fetching arome_france 2026-07-30T15Z (temp rendered 52/52,
    hml_composite's own 52/52 steps all independently confirmed no-data - a
    real, one-off loss in that cycle's cloud package, not a fetch problem),
    while the dashboard kept reporting it as "fetched", implying work still
    outstanding when none was left to do. This is exactly the class of bug
    completeness.py itself was written to close - two copies of the same
    judgement, silently arriving at different answers - just one level
    further down than the original fraction-vs-fraction duplication.
    """
    from src.viz.frame_renderer import OUTPUT_DIR
    out = dict(present)
    for fld in fields:
        if fld not in out and (OUTPUT_DIR / model_id / fld).is_dir():
            out[fld] = set()
    return out


def is_complete(model_id: str, run_init: datetime, declared: list[int],
                frames_by_field: dict[str, set[int]], fields: list[str],
                now: datetime) -> bool:
    """Has this run produced every frame it can?

    `fields` is passed separately from frames_by_field's keys so that a field
    with NO directory at all reads as incomplete rather than as vacuously
    satisfied - which is how "every field has at least one frame" once called a
    run 6% processed finished.
    """
    if not fields:
        return False
    if any(f not in frames_by_field for f in fields):
        return False
    return not missing_steps(model_id, run_init, declared, frames_by_field, now)
