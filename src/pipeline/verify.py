"""What "this run has genuinely been rendered" means, precisely.

Getting this wrong destroys unrecoverable data, so the definition is written
out here once, in one place, and every deletion decision goes through it.

A (model, run_init, step) is RENDER-VERIFIED when, for every field
supported_fields(model) says the model can structurally produce, a real PNG
exists on disk at frame_renderer's own output path and passes png_ok().

Two properties of the existing code make file existence a sound proof and
are load-bearing here:

  * render_frame() no longer writes placeholder PNGs (see its docstring: "a
    frame exists on disk == it has real data"). A missing file therefore
    means genuinely not rendered, never "rendered, nothing to show".
  * supported_fields() is derived from models.yaml, so the expected set never
    contains a field the model could not produce in the first place
    (arome_france's "total", ecmwf_ens's L/M/H split, everyone's
    prob_hml_composite but aifs_ens).

png_ok() additionally checks the PNG magic number and the IEND trailer, not
just non-zero size: a crash or a full disk during savefig() leaves a
truncated file behind, and a truncated frame must count as not-rendered.

Nothing in this module writes or deletes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.fetchers.base import (
    FETCH_TOPUP_WINDOW_H,
    format_init_dir,
    full_range_steps,
    steps_for_run,
)
from src.pipeline import fields as field_deps
from src.pipeline import journal, raw_layout
from src.viz import frame_renderer

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND\xaeB`\x82"
_MIN_PNG_BYTES = 1024


def png_ok(path: Path) -> bool:
    """A complete, non-truncated PNG - not merely a file that exists."""
    try:
        if path.stat().st_size < _MIN_PNG_BYTES:
            return False
        with open(path, "rb") as f:
            if f.read(len(_PNG_MAGIC)) != _PNG_MAGIC:
                return False
            f.seek(-len(_PNG_IEND), 2)
            return f.read(len(_PNG_IEND)) == _PNG_IEND
    except OSError:
        return False


def expected_fields(model_id: str) -> list[str]:
    """The fields this model must have frames for to count as rendered.

    Straight from frame_renderer.supported_fields() (models.yaml-derived) -
    never a second table here, per CLAUDE.md hard constraint #2.
    """
    return frame_renderer.supported_fields(model_id)


def frame_path(model_id: str, run_init: datetime, step: int, fld: str) -> Path:
    """Same convention render_frame() writes to. Read through the module
    attribute (not a from-import) so a test can retarget OUTPUT_DIR."""
    return (
        frame_renderer.OUTPUT_DIR
        / model_id
        / fld
        / f"{format_init_dir(run_init)}_{step:03d}.png"
    )


def is_renderable(model_id: str) -> bool:
    """Does frame_renderer have a reader for this model at all?

    aemet_harmonie (colour-ramp GeoTIFF) and the Open-Meteo point-API models
    have none, so no amount of rendering will ever produce a frame for them
    and their raw data can never be justified as reclaimable. Their raw is
    also tiny (155 MB of GeoTIFF, a few hundred KB of JSON, measured
    2026-07-27), so keeping it forever costs nothing.
    """
    return model_id in frame_renderer._MODEL_READERS


@dataclass
class StepVerdict:
    step: int
    rendered_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    # missing AND confirmed no-data by >= min_no_data_observations render
    # passes - a candidate for "structurally absent", subject to reclaim.py's
    # additional "the file is provably readable" test.
    confirmed_no_data_fields: list[str] = field(default_factory=list)
    # missing AND can never be produced again, because the earlier step's raw
    # this field differences against is already gone (see src/pipeline/
    # fields.py). Holding this step's raw for it would be pointless: it is
    # not the missing input.
    unproducible_fields: list[str] = field(default_factory=list)

    @property
    def fully_rendered(self) -> bool:
        """Rendered as completely as it ever can be. A field whose lookback
        input has been discarded counts as settled, not outstanding - the
        alternative is a run that is permanently 'incomplete' and pins its
        raw forever."""
        return not [f for f in self.missing_fields if f not in self.unproducible_fields]

    @property
    def genuinely_missing(self) -> list[str]:
        return [f for f in self.missing_fields if f not in self.unproducible_fields]


@dataclass
class RunVerdict:
    model: str
    run_init: datetime
    renderable: bool
    expected_fields: list[str]
    published_steps: list[int]
    steps_on_disk: list[int]
    reclaimed_steps: list[int]
    eclipse_steps: list[int]
    extracted: bool
    extraction_ready: bool
    verdicts: dict[int, StepVerdict]
    # True once no further step of this run will ever be published/fetched
    # (past the top-up window), which is what lets the successor rule below
    # release a step whose successor never arrived.
    sealed: bool = False

    def next_published_step(self, step: int) -> int | None:
        later = [s for s in self.published_steps if s > step]
        return min(later) if later else None

    def successor_satisfied(self, step: int) -> tuple[bool, str]:
        """May this step's raw be released as far as LATER steps' frames are
        concerned? See src/pipeline/fields.py - a differenced field (rain)
        makes step n's raw an input to step n+1's frame, so the raw outlives
        its own frames by exactly one step.

        Always True today: every rendered field is self-contained. The check
        exists so that adding rain does not require re-deriving the deletion
        rule under time pressure.
        """
        if not field_deps.needs_successor_rendered(self.expected_fields):
            return True, ""
        nxt = self.next_published_step(step)
        if nxt is None:
            return True, ""  # nothing can ever depend on it
        if nxt in self.reclaimed_steps:
            return True, ""  # successor already rendered and discarded
        verdict = self.verdicts.get(nxt)
        if verdict is not None and verdict.fully_rendered:
            return True, ""
        if self.sealed:
            # No further data will arrive for this run, so the successor's
            # frame will never be produced whatever we keep.
            return True, ""
        return False, (
            f"step {step} is a lookback input to step {nxt}, which is not "
            f"rendered yet"
        )

    @property
    def fully_rendered_steps(self) -> list[int]:
        return sorted(s for s, v in self.verdicts.items() if v.fully_rendered)

    @property
    def any_step_rendered(self) -> bool:
        return any(v.rendered_fields for v in self.verdicts.values())


def raw_files(model_id: str, run_init: datetime) -> list[Path]:
    """Every non-marker file currently in the run directory."""
    d = journal.run_dir(model_id, run_init)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and not raw_layout.is_marker(p.name))


def eclipse_target_steps(model_config: dict, run_init: datetime) -> set[int]:
    """The steps this run supplies for the eclipse archive valid hours - the
    ones points.parquet extraction reads. Empty when the run doesn't reach
    the eclipse day at all."""
    return {
        hit[0] for hit in steps_for_run(model_config, run_init).values() if hit is not None
    }


def _lookback_input_step(published: list[int], step: int, lookback: int) -> int | None:
    """The step whose raw a field with this lookback also reads (None if the
    step is early enough that there is none)."""
    if lookback <= 0:
        return None
    try:
        idx = published.index(step)
    except ValueError:
        return None
    return published[idx - lookback] if idx - lookback >= 0 else None


def verify_run(
    model_id: str,
    model_config: dict,
    run_init: datetime,
    *,
    min_no_data_observations: int = 2,
    now: datetime | None = None,
) -> RunVerdict:
    """Read-only status of one archived run: what is published, what is on
    disk, what has been rendered, what has been reclaimed already, and
    whether points.parquet extraction can (or did) happen."""
    # Imported lazily: src.extract.base pulls polars, and the fetchers'
    # package init pulls the GRIB stack - keeping this out of module scope
    # lets raw_layout/journal be imported in lighter contexts.
    from src.extract.base import already_extracted

    published = full_range_steps(model_config, run_init)
    on_disk = raw_files(model_id, run_init)
    by_step = raw_layout.group_files_by_step(
        model_id, model_config, run_init, [p.name for p in on_disk]
    )
    reclaimed = journal.reclaimed_steps(model_id, run_init)
    renderable = is_renderable(model_id)
    fields = expected_fields(model_id) if renderable else []

    now = now or datetime.now(UTC)
    sealed = (now - run_init).total_seconds() / 3600 > FETCH_TOPUP_WINDOW_H

    verdicts: dict[int, StepVerdict] = {}
    if renderable:
        for step in sorted(set(by_step) | set(published)):
            v = StepVerdict(step=step)
            for fld in fields:
                if png_ok(frame_path(model_id, run_init, step, fld)):
                    v.rendered_fields.append(fld)
                    continue
                v.missing_fields.append(fld)
                if (
                    journal.no_data_observations(model_id, run_init, step, fld)
                    >= min_no_data_observations
                ):
                    v.confirmed_no_data_fields.append(fld)
                # A differenced field (see src/pipeline/fields.py) also reads
                # an earlier step's raw. If that input is gone - reclaimed, or
                # simply never fetched for a run no longer being topped up -
                # this frame can never be produced, and holding THIS step's
                # raw would not help, since it is not the missing input.
                back = _lookback_input_step(published, step, field_deps.field_lookback(fld))
                if back is not None and back not in by_step and (back in reclaimed or sealed):
                    v.unproducible_fields.append(fld)
            verdicts[step] = v

    eclipse_steps = eclipse_target_steps(model_config, run_init)
    # Extraction now covers the FULL forecast range (points.parquet feeds the
    # point-forecast tool, not just the eclipse-day views), and it runs
    # INCREMENTALLY - as soon as a run has any step on disk, for freshness,
    # then again whenever higher steps arrive (orchestrator._maybe_extract
    # tracks how far it has extracted; the run is finalised once `sealed`).
    # So the readiness gate is simply "raw exists" - the legacy "the 3 eclipse
    # steps are present" gate (which never became true for a short-range run
    # that can't reach eclipse day, and made the newest run wait hours for its
    # late top-up steps) is gone. Each pass extracts before reclaim runs, so
    # every step's rows are captured while its raw is still on disk.
    return RunVerdict(
        model=model_id,
        run_init=run_init,
        renderable=renderable,
        expected_fields=fields,
        published_steps=published,
        steps_on_disk=sorted(by_step),
        reclaimed_steps=sorted(reclaimed),
        eclipse_steps=sorted(eclipse_steps),
        extracted=already_extracted(model_id, run_init),
        extraction_ready=bool(on_disk),
        verdicts=verdicts,
        sealed=sealed,
    )
