"""T22 acceptance test for src/derive/humidity_to_cloud.py (TASKS.md T22 /
CLAUDE.md Hard Constraint #3): GFS carries BOTH native low/mid/high cloud
(LCDC/MCDC/HCDC) AND the pressure-level q/t humidity_to_cloud.py needs as
input. This script fetches a small, real, recent GFS pressure-level +
native-cloud sample via idx byte-range requests (never a full GRIB download,
per CLAUDE.md's herbie-style fetch-politeness convention), derives L/M/H from
the humidity fields, and diffs the result against GFS's own native L/M/H over
the Iberia bbox -- the calibration gate this module needs to pass before it
can be trusted on ecmwf_hres, which has no native L/M/H to check against.

Usage:
    python scripts/calibrate_humidity_to_cloud.py [--date YYYYMMDD --cycle HH --fhr FFF]

Defaults to the most recent GFS 0.25deg run (falling back a cycle at a time)
that has a published f024 index file.

This script is standalone test/calibration tooling, not part of the
importable src/ package, and is not wired into the scheduler.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import numpy as np  # noqa: E402

LEVELS_MB = [1000, 925, 850, 700, 500, 300]
WANTED = [(p, f"{lvl} mb") for lvl in LEVELS_MB for p in ("TMP", "SPFH")] + [
    ("LCDC", "low cloud layer"),
    ("MCDC", "middle cloud layer"),
    ("HCDC", "high cloud layer"),
]

SITES = {
    "Madrid": (40.42, -3.70),
    "Barcelona": (41.39, 2.16),
    "Lisbon": (38.72, -9.14),
    "Seville": (37.39, -5.99),
    "Zaragoza": (41.65, -0.88),
}

IBERIA_BBOX = {"lat_min": 36.0, "lat_max": 44.0, "lon_min": -10.0, "lon_max": 5.0}


def _ensure_eccodes_loadable() -> None:
    """On Linux/production, eccodes is expected to resolve normally (system
    package or conda). On a bare Windows dev box there is usually no system
    ecCodes install; if the plain import fails, fall back to a pip-installed
    `ecmwflibs` wheel (bundles eccodes.dll + all its transitive DLL deps) and
    monkeypatch findlibs to point at it. No-op, and silent, everywhere else."""
    try:
        import eccodes  # noqa: F401

        return
    except Exception:
        pass

    if sys.platform != "win32":
        raise RuntimeError(
            "Cannot import eccodes and this is not Windows -- install a real "
            "ecCodes (system package or conda-forge eccodes) rather than "
            "relying on the Windows-only ecmwflibs fallback this script has."
        )

    try:
        import findlibs
    except ImportError as e:
        raise RuntimeError("findlibs not installed (should be a cfgrib dependency)") from e

    try:
        import ecmwflibs
    except ImportError as e:
        raise RuntimeError(
            "No system eccodes AND no `ecmwflibs` package installed. On Windows, "
            "either install a real ecCodes (e.g. conda-forge's `eccodes`, then set "
            "ECCODES_DIR), or `pip install ecmwflibs` into this venv for local "
            "testing only (do not add it to pyproject.toml/uv.lock -- production "
            "targets Linux, where a real eccodes install is expected to work)."
        ) from e

    ecmwflibs_dir = Path(ecmwflibs.__file__).resolve().parent
    _orig_find = findlibs.find

    def _patched_find(lib_name, pkg_name=None):
        if lib_name in ("eccodes", "libeccodes"):
            candidate = ecmwflibs_dir / "eccodes.dll"
            if candidate.exists():
                return str(candidate)
        return _orig_find(lib_name, pkg_name)

    findlibs.find = _patched_find
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(ecmwflibs_dir))


def parse_idx(text: str):
    rows = []
    for line in text.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    return rows


def find_recent_run_and_fetch(client: httpx.Client, out_path: Path):
    """Walk backward from now looking for the most recent GFS cycle whose
    f024 idx is published, then byte-range-fetch just the messages we need."""
    now = datetime.now(UTC)
    for hours_back in range(0, 48, 6):
        candidate = now - timedelta(hours=hours_back)
        candidate = candidate.replace(minute=0, second=0, microsecond=0)
        hh = f"{(candidate.hour // 6) * 6:02d}"
        yyyymmdd = candidate.strftime("%Y%m%d")
        base = (
            f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{yyyymmdd}/{hh}/atmos/"
            f"gfs.t{hh}z.pgrb2.0p25.f024"
        )
        idx_url = base + ".idx"
        r = client.get(idx_url)
        if r.status_code != 200:
            continue
        print(f"Using GFS run {yyyymmdd} {hh}Z f024 (idx: {idx_url})")
        rows = parse_idx(r.text)
        messages = []
        for i, (_line_no, offset, param, level, fcst) in enumerate(rows):
            if not re.match(r"^\d+ hour fcst$", fcst):
                continue
            for want_param, want_level in WANTED:
                if param == want_param and level == want_level:
                    end = rows[i + 1][1] - 1 if i + 1 < len(rows) else None
                    messages.append((offset, end))
        if len(messages) != len(WANTED):
            continue
        with open(out_path, "wb") as out:
            for start, end in messages:
                range_header = f"bytes={start}-{end if end is not None else ''}"
                resp = client.get(base, headers={"Range": range_header})
                resp.raise_for_status()
                out.write(resp.content)
        return yyyymmdd, hh
    raise RuntimeError("Could not find a recent GFS run with a published f024 idx in the last 48h")


def bbox_flat(da, lat_slice, lon_ranges):
    parts = [da.sel(latitude=lat_slice).sel(longitude=slice(*r)).values.ravel() for r in lon_ranges]
    return np.concatenate(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grib", type=Path, default=None, help="reuse an already-downloaded sample"
    )
    args = parser.parse_args()

    _ensure_eccodes_loadable()
    import cfgrib

    from src.derive.humidity_to_cloud import derive_cloud_fractions

    grib_path = args.grib
    if grib_path is None:
        # Test-only scratch file, deliberately kept OUT of data/raw/ -- that
        # tree is the archiver's own {model}/{initYYYYMMDDHH}/ layout
        # (CLAUDE.md repo layout), not a place for this script's temp sample.
        grib_path = Path(tempfile.gettempdir()) / "eclipse_weather_calibration_gfs_sample.grib2"
        with httpx.Client(timeout=60) as client:
            find_recent_run_and_fetch(client, grib_path)

    datasets = cfgrib.open_datasets(str(grib_path))
    qt_ds = next(d for d in datasets if "q" in d.data_vars and "t" in d.data_vars)
    lcc = next(d for d in datasets if "lcc" in d.data_vars)["lcc"]
    mcc = next(d for d in datasets if "mcc" in d.data_vars)["mcc"]
    hcc = next(d for d in datasets if "hcc" in d.data_vars)["hcc"]

    derived = derive_cloud_fractions(qt_ds)

    print(f"\nSite-level comparison (derived vs native), {len(SITES)} Iberia cities:")
    for name, (lat, lon) in SITES.items():
        lon360 = lon % 360.0
        sel = {"latitude": lat, "longitude": lon360, "method": "nearest"}
        dl = float(derived["cloud_low"].sel(**sel).values)
        dm = float(derived["cloud_mid"].sel(**sel).values)
        dh = float(derived["cloud_high"].sel(**sel).values)
        nl = float(lcc.sel(**sel).values)
        nm = float(mcc.sel(**sel).values)
        nh = float(hcc.sel(**sel).values)
        print(
            f"  {name:10s} L: {dl:6.1f} vs {nl:6.1f}   M: {dm:6.1f} vs {nm:6.1f}   "
            f"H: {dh:6.1f} vs {nh:6.1f}"
        )

    lat_slice = slice(IBERIA_BBOX["lat_max"], IBERIA_BBOX["lat_min"])
    lon_ranges = [(0.0, IBERIA_BBOX["lon_max"]), (360.0 + IBERIA_BBOX["lon_min"], 359.75)]

    print(f"\nFull Iberia bbox {IBERIA_BBOX} calibration stats (derived vs native GFS):")
    for band_name, d_da, n_da in [
        ("low", derived["cloud_low"], lcc),
        ("mid", derived["cloud_mid"], mcc),
        ("high", derived["cloud_high"], hcc),
    ]:
        d_flat = bbox_flat(d_da, lat_slice, lon_ranges)
        n_flat = bbox_flat(n_da, lat_slice, lon_ranges)
        mad = float(np.mean(np.abs(d_flat - n_flat)))
        bias = float(np.mean(d_flat - n_flat))
        rmse = float(np.sqrt(np.mean((d_flat - n_flat) ** 2)))
        corr = float(np.corrcoef(d_flat, n_flat)[0, 1])
        print(
            f"  {band_name:5s} (n={d_flat.size}): mean|diff|={mad:6.2f}pp  bias={bias:6.2f}pp  "
            f"RMSE={rmse:6.2f}  corr={corr:5.2f}"
        )


if __name__ == "__main__":
    main()
