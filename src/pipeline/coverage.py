"""Did every model actually get every run it should have?

The status file says what a pass DID; it cannot say what never happened. A run
that was never fetched produces no outcome, no error and no log line - it is
simply absent, which is indistinguishable from "not due yet" unless something
computes the expectation independently. icon_eu and icon_global both lost
2026-07-25T12Z that way: the fetch errored, the run stayed empty, and it only
surfaced 36 hours later as a hole in an unrelated progress strip. By then DWD's
~24 h retention had closed and it was unrecoverable.

Coverage is judged on RENDERED FRAMES, not on raw. Production deletes raw as
soon as it has been rendered and verified, so raw presence means "recent", not
"archived" - judging by it would report every successfully-processed run as
missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

LOOKBACK_H = 48


def _frame_index(model_id: str) -> dict[str, dict[str, int]]:
    """{run_stamp: {field: n_frames}} from ONE listing per field directory.

    The obvious implementation - ask "does this run have frames?" per run, per
    field - re-lists a directory holding thousands of files once per question,
    which made building the matrix slow enough that it was only worth doing at
    the end of a pass. That is precisely backwards: this is the panel you want
    while a long pass is still running.
    """
    from src.viz.frame_renderer import OUTPUT_DIR, supported_fields
    idx: dict[str, dict[str, int]] = {}
    for field in supported_fields(model_id):
        d = OUTPUT_DIR / model_id / field
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix != ".png":
                continue
            stamp = p.name.split("_", 1)[0]
            idx.setdefault(stamp, {})
            idx[stamp][field] = idx[stamp].get(field, 0) + 1
    return idx


def build(now: datetime | None = None, lookback_h: int = LOOKBACK_H) -> dict:
    """{model: [{run_init, state, frames}]} for every cycle slot in the window.

    state is one of:
      ok       - frames exist for every field this model renders
      partial  - some fields have frames, others do not
      missing  - past its publication deadline with nothing to show
      pending  - not due yet; absence here is not a fault
      no-render- model has no renderer, so frames can never be the evidence
    """
    from src.config import load_models
    from src.fetchers.base import cycle_run_inits, due_time
    from src.viz.frame_renderer import _MODEL_READERS, supported_fields

    now = now or datetime.now(UTC)
    out: dict[str, list[dict]] = {}
    for model_id, cfg in load_models()["models"].items():
        if "cycles" not in cfg or "fetch" not in cfg:
            continue
        renderable = model_id in _MODEL_READERS
        n_fields = len(supported_fields(model_id)) if renderable else 0
        index = _frame_index(model_id) if renderable else {}
        rows = []
        for run_init in cycle_run_inits(cfg["cycles"], now, lookback_hours=lookback_h):
            due = due_time(cfg.get("publication_lag_h", [0, 0]), run_init)
            if not renderable:
                state, frames = "no-render", 0
            elif now < due:
                state, frames = "pending", 0
            else:
                per_field = index.get(f"{run_init:%Y%m%d%H}", {})
                frames = sum(per_field.values())
                if not per_field:
                    state = "missing"
                else:
                    state = "ok" if len(per_field) == n_fields else "partial"
            rows.append({
                "run_init": run_init.isoformat().replace("+00:00", "Z"),
                "state": state,
                "frames": frames,
                "due_at": due.isoformat().replace("+00:00", "Z"),
                "age_h": round((now - run_init).total_seconds() / 3600, 1),
            })
        rows.sort(key=lambda r: r["run_init"], reverse=True)
        out[model_id] = rows
    return {
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "lookback_h": lookback_h,
        "models": out,
    }


def write(now: datetime | None = None) -> None:
    import json

    from src.viz import frame_renderer
    try:
        path = frame_renderer.OUTPUT_DIR / "pipeline_coverage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(build(now), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
