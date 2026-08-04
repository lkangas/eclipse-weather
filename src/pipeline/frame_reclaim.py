"""Prune rendered frame PNGs down to the newest N run-inits per model.

Separate from reclaim.py on purpose. reclaim.py asks "is this raw file's
rendered product verifiably elsewhere" (per-file, always safe to answer from
disk state alone). This asks a different question - "is this RUN still
inside the window any tool actually displays" - which is a per-model,
count-based cutoff, not a per-file safety check. A pruned run's frames
cannot be regenerated once its raw has itself been reclaimed, so this is a
real deletion, never a "recoverable" one.

Why a count cutoff, and why 18 by default: Tool 2's own client
(src/viz/web/compare_runs.html) already caps its display at the newest
MAX_ROWS=18 runs per model, always truncating the oldest ("a fortnight-old
run is history, not comparison" - see that file's comment). Tool 3 is meant
to match the same window. Tool 1 only ever wants the newest 1-2 runs. So any
frame for a run older than the newest N is generated, stored, and served in
a manifest, but never shown to anyone - disk cost with no product behind it.
Confirmed on the live VPS 2026-08-04: models had 32-92 archived runs against
an 18-run display window, ~9 of the archive's 12 GB was already dead weight.

The cutoff is per-model, computed across ALL of that model's field
subdirectories together (not per-field independently) - a run must stay or
go as a unit, so Tool 2/3 never see a run with some fields present and
others silently missing because they happened to fall on different sides of
the line.

Nothing here runs automatically. config/production.yaml's
frames.max_runs_per_model defaults to None (keep everything); the pipeline
loop does not call this module at all yet. It exists so the same logic backs
both a one-off manual cleanup and, later, a real per-pass wiring - see
orchestrator.py's docstring for where that would plug in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRAME_RE = re.compile(r"^(?P<run_init>\d{10})_(?P<step>\d{3})\.png$")


@dataclass
class FrameCandidate:
    path: Path
    run_init: str  # YYYYMMDDHH, string - these are grouping keys, not datetimes
    bytes: int


@dataclass
class ModelFramePrunePlan:
    model: str
    keep_runs: int
    kept_run_inits: list[str] = field(default_factory=list)
    pruned_run_inits: list[str] = field(default_factory=list)
    to_prune: list[FrameCandidate] = field(default_factory=list)
    unparsed: list[Path] = field(default_factory=list)  # not matching the naming convention - never touched

    @property
    def bytes_to_prune(self) -> int:
        return sum(c.bytes for c in self.to_prune)


def plan_model_prune(model_dir: Path, model: str, keep_runs: int) -> ModelFramePrunePlan:
    """Every PNG under model_dir (one subdir per rendered field) whose
    run_init is not among the newest `keep_runs` distinct run_inits found
    for this model. Read-only - callers decide whether to apply()."""
    plan = ModelFramePrunePlan(model=model, keep_runs=keep_runs)
    if not model_dir.is_dir():
        return plan

    by_run: dict[str, list[FrameCandidate]] = {}
    for field_dir in model_dir.iterdir():
        if not field_dir.is_dir():
            continue
        for p in field_dir.iterdir():
            if not p.is_file():
                continue
            m = _FRAME_RE.match(p.name)
            if not m:
                plan.unparsed.append(p)
                continue
            run_init = m.group("run_init")
            by_run.setdefault(run_init, []).append(
                FrameCandidate(path=p, run_init=run_init, bytes=p.stat().st_size)
            )

    run_inits_sorted = sorted(by_run)  # YYYYMMDDHH sorts chronologically as a string
    plan.kept_run_inits = run_inits_sorted[-keep_runs:] if keep_runs > 0 else run_inits_sorted
    plan.pruned_run_inits = [r for r in run_inits_sorted if r not in set(plan.kept_run_inits)]
    for run_init in plan.pruned_run_inits:
        plan.to_prune.extend(by_run[run_init])
    return plan


def plan_all(frames_dir: Path, keep_runs: int, models: list[str] | None = None) -> list[ModelFramePrunePlan]:
    """One plan per model directory found under frames_dir."""
    if not frames_dir.is_dir():
        return []
    plans = []
    for model_dir in sorted(frames_dir.iterdir()):
        if not model_dir.is_dir() or model_dir.name.startswith("."):
            continue
        if models and model_dir.name not in models:
            continue
        plans.append(plan_model_prune(model_dir, model_dir.name, keep_runs))
    return plans


def apply_plan(plan: ModelFramePrunePlan) -> int:
    """Delete every candidate in plan.to_prune. Returns bytes freed."""
    freed = 0
    for c in plan.to_prune:
        try:
            c.path.unlink()
            freed += c.bytes
        except OSError:
            continue
    return freed
