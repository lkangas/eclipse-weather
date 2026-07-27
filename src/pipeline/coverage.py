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


def _has_frames(model_id: str, run_init: datetime) -> tuple[bool, int]:
    from src.viz.frame_renderer import OUTPUT_DIR, supported_fields
    stamp = f"{run_init:%Y%m%d%H}_"
    total = 0
    fields_hit = 0
    for field in supported_fields(model_id):
        d = OUTPUT_DIR / model_id / field
        if not d.is_dir():
            continue
        n = sum(1 for p in d.iterdir() if p.name.startswith(stamp) and p.suffix == ".png")
        if n:
            fields_hit += 1
            total += n
    return fields_hit > 0, total


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
        rows = []
        for run_init in cycle_run_inits(cfg["cycles"], now, lookback_hours=lookback_h):
            due = due_time(cfg.get("publication_lag_h", [0, 0]), run_init)
            if not renderable:
                state, frames = "no-render", 0
            elif now < due:
                state, frames = "pending", 0
            else:
                any_frames, frames = _has_frames(model_id, run_init)
                if not any_frames:
                    state = "missing"
                else:
                    hit = sum(
                        1 for f in supported_fields(model_id)
                        if _field_has(model_id, f, run_init)
                    )
                    state = "ok" if hit == n_fields else "partial"
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


def _field_has(model_id: str, field: str, run_init: datetime) -> bool:
    from src.viz.frame_renderer import OUTPUT_DIR
    d = OUTPUT_DIR / model_id / field
    if not d.is_dir():
        return False
    stamp = f"{run_init:%Y%m%d%H}_"
    return any(p.name.startswith(stamp) for p in d.iterdir())


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
