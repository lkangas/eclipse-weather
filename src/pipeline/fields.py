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
is in place and tested so that adding rain is a one-line registration here
plus the renderer work, not a retrofit of the deletion logic.

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
    # When rain lands, register it here, e.g.:
    #   "rain": 1,
    #   "cloud_rain_composite": 1,
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
