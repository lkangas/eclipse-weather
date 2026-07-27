"""Decide - and only on explicit request, perform - deletion of raw data
whose rendered product is verifiably on disk.

The single most important property of this module is that it refuses far
more often than it deletes. A file is reclaimed only when EVERY one of these
holds:

 1. The model is renderable at all (frame_renderer has a reader for it).
    aemet_harmonie and the Open-Meteo point models are never touched.
 2. The file's step membership is known (src/pipeline/raw_layout.py). An
    unrecognised filename is never deleted.
 3. Every step the file carries is render-verified: a complete PNG on disk
    for every field supported_fields() says the model can produce.
 4. ...or the step is provably STRUCTURALLY ABSENT: several independent
    render passes found no data for it AND another step carried by the same
    raw file rendered successfully, which proves the file itself is readable.
    This is what releases arome_france/arpege_europe's group files, whose
    +0h step carries no data by design (T34). For one-file-per-step models
    the condition is unsatisfiable by construction, which is the correct
    outcome: a present-but-unrenderable single-step file is a corruption
    suspicion and corrupt raw must be re-fetched, never quietly deleted.
 5. Every step it carries has had its SUCCESSOR step rendered, where any
    rendered field differences against an earlier step's raw. Accumulated
    precipitation is exactly that case (see src/pipeline/fields.py): step
    n's raw is an input to step n+1's rain frame, so it outlives its own
    frames by one step. Inert today - every field currently rendered is
    self-contained - but the rule is in force so that adding rain never
    requires re-deriving this module under time pressure.
 6. If the file carries an eclipse archive valid hour, the run has already
    been extracted into points.parquet. Site-level numbers are the one
    product that cannot be recovered from a rendered PNG.
 7. The file is older than reclaim.min_file_age_seconds, so nothing can be
    deleted while it is still being written.

Everything else is HELD, with a reason, and reported. Holding leaks disk;
disk is recoverable, an eclipse run is not.

Deletion also records a tombstone (src/pipeline/journal.py) so a later
top-up fetch does not re-download what was deliberately discarded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.pipeline import journal, raw_layout, verify
from src.pipeline.settings import Settings

log = logging.getLogger("pipeline.reclaim")

# Decision codes (stable strings - they end up in status JSON and logs).
RECLAIM = "reclaim"
HOLD_NOT_RENDERABLE = "hold:model-not-renderable"
HOLD_UNKNOWN_LAYOUT = "hold:unknown-file-layout"
HOLD_TOO_YOUNG = "hold:file-too-young"
HOLD_NOT_RENDERED = "hold:steps-not-rendered"
HOLD_UNREADABLE = "hold:no-frames-from-readable-file"
HOLD_AWAITING_EXTRACTION = "hold:awaiting-points-extraction"
HOLD_NO_EXPECTED_FIELDS = "hold:model-has-no-renderable-field"
HOLD_LOOKBACK_SUCCESSOR = "hold:successor-step-not-rendered"


@dataclass
class Candidate:
    path: Path
    steps: list[int]
    bytes: int
    decision: str
    reason: str = ""

    @property
    def reclaimable(self) -> bool:
        return self.decision == RECLAIM


@dataclass
class RunPlan:
    model: str
    run_init: datetime
    candidates: list[Candidate] = field(default_factory=list)
    needs_attention: list[str] = field(default_factory=list)

    @property
    def to_reclaim(self) -> list[Candidate]:
        return [c for c in self.candidates if c.reclaimable]

    @property
    def held(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.reclaimable]

    @property
    def bytes_to_reclaim(self) -> int:
        return sum(c.bytes for c in self.to_reclaim)

    @property
    def bytes_held(self) -> int:
        return sum(c.bytes for c in self.held)


def _step_released(
    v: verify.RunVerdict,
    step: int,
    file_steps: set[int],
    settings: Settings,
) -> tuple[bool, str, str]:
    """May this step's raw be dropped? -> (released, reason_if_not,
    hold_decision_code)."""
    verdict = v.verdicts.get(step)
    if verdict is None:
        # A step on disk that models.yaml does not list as published and that
        # nothing rendered. Unexpected shape - hold and report.
        return False, f"step {step} has no render verdict", HOLD_NOT_RENDERED

    if not verdict.fully_rendered:
        still_missing = [
            f for f in verdict.genuinely_missing if f not in verdict.confirmed_no_data_fields
        ]
        if still_missing:
            return (
                False,
                f"step {step} missing frames: {','.join(sorted(still_missing))}",
                HOLD_NOT_RENDERED,
            )
        # Every missing field is a confirmed no-data. Only believe that when
        # some OTHER step carried by this same file rendered - i.e. the file
        # is readable and the gap is structural rather than a bad download.
        siblings_ok = any(
            s != step and v.verdicts.get(s) is not None and v.verdicts[s].rendered_fields
            for s in file_steps
        )
        if not siblings_ok:
            return False, (
                f"step {step} produced no frames after "
                f"{settings.min_no_data_observations}+ passes and no other step in the "
                f"same file rendered - suspect unreadable/corrupt raw, not structural"
            ), HOLD_UNREADABLE

    # Its own frames are settled - but a LATER step's frame may still need
    # this step's raw (differenced fields, see src/pipeline/fields.py), so
    # being fully rendered is not on its own enough to release it.
    ok, why = v.successor_satisfied(step)
    if not ok:
        return False, why, HOLD_LOOKBACK_SUCCESSOR
    return True, "", ""


def plan_run(
    model_id: str,
    model_config: dict,
    run_init: datetime,
    settings: Settings,
    now: datetime | None = None,
    verdict: verify.RunVerdict | None = None,
) -> RunPlan:
    """Read-only. Decides, for each raw file of one run, reclaim or hold."""
    now = now or datetime.now(UTC)
    plan = RunPlan(model=model_id, run_init=run_init)
    files = verify.raw_files(model_id, run_init)
    if not files:
        return plan

    if not verify.is_renderable(model_id):
        for p in files:
            plan.candidates.append(
                Candidate(p, [], _size(p), HOLD_NOT_RENDERABLE,
                          "no frame_renderer reader for this model")
            )
        return plan

    v = verdict or verify.verify_run(
        model_id, model_config, run_init,
        min_no_data_observations=settings.min_no_data_observations,
        now=now,
    )

    if not v.expected_fields:
        for p in files:
            plan.candidates.append(
                Candidate(p, [], _size(p), HOLD_NO_EXPECTED_FIELDS,
                          "supported_fields() is empty for this model")
            )
        return plan

    eclipse_steps = set(v.eclipse_steps)
    age_cutoff = settings.min_file_age_seconds

    for p in files:
        steps = raw_layout.steps_in_file(model_id, model_config, run_init, p.name)
        size = _size(p)
        if steps is None:
            plan.candidates.append(
                Candidate(p, [], size, HOLD_UNKNOWN_LAYOUT,
                          "filename does not match any known fetcher convention")
            )
            plan.needs_attention.append(f"unknown raw filename: {p.name}")
            continue

        try:
            age_s = now.timestamp() - p.stat().st_mtime
        except OSError:
            continue
        if age_s < age_cutoff:
            plan.candidates.append(
                Candidate(p, sorted(steps), size, HOLD_TOO_YOUNG,
                          f"{age_s:.0f}s old, minimum {age_cutoff}s")
            )
            continue

        if settings.require_extraction_for_eclipse_steps and (steps & eclipse_steps):
            if not v.extracted:
                plan.candidates.append(
                    Candidate(p, sorted(steps), size, HOLD_AWAITING_EXTRACTION,
                              f"carries eclipse step(s) {sorted(steps & eclipse_steps)}; "
                              f"run not yet in points.parquet")
                )
                continue

        blockers: list[str] = []
        holds: list[str] = []
        for step in sorted(steps):
            released, why, hold_code = _step_released(v, step, set(steps), settings)
            if not released:
                blockers.append(why)
                holds.append(hold_code)
        if blockers:
            # A suspected-corrupt file is the one hold worth escalating; a
            # not-yet-rendered or waiting-on-successor file is routine.
            decision = (
                HOLD_UNREADABLE if HOLD_UNREADABLE in holds
                else HOLD_NOT_RENDERED if HOLD_NOT_RENDERED in holds
                else holds[0]
            )
            plan.candidates.append(
                Candidate(p, sorted(steps), size, decision, "; ".join(blockers))
            )
            if decision == HOLD_UNREADABLE:
                plan.needs_attention.append(f"{p.name}: {blockers[0]}")
            continue

        plan.candidates.append(Candidate(p, sorted(steps), size, RECLAIM))

    return plan


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def apply_plan(plan: RunPlan, settings: Settings, now: datetime | None = None) -> int:
    """Delete every RECLAIM candidate and tombstone it. Returns bytes freed.

    Refuses outright unless reclaim is enabled in config. Callers must ALSO
    have been given an explicit --apply; this function is never reached from
    the default (plan-only) path.
    """
    if not settings.reclaim_enabled:
        log.warning("reclaim disabled in config - nothing deleted for %s %s",
                    plan.model, plan.run_init.isoformat())
        return 0

    entries: dict[str, dict] = {}
    freed = 0
    fields = verify.expected_fields(plan.model)
    for c in plan.to_reclaim:
        try:
            c.path.unlink()
        except FileNotFoundError:
            pass  # already gone (concurrent pass) - still record the tombstone
        except OSError as e:
            log.error("failed to delete %s: %s", c.path, e)
            continue
        entries[c.path.name] = {"steps": c.steps, "bytes": c.bytes, "frames": fields}
        freed += c.bytes

    if entries:
        journal.record_reclaimed(plan.model, plan.run_init, entries, now=now)
        log.info(
            "reclaimed %d file(s), %.2f GB for %s %s",
            len(entries), freed / 1024**3, plan.model, plan.run_init.isoformat(),
        )
    return freed
