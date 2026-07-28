"""How many EARLIER steps' raw data a rendered field needs.

Every field rendered today is self-contained: one step's raw GRIB produces
one frame, so a step's raw may be discarded the moment that step's frames
exist. That is not a law of nature, and the pipeline must not assume it.

The known case (not implemented yet - see TASKS.md T37's rain/surface-temp
research) is PRECIPITATION. Every model in this registry publishes rain as an
ACCUMULATION since run start (or since a bucket boundary), never as a
per-step amount, so the displayable quantity is a difference of consecutive
accumulations:

    rain(step_n) = A(step_n) - A(step_n-1)

which makes a rain frame depend on TWO raw files. Three consequences the
reclaim design has to honour, and does:

  1. Step n-1's raw must not be deleted until step n has been rendered.
     src/pipeline/reclaim.py enforces this as the successor rule: a file is
     held while any step it carries still has an unrendered successor, unless
     the run is old enough that no further step will ever arrive.
     Cost: exactly one extra step's raw held per in-flight run.
  2. A frame whose lookback input has already been discarded can never be
     regenerated. src/pipeline/verify.py marks such a field UNPRODUCIBLE for
     that step rather than MISSING, so the pipeline neither retries it
     forever nor holds its raw forever - the deadlock the naive check falls
     into.
  3. A step whose predecessor was never fetched must render nothing for a
     differenced field (has_data=False), rather than a raw accumulation.
     That belongs to the renderer, not here, and is called out in the docs
     for whoever implements it.

Everything below is inert today (every entry is 0), by design: the mechanism
is in place and tested so that adding a differenced field is a one-line
registration here plus the renderer work, not a retrofit of the deletion logic.

temp and rain were registered 2026-07-28 and are both 0. That is not a
formality - an UNREGISTERED field falls to DEFAULT_LOOKBACK = 1, which made
every step's raw a lookback input to its successor and held the entire archive
from reclaim. scripts/verify_pipeline.py caught it before deployment; the
lesson is that adding a field to supported_fields() without adding it here is
silently fail-safe, not silently correct.

MERGE SEAM: if src/viz/frame_renderer.py ever declares its fields' inputs
itself, this table should read from there instead of restating it.
"""

from __future__ import annotations

# field name -> how many published steps back its render also reads.
# 0 = self-contained (every field that exists today).
LOOKBACK_STEPS: dict[str, int] = {
    "total": 0,
    "low": 0,
    "mid": 0,
    "high": 0,
    "hml_composite": 0,
    "prob_hml_composite": 0,
    "temp": 0,
    # rain is 0, NOT 1 - and the distinction is the whole reason PRATE was
    # chosen. This table was written expecting rain to be a differenced
    # accumulation (APCP at step n minus step n-1), which would make step n-1's
    # raw an input to step n's frame and force a lookback of 1. What shipped
    # reads the INSTANTANEOUS rate instead (models.yaml gfs.rain: PRATE,
    # step_type instant, rate true), so every rain frame is self-contained.
    # If an accumulation-differenced rain is ever added for another model, it
    # needs its OWN field name registered at 1 - do not change this entry.
    "rain": 0,
}

DEFAULT_LOOKBACK = 1
"""Unknown field -> assume it needs its predecessor. Erring toward holding an
extra file is recoverable; erring toward deletion is not."""


def field_lookback(field: str) -> int:
    return LOOKBACK_STEPS.get(field, DEFAULT_LOOKBACK)


def max_lookback(fields: list[str]) -> int:
    return max((field_lookback(f) for f in fields), default=0)


def needs_successor_rendered(fields: list[str]) -> bool:
    """Does any of these fields make step n's raw an input to a LATER step's
    frame? If so, that raw outlives its own frames."""
    return max_lookback(fields) > 0
