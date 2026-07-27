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
    fields = frame_renderer.supported_fields(model_id)
    results: dict[int, dict[str, bool]] = {}
    for step in steps:
        results[step] = {}
        for fld in fields:
            _, has_data = frame_renderer.render_frame(model_id, run_init, step, fld)
            results[step][fld] = has_data
    journal.record_render_pass(model_id, run_init, results)
    return results


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
