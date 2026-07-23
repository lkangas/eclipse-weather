"""T30 -- Availability Gantt chart.

One horizontal row per NWP model in config/models.yaml: a bar from when the
model starts being useful for the eclipse (first_covering + the conservative
publication_lag_h bound) out to the eclipse date, plus small tick marks at
every subsequent run's cycle time, colored by the local forecast-step cadence
at the eclipse valid time (see `cadence_at_lead`'s docstring for why this
module encodes cadence rather than the task spec's literal example metric).

Deliberately does NOT `import src.fetchers.base` (or anything under
src.fetchers/src.extract) -- that package's __init__ wires up the herbie-
based fetcher registry, which crashes on this Windows dev box (no
cfgrib/eccodes). The handful of helpers this module needs (`eclipse_t`,
the step-cadence arithmetic, `due_time`'s upper-lag convention) are small
enough to reimplement here directly, mirrored against src/fetchers/base.py's
real logic for consistency.

Static SVG via matplotlib (defaults, no styling effort) per CLAUDE.md's
stack notes -- plotly comes later. Every date comes from config/models.yaml
or from `now`; nothing is hardcoded.

Run directly:
    .venv\\Scripts\\python.exe src\\viz\\availability_gantt.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize

from src.config import REPO_ROOT, eclipse_config, load_models

OUT_PATH = REPO_ROOT / "data" / "viz" / "availability_gantt.svg"

# Entries with no cycles/first_covering of their own -- aggregator or
# reference-only, not a real archiver source with its own onboarding date.
SKIP_MODELS = {"open_meteo", "climatology"}

# aifs_ens shares aifs_single's cycle/steps table exactly (T02: "confirmed
# all 4 cycles, 2 dates checked" for both) but models.yaml never declares its
# own `first_covering`/`publication_lag_h` -- those two fields are simply
# missing from that entry. Rather than inventing a new date-threshold
# convention (models.yaml's own header comment and its per-model notes are
# not even mutually consistent about whether "covers T" means reaching
# 18:00Z or the exact 18:30Z eclipse moment -- see the module docstring test
# evidence), borrow the sibling's already human-reviewed values and flag it
# loudly rather than silently guessing.
SIBLING_FALLBACK = {"aifs_ens": "aifs_single"}

DEFAULT_ECLIPSE_T = "2026-08-12T18:30:00Z"


def eclipse_t() -> datetime:
    """Mirrors src/fetchers/base.py's eclipse_t(): ECLIPSE_T env var first,
    then config/models.yaml's eclipse.t, then a hardcoded last-resort
    default. Reimplemented here (not imported) to avoid the herbie import
    chain -- see module docstring."""
    raw = os.environ.get("ECLIPSE_T")
    if not raw:
        raw = eclipse_config().get("t", DEFAULT_ECLIPSE_T)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def parse_dt(s: str) -> datetime:
    """Parse models.yaml's compact cycle-time strings, e.g. '2026-07-09T00Z'
    or '2026-07-27T18Z', into aware UTC datetimes."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "T" in s and ":" not in s.split("T", 1)[1]:
        s += ":00"
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def cadence_at_lead(
    steps_spec: list[dict], lead_hours: float, cycle_max: float | None
) -> float | None:
    """The step cadence (every_h, in hours) that applies at a given lead time,
    capped by this cycle-hour's own max range -- or None if lead_hours falls
    outside what this run publishes at all.

    T30 suggests "how many hours the nearest available forecast step is from
    the true 18:30Z eclipse moment" as an example step-density/precision
    metric (a steps_for_run-style misalignment, reimplemented here as
    generate_available_steps()+nearest_step() equivalents from
    src/fetchers/base.py would compute it). Tried it first against the real
    models.yaml + eclipse.t: it is a CONSTANT 0.5h for literally every tick
    in this entire dataset (verified: `{o for r in rows for _, o in
    r.ticks}` == `{0.5}`, before this function existed). That's a genuine
    structural fact, not a bug -- every model here publishes whole-hour
    steps, every cycle inits on the hour, and the eclipse moment sits at a
    fixed :30, so the nearest integer step is always exactly half an hour
    off regardless of cadence -- but it makes that metric useless as a color
    encoding (no variation to show). This function encodes the local
    cadence itself instead: the more informative "step-density/precision"
    signal (1h vs 3h vs 6h bracketing around T) that actually varies across
    models and lead times."""
    if lead_hours < 0:
        return None
    cap = cycle_max if cycle_max is not None else float("inf")
    if lead_hours > cap:
        return None
    for seg in steps_spec:
        effective_to = min(seg["to_h"], cap)
        if lead_hours <= effective_to:
            return seg["every_h"]
        if effective_to >= cap:
            break
    return None


def upper_lag_hours(publication_lag_h) -> float:
    """The conservative (upper) bound of a models.yaml publication_lag_h pair."""
    if isinstance(publication_lag_h, list | tuple):
        return max(publication_lag_h) if publication_lag_h else 0.0
    return float(publication_lag_h)


def cycle_run_inits_forward(cycles: dict, start: datetime, end: datetime) -> list[datetime]:
    """Every run_init on this model's daily cycle grid (e.g. {"00":384,...})
    falling in [start, end], inclusive."""
    run_inits = []
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        for cycle_hour_str in cycles:
            candidate = datetime(day.year, day.month, day.day, int(cycle_hour_str), tzinfo=UTC)
            if start <= candidate <= end:
                run_inits.append(candidate)
        day += timedelta(days=1)
    return sorted(run_inits)


@dataclass
class ModelRow:
    name: str
    bar_start: datetime
    bar_end: datetime
    first_covering: datetime
    first_covering_source: str  # "declared" | "borrowed:<sibling>"
    ticks: list[tuple[datetime, float]]  # (run_init, local_step_cadence_hours)


def build_rows(models: dict, eclipse_time: datetime) -> list[ModelRow]:
    rows: list[ModelRow] = []
    for name, cfg in models.items():
        if name in SKIP_MODELS or "cycles" not in cfg or "steps" not in cfg:
            continue

        fc_source = "declared"
        if "first_covering" in cfg:
            first_covering = parse_dt(cfg["first_covering"])
            lag = cfg.get("publication_lag_h")
        else:
            sibling_name = SIBLING_FALLBACK.get(name)
            sibling = models.get(sibling_name, {}) if sibling_name else {}
            if "first_covering" not in sibling:
                raise ValueError(
                    f"{name}: no first_covering declared and no sibling fallback available"
                )
            first_covering = parse_dt(sibling["first_covering"])
            lag = cfg.get("publication_lag_h", sibling.get("publication_lag_h"))
            fc_source = f"borrowed:{sibling_name}"

        bar_start = first_covering + timedelta(hours=upper_lag_hours(lag))
        bar_end = eclipse_time

        cycles = cfg["cycles"]
        ticks: list[tuple[datetime, float]] = []
        for run_init in cycle_run_inits_forward(cycles, first_covering, eclipse_time):
            cycle_max = cycles.get(f"{run_init.hour:02d}")
            lead_hours = (eclipse_time - run_init).total_seconds() / 3600
            cadence = cadence_at_lead(cfg["steps"], lead_hours, cycle_max)
            if cadence is None:
                continue  # this cycle's own max range doesn't reach the eclipse moment
            ticks.append((run_init, cadence))

        rows.append(
            ModelRow(
                name=name,
                bar_start=bar_start,
                bar_end=bar_end,
                first_covering=first_covering,
                first_covering_source=fc_source,
                ticks=ticks,
            )
        )
    rows.sort(key=lambda r: r.bar_start)
    return rows


def plot(rows: list[ModelRow], eclipse_time: datetime, now: datetime, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(14, 0.55 * len(rows) + 2))

    all_cadences = [c for r in rows for _dt, c in r.ticks]
    vmin = min(all_cadences) if all_cadences else 0.0
    vmax = max(all_cadences) if all_cadences else 1.0
    norm = Normalize(vmin=vmin, vmax=vmax)
    cmap = plt.get_cmap("viridis_r")

    # earliest at top: plot in reverse row order since barh's y grows upward
    n = len(rows)
    for i, row in enumerate(rows):
        y = n - 1 - i
        ax.barh(
            y,
            width=mdates.date2num(row.bar_end) - mdates.date2num(row.bar_start),
            left=mdates.date2num(row.bar_start),
            height=0.5,
            color="tab:blue",
            alpha=0.25,
            edgecolor="tab:blue",
        )
        if row.ticks:
            xs = [mdates.date2num(dt) for dt, _off in row.ticks]
            ys = [y] * len(xs)
            cs = [off for _dt, off in row.ticks]
            ax.scatter(
                xs, ys, c=cs, cmap=cmap, norm=norm, marker="|", s=180, linewidths=1.5, zorder=3
            )

    ax.set_yticks(range(n))
    ax.set_yticklabels([rows[n - 1 - i].name for i in range(n)])
    ax.set_ylim(-0.5, n - 0.5)

    ax.axvline(
        mdates.date2num(eclipse_time),
        color="red",
        linestyle="--",
        linewidth=1,
        label="eclipse T (18:30 UTC)",
    )
    ax.axvline(mdates.date2num(now), color="gray", linestyle=":", linewidth=1, label="today")

    left_edge = min([r.bar_start for r in rows] + [now]) - timedelta(days=1)
    right_edge = eclipse_time + timedelta(days=1)
    ax.set_xlim(mdates.date2num(left_edge), mdates.date2num(right_edge))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=15))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate(rotation=45)

    ax.set_xlabel("date (UTC)")
    ax.set_title(
        f"Model availability for eclipse-day coverage "
        f"({eclipse_time.strftime('%Y-%m-%d %H:%M')} UTC totality)"
    )
    ax.legend(loc="upper left")
    ax.grid(axis="x", alpha=0.3)

    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, pad=0.01)
    cbar.set_label("local forecast-step cadence at the eclipse valid time (h; coarser = purple)")

    fig.tight_layout()
    fig.savefig(out_path, format=out_path.suffix.lstrip(".") or "svg")
    plt.close(fig)


def main() -> None:
    models_cfg = load_models()
    models = models_cfg["models"]
    eclipse_time = eclipse_t()
    now = datetime.now(UTC)

    rows = build_rows(models, eclipse_time)
    plot(rows, eclipse_time, now, OUT_PATH)

    print(f"eclipse T = {eclipse_time.isoformat()}")
    print(f"now = {now.isoformat()}")
    print(f"wrote {OUT_PATH} ({len(rows)} models)")
    print()
    print(f"{'model':<16} {'first_covering':<12} {'source':<20} {'bar_start':<20} {'n_ticks':>7}")
    for r in rows:
        print(
            f"{r.name:<16} {r.first_covering.strftime('%Y-%m-%d %H:%M'):<12} "
            f"{r.first_covering_source:<20} {r.bar_start.isoformat():<20} {len(r.ticks):>7}"
        )


if __name__ == "__main__":
    main()
