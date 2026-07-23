"""T31(a): Iberia map with the totality path overlaid, sites colored by a
chosen model's cloud value at its latest archived run. Deliberately simple
(plain matplotlib, plate-carree lat/lon axes, no cartopy/basemap dependency)
per explicit user direction - a functional prototype, not a polished map.

No runtime L/M/H toggle (this is a static image, not an interactive tool) -
call plot_eclipse_map() once per field you want to see; low/mid/high/total
are all supported via the `field` parameter.

Totality path data: config/totality_path.json, copied from the sibling
eclipse-dashboard repo's precomputed output (itself from eclipse-calc's
Besselian-element calculation, validated to sub-km accuracy for this exact
event) - see that file's own "source" field for full provenance. This
resolves TASKS.md's T33 well ahead of its original Aug 9-10 schedule, since
a validated calculation already existed in a sibling project.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib.pyplot as plt
import polars as pl

from src.config import DATA_ROOT, POINTS_PARQUET, REPO_ROOT, eclipse_config, load_sites

TOTALITY_PATH_JSON = REPO_ROOT / "config" / "totality_path.json"
OUTPUT_DIR = DATA_ROOT / "viz"

FIELD_COLUMN = {
    "low": "cloud_low",
    "mid": "cloud_mid",
    "high": "cloud_high",
    "total": "cloud_total",
}


def _load_totality_path() -> dict:
    with open(TOTALITY_PATH_JSON, encoding="utf-8") as f:
        return json.load(f)


def _latest_model_snapshot(model_name: str, field: str) -> pl.DataFrame | None:
    """One row per named site (WNW-strip points excluded): the given model's
    LATEST archived run_init, at whichever valid time is nearest eclipse.t,
    non-null `field`, preferring native provenance over derived when both
    exist for the same site/valid (matches T21's ecmwf_hres two-row design)."""
    if not POINTS_PARQUET.exists():
        return None
    col = FIELD_COLUMN[field]
    df = pl.read_parquet(POINTS_PARQUET)
    df = df.filter((pl.col("model") == model_name) & ~pl.col("site").str.contains("_wnw"))
    if df.height == 0:
        return None

    latest_run = df["run_init"].max()
    df = df.filter(pl.col("run_init") == latest_run).filter(pl.col(col).is_not_null())
    if df.height == 0:
        return None

    t = eclipse_config()["t"]
    target = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
    df = df.with_columns((pl.col("valid") - target).dt.total_seconds().abs().alias("_gap"))
    nearest_valid = df.sort("_gap")["valid"][0]
    df = df.filter(pl.col("valid") == nearest_valid)

    # native before derived when a site has both (only matters for ecmwf_hres)
    df = df.sort("provenance", descending=True)  # "total_only" < "native" < "derived" alpha-wise
    df = df.unique(subset=["site"], keep="first")
    return df


def plot_eclipse_map(
    model_name: str, field: str = "total", output_path: Path | None = None
) -> Path:
    if field not in FIELD_COLUMN:
        raise ValueError(f"field must be one of {sorted(FIELD_COLUMN)}, got {field!r}")

    path = _load_totality_path()
    sites = load_sites()["sites"]
    snap = _latest_model_snapshot(model_name, field)

    fig, ax = plt.subplots(figsize=(8, 8))

    north = path["northLimit"]
    south = path["southLimit"]
    band_lon = [p["lon"] for p in north] + [p["lon"] for p in reversed(south)]
    band_lat = [p["lat"] for p in north] + [p["lat"] for p in reversed(south)]
    ax.fill(band_lon, band_lat, color="0.85", label="totality band (N/S limits)", zorder=1)

    central = path["centralLine"]
    ax.plot(
        [p["lon"] for p in central],
        [p["lat"] for p in central],
        "k--",
        linewidth=1,
        label="central line",
        zorder=2,
    )

    site_lons = [s["lon"] for s in sites]
    site_lats = [s["lat"] for s in sites]
    col = FIELD_COLUMN[field]

    if snap is not None:
        by_site = {row["site"]: row[col] for row in snap.iter_rows(named=True)}
        values = [by_site.get(s["name"]) for s in sites]
        has_value = [v is not None for v in values]
        sc = ax.scatter(
            [lo for lo, hv in zip(site_lons, has_value, strict=True) if hv],
            [la for la, hv in zip(site_lats, has_value, strict=True) if hv],
            c=[v for v, hv in zip(values, has_value, strict=True) if hv],
            cmap="Blues",
            vmin=0,
            vmax=100,
            s=150,
            edgecolors="black",
            zorder=3,
        )
        fig.colorbar(sc, ax=ax, label=f"{model_name} cloud_{field} (%)", shrink=0.7)
        missing = [s["name"] for s, hv in zip(sites, has_value, strict=True) if not hv]
        if missing:
            ax.scatter(
                [s["lon"] for s in sites if s["name"] in missing],
                [s["lat"] for s in sites if s["name"] in missing],
                marker="x",
                color="red",
                s=100,
                label="no data",
                zorder=3,
            )
    else:
        ax.scatter(site_lons, site_lats, color="gray", s=150, edgecolors="black", zorder=3)
        ax.text(
            0.5, 0.02, f"(no archived {model_name} data yet for this field)",
            transform=ax.transAxes, ha="center", fontsize=9, color="red",
        )

    for s in sites:
        ax.annotate(
            s["name"], (s["lon"], s["lat"]), textcoords="offset points", xytext=(5, 5), fontsize=8
        )

    bbox = eclipse_config()["bbox"]
    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{model_name} cloud_{field} at eclipse valid time, over the totality path")
    ax.legend(loc="upper left", fontsize=8)

    output_path = output_path or (OUTPUT_DIR / f"eclipse_map_{model_name}_{field}.svg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    for _model in ["gfs", "ecmwf_hres", "icon_eu"]:
        for _field in ["total", "low"]:
            try:
                p = plot_eclipse_map(_model, _field)
                print(f"wrote {p}")
            except Exception as e:  # noqa: BLE001
                print(f"skipped {_model}/{_field}: {e}")
