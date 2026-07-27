"""Durable record of per-step fetch failures, and the throttle built on it.

Why this exists: a step that upstream will never publish was being retried on
every window of every pass, forever. The pipeline's windows are cumulative
(`steps <= cap`), so each window re-offers every earlier step to the fetcher;
already-fetched ones are skipped cheaply by raw_file_present(), but a step that
never lands is never "already fetched", so it is attempted again every time.
Measured on the live VPS: gefs_extended f000/total and f000/levels (NOAA does
not publish those two products at f000 for that run) cost ~7 s of retries and
backoff on each of ~35 windows per run.

`should_attempt_fetch()` in src/fetchers/base.py cannot fix this: it throttles
per (model, run), and the whole point of a top-up window is that the run IS
still worth re-fetching - just not that step.

Deliberately pipeline-side only. The fix works by narrowing the step list
BEFORE the fetcher is called, so nothing in src/fetchers/ changes and the
desktop archiver's behaviour is untouched. CLAUDE.md's first hard constraint is
that the archiver stays reliable; a dashboard feature has no business editing
its critical path.

The ledger doubles as the dashboard's fetch-failure table - these gaps
previously existed only as log lines, which is how icon_eu/icon_global
2026-07-25T12Z was lost for 36 hours before anyone noticed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

LEDGER_FILENAME = "fetch_failures.jsonl"

# Attempts before a step is considered dead and skipped. Three passes is enough
# to distinguish "upstream is briefly 500ing" from "this product does not
# exist", without wasting many minutes on the latter.
DEAD_AFTER_ATTEMPTS = 3

# A dead step is retried once a day even so: upstream backfills happen, and a
# permanent skip would turn a transient outage into permanent data loss.
DEAD_RETRY_AFTER_H = 24.0

# "f000/total: downloaded file missing..." / "f123/levels: ..."
_STEP_RE = re.compile(r"\bf(\d{1,4})/(\w+)\s*:")


def _ledger_path() -> Path:
    from src.viz import frame_renderer
    return frame_renderer.OUTPUT_DIR / LEDGER_FILENAME


def _state_path(model_id: str, run_init: datetime) -> Path:
    from src.config import DATA_RAW
    return DATA_RAW / model_id / f"{run_init:%Y%m%d%H}" / ".fetch_failures.json"


def parse_step_failures(error: str | None) -> dict[int, list[str]]:
    """Pull `{step_hours: [product, ...]}` out of a FetchResult.error string.

    Returns {} for None, for an empty string, or for an error that names no
    step at all (a whole-run failure - a dead URL, an auth rejection). Those
    must NOT be attributed to a step: skipping steps because the API key
    expired would quietly stop archiving the model.
    """
    if not error:
        return {}
    out: dict[int, list[str]] = {}
    for step_s, product in _STEP_RE.findall(error):
        out.setdefault(int(step_s), []).append(product)
    return out


def record(model_id: str, run_init: datetime, error: str | None, now: datetime) -> None:
    """Note this pass's per-step failures against the run, and append the new
    ones to the ledger the dashboard reads."""
    failures = parse_step_failures(error)
    if not failures:
        return

    path = _state_path(model_id, run_init)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    fresh = []
    for step, products in failures.items():
        key = str(step)
        entry = state.get(key) or {"attempts": 0, "products": [], "first_seen": None}
        entry["attempts"] += 1
        entry["products"] = sorted(set(entry["products"]) | set(products))
        entry["last_seen"] = now.isoformat().replace("+00:00", "Z")
        if entry["first_seen"] is None:
            entry["first_seen"] = entry["last_seen"]
            fresh.append((step, entry))
        state[key] = entry

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        return  # never let bookkeeping break a fetch

    # Only newly-seen steps go to the ledger, so a step failing on 35 windows
    # is one line rather than 35.
    if fresh:
        try:
            lp = _ledger_path()
            lp.parent.mkdir(parents=True, exist_ok=True)
            with lp.open("a", encoding="utf-8") as fh:
                for step, entry in fresh:
                    fh.write(json.dumps({
                        "at": entry["first_seen"],
                        "model": model_id,
                        "run_init": run_init.isoformat().replace("+00:00", "Z"),
                        "step": step,
                        "products": entry["products"],
                    }) + "\n")
        except OSError:
            pass


def dead_steps(model_id: str, run_init: datetime, now: datetime) -> set[int]:
    """Steps to leave out of the next fetch window for this run."""
    try:
        state = json.loads(_state_path(model_id, run_init).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    dead = set()
    for key, entry in state.items():
        if entry.get("attempts", 0) < DEAD_AFTER_ATTEMPTS:
            continue
        last = entry.get("last_seen")
        if last:
            try:
                seen = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if now - seen >= timedelta(hours=DEAD_RETRY_AFTER_H):
                    continue  # earn one retry back
            except ValueError:
                pass
        try:
            dead.add(int(key))
        except ValueError:
            continue
    return dead


def summary(limit: int = 200) -> list[dict]:
    """Recent ledger entries, newest first - the dashboard's failure table."""
    try:
        rows = _ledger_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(rows):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
