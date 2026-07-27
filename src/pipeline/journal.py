"""Per-run bookkeeping that survives the deletion of the run's raw data.

Two small JSON files live in each run directory alongside the existing
`.extracted` / `.last_fetch_attempt` markers:

  .reclaimed.json      every raw file this pipeline deleted, with the steps it
                       carried, its size, and when. A TOMBSTONE.
  .render_journal.json how many consecutive render passes have found a given
                       (step, field) to have no data. Only used to tell a
                       structurally-absent step apart from a not-yet-rendered
                       one; see reclaim.py.

The tombstone is what resolves the direct conflict between two things that
are both correct in isolation:

  * a run stays eligible for TOP-UP fetches for 48 h, because providers
    publish a run's steps progressively (src/fetchers/base.py's note on
    GEFS's extended range landing ~25-27 h after init), and
  * production deletes a step's raw data minutes after rendering it.

Without a tombstone the two combine into a re-download loop: the top-up pass
sees no file, assumes the step was never fetched, and pulls those 300 MB
again - forever, every hour, for 48 h. With it, "do we already have this
file?" is answered by `raw_file_present()` in src/fetchers/base.py, which is
true when the file is on disk OR when it has been reclaimed after a verified
render. Absence-because-deleted and absence-because-never-fetched become
distinguishable, which is exactly what the top-up logic needs.

Note the run DIRECTORY is never removed, only its contents: `already_fetched()`
tests "directory exists and is non-empty", and these marker files are what
keep that true after the last GRIB is gone.

The tombstone deliberately records only files deleted BECAUSE THEY WERE
SUCCESSFULLY RENDERED. It must never be used to record a file removed for
any other reason (corruption, manual cleanup): re-fetching a corrupt file is
exactly what should happen, and a tombstone would suppress it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from src import config

RECLAIMED_FILE = ".reclaimed.json"
RENDER_JOURNAL_FILE = ".render_journal.json"

TOMBSTONE_VERSION = 1

# (path, mtime_ns) -> parsed json, so the fetchers' per-file lookups don't
# re-read and re-parse the manifest once per candidate download.
_CACHE: dict[Path, tuple[int, dict]] = {}


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_dir(model_id: str, run_init: datetime) -> Path:
    """The run's raw directory. Unlike src.fetchers.base.raw_output_dir this
    never creates it - planning must be able to run read-only."""
    return config.DATA_RAW / model_id / run_init.strftime("%Y%m%d%H")


def _read_json(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        _CACHE.pop(path, None)
        return {}
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime_ns:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    _CACHE[path] = (stat.st_mtime_ns, data)
    return data


def _write_json(path: Path, data: dict) -> None:
    """Atomic-ish write: temp file in the same directory, then replace, so a
    crash mid-write can never leave a half-parsed tombstone behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    _CACHE.pop(path, None)


# --------------------------------------------------------------------------
# tombstones
# --------------------------------------------------------------------------


def load_tombstone(model_id: str, run_init: datetime) -> dict:
    return _read_json(run_dir(model_id, run_init) / RECLAIMED_FILE)


def reclaimed_filenames(model_id: str, run_init: datetime) -> set[str]:
    return set((load_tombstone(model_id, run_init).get("files") or {}).keys())


def reclaimed_steps(model_id: str, run_init: datetime) -> set[int]:
    """Steps whose raw data was deleted after a verified render.

    Anything deciding "does this run still need work?" - a top-up fetch, a
    re-render of a run that gained steps, a corrupt-file scan - must treat
    these as DONE, not as missing.
    """
    steps: set[int] = set()
    for entry in (load_tombstone(model_id, run_init).get("files") or {}).values():
        steps.update(entry.get("steps") or [])
    return steps


def is_reclaimed(path: Path) -> bool:
    """Was this exact raw file deleted after a verified render?

    Called from the fetchers' per-file idempotency checks via
    src.fetchers.base.raw_file_present(), so it is on the hot path of every
    top-up pass; hence the mtime-keyed cache above.
    """
    data = _read_json(path.parent / RECLAIMED_FILE)
    return path.name in (data.get("files") or {})


def record_reclaimed(
    model_id: str,
    run_init: datetime,
    entries: dict[str, dict],
    now: datetime | None = None,
) -> Path:
    """Merge `entries` ({filename: {"steps": [...], "bytes": int,
    "frames": [...]}}) into this run's tombstone."""
    now = now or datetime.now(UTC)
    path = run_dir(model_id, run_init) / RECLAIMED_FILE
    data = dict(_read_json(path))
    files = dict(data.get("files") or {})
    for name, entry in entries.items():
        files[name] = {**entry, "reclaimed_at": _iso_z(now)}
    data.update(
        version=TOMBSTONE_VERSION,
        model=model_id,
        run_init=_iso_z(run_init),
        files=files,
        totals={
            "files": len(files),
            "bytes": sum(int(e.get("bytes") or 0) for e in files.values()),
        },
        updated_at=_iso_z(now),
    )
    _write_json(path, data)
    return path


# --------------------------------------------------------------------------
# render journal
# --------------------------------------------------------------------------


def load_render_journal(model_id: str, run_init: datetime) -> dict:
    return _read_json(run_dir(model_id, run_init) / RENDER_JOURNAL_FILE)


def no_data_observations(model_id: str, run_init: datetime, step: int, field: str) -> int:
    journal = load_render_journal(model_id, run_init)
    return int(((journal.get("no_data") or {}).get(str(step)) or {}).get(field, 0))


def record_render_pass(
    model_id: str,
    run_init: datetime,
    results: dict[int, dict[str, bool]],
    now: datetime | None = None,
) -> None:
    """Fold one render pass's {step: {field: has_data}} into the journal.

    has_data False increments that (step, field)'s consecutive no-data count;
    True clears it. reclaim.py needs several consecutive no-data observations
    before it will accept "this step is structurally absent from an otherwise
    readable file" - one observation could just as easily be a transient read
    failure, and render_frame() reports both the same way.
    """
    if not results:
        return
    now = now or datetime.now(UTC)
    path = run_dir(model_id, run_init) / RENDER_JOURNAL_FILE
    data = dict(_read_json(path))
    no_data: dict[str, dict[str, int]] = {
        k: dict(v) for k, v in (data.get("no_data") or {}).items()
    }
    for step, fields in results.items():
        key = str(step)
        for fld, has_data in fields.items():
            if has_data:
                if key in no_data:
                    no_data[key].pop(fld, None)
                    if not no_data[key]:
                        no_data.pop(key)
            else:
                no_data.setdefault(key, {})
                no_data[key][fld] = no_data[key].get(fld, 0) + 1
    data.update(no_data=no_data, updated_at=_iso_z(now), model=model_id)
    _write_json(path, data)
