"""Rendering, one window of steps at a time, plus manifest regeneration.

src/viz/frame_renderer.py's own render_run() renders a WHOLE run, which is
the right unit for the desktop backfill but the wrong one here: production
must render only the window it has just fetched, before that window's raw is
reclaimed and the next window is downloaded. So this module drives
render_frame() over an explicit step list. frame_renderer itself is left
completely untouched (deliberately - see this package's merge-seam note in
src/pipeline/orchestrator.py).

render_frame() is already idempotent per frame (it skips the matplotlib work
when the PNG exists, while still calling the reader for an accurate has_data),
so re-rendering a window is cheap and a re-run after a crash is safe.

WINDOW BOUNDARIES AND DIFFERENCED FIELDS: a field that differences against an
earlier step (accumulated precipitation - see src/pipeline/fields.py) needs
the previous step's raw as well as its own. That works across a window
boundary because reclaim.py holds a step's raw until its successor has
rendered, so the last step of window N is still on disk while window N+1
renders. Whoever implements rain must make the renderer report
has_data=False when the predecessor's raw is missing, rather than plotting a
raw since-run-start accumulation.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.pipeline import journal
from src.viz import frame_renderer

log = logging.getLogger("pipeline.render")


def render_steps(
    model_id: str, run_init: datetime, steps: list[int]
) -> dict[int, dict[str, bool]]:
    """Render every structurally-supported field for each of `steps`.

    Returns {step: {field: has_data}} - the same shape render_run() returns -
    and folds it into this run's render journal, which is how a step that is
    genuinely absent from an otherwise readable raw file (arome_france's +0h)
    eventually becomes distinguishable from one that simply has not been
    rendered yet. See src/pipeline/reclaim.py.
    """
    # A model with no reader cannot be rendered at all, whatever
    # supported_fields() says. It says "hml_composite" for the four Open-Meteo
    # point models because models.yaml confirms their cloud LEVELS - true, and
    # irrelevant: those levels arrive as JSON point series, there is no grid to
    # draw, and render_frame() raises KeyError on the missing reader.
    #
    # src/scheduler/run.py has always guarded this with `renderable =
    # set(_MODEL_READERS)`; this path never did, so every pass logged four
    # tracebacks and recorded four run errors. Non-fatal - they are caught
    # upstream and the pass continues - but noise in `errors` is exactly what
    # makes a real failure invisible during Aug 5-12.
    if model_id not in frame_renderer._MODEL_READERS:
        return {}

    fields = frame_renderer.supported_fields(model_id)
    renderable = _steps_with_raw(model_id, run_init, steps)
    results: dict[int, dict[str, bool]] = {}
    for step in steps:
        if step not in renderable:
            continue
        results[step] = {}
        for fld in fields:
            _, has_data = frame_renderer.render_frame(model_id, run_init, step, fld)
            results[step][fld] = has_data
    journal.record_render_pass(model_id, run_init, results)
    return results


def _steps_with_raw(model_id: str, run_init: datetime, steps: list[int]) -> set[int]:
    """Which of `steps` actually have raw on disk to render from.

    A step whose raw was never fetched says NOTHING about whether the field
    exists in it - but render_frame() reports it exactly the way it reports a
    readable file that genuinely lacks the field: has_data=False. Journaling
    that as a no-data observation turns "not fetched yet" into "structurally
    absent", and completeness.py then excuses the step forever, so the top-up
    that would have fetched it never runs again.

    Measured on the live VPS 2026-07-29. Meteo-France publishes AROME in 6-hour
    group files; the 07-29 06Z fetch landed when only SP1/SP2_00H06H existed,
    so steps 7..51 had no raw at all. They were rendered anyway, journaled as
    no-data, excused, and the run was declared complete with 6 of its 51 cloud
    frames. The 07-28 06Z and 18Z runs went the same way (12 and 18 frames);
    every other cycle that day happened to be fetched after full publication
    and got all 51. Before the completeness rewrite the fraction rule kept
    re-fetching these runs, which is why this only started on 07-28.

    Skipping the render also saves the reader calls, but that is a side
    benefit - the point is not to record evidence we do not have.
    """
    from src.config import DATA_RAW, get_model
    from src.pipeline import raw_layout

    run_dir = DATA_RAW / model_id / f"{run_init:%Y%m%d%H}"
    try:
        names = [p.name for p in run_dir.iterdir()
                 if p.is_file() and not raw_layout.is_marker(p.name)]
    except OSError:
        names = []
    if not names:
        # Nothing fetched, or every file already reclaimed (the tombstone is a
        # marker, so it is excluded above). Either way there is nothing here to
        # render and nothing to conclude from it.
        return set()

    by_step = raw_layout.group_files_by_step(model_id, get_model(model_id), run_init, names)
    if not by_step:
        # Files are present but raw_layout cannot name their steps - a new
        # fetcher, or a renamed output. Fall back to today's behaviour rather
        # than declaring every step unfetched: that would stop the journal
        # recording anything at all, and a run that can never record a no-data
        # observation is a run that can never be complete. Same fail-safe
        # direction as raw_layout's own "unknown filename is never reclaimed".
        return set(steps)
    return {s for s in steps if s in by_step}


def regenerate_manifests() -> list[str]:
    """Rebuild Tool 1/2/3's manifests from the rendered tree.

    All three are pure disk scans now (0.5 s / ~5 s / ~7 s measured) and read
    no raw data, which is exactly what makes delete-after-render possible.
    Called once per pass, after rendering, never per frame.

    MERGE SEAM: the desktop scheduler is concurrently gaining automatic
    manifest regeneration after rendering. If that lands as a shared helper,
    this function should become a call to it rather than a second
    implementation - the imports below are the only thing to replace.
    """
    done: list[str] = []
    for name in (
        "generate_tool1_manifest",
        "generate_tool2_manifest",
        "generate_tool3_manifest",
    ):
        try:
            module = __import__(f"scripts.{name}", fromlist=["main"])
            module.main()
            done.append(name)
        except Exception:
            log.exception("manifest regeneration failed: %s", name)
    return done
