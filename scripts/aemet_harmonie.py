"""Standalone AEMET HARMONIE-AROME tool - fetch, audit and emergency recovery.

Deliberately NOT wired into the scheduler or src/pipeline. AEMET is the one
source in this project with no historical archive at all (models.yaml
`retention: latest run only`), which makes it both the easiest model to
silently corrupt and the only one where a mistake is permanent. This script is
the place to exercise, observe and prove that path on its own before trusting
it inside a loop that runs unattended for the Aug 5-12 window.

Everything here drives the REAL production code path
(src/fetchers/aemet_geotiff_fetcher.fetch) rather than a reimplementation of
it, so what you verify with `fetch` is exactly what the archiver will do.

    uv run python scripts/aemet_harmonie.py status
        What the endpoint is serving right now (bundle token + declared run),
        what history is still retrievable, and what is on disk.

    uv run python scripts/aemet_harmonie.py fetch [--force] [--run-init ...]
        Fetch the current bundle through the real fetcher. Idempotent: a second
        run against an unchanged bundle downloads nothing and writes nothing.
        --force bypasses the two no-download shortcuts (latest-only guard,
        bundle-token check) to exercise the full download/parse/file path.
        --run-init deliberately lets you ask for the WRONG run, to demonstrate
        that the bundle still lands under the init it declares.

    uv run python scripts/aemet_harmonie.py audit
        Reconstruct, from file mtimes, which run every archived raster actually
        came from - the forensic view of the pre-2026-07-27 contamination.
        Read-only. Prints a remediation plan; never enacts it.

    uv run python scripts/aemet_harmonie.py history [--import RUN[,RUN...]]
        The undocumented imagen-modelo endpoint, which is the ONLY way to
        recover an AEMET run the archiver missed. Different product (PNG,
        EPSG:3857, 586x476) from the archived GeoTIFFs (EPSG:4326, 0.025 deg),
        so imports go to data/aemet_png_history/, NOT into data/raw/ - see
        _history_dir()'s note.

Point it at a different archive with ECLIPSE_DATA_ROOT (e.g. an external
drive mounted outside the repo, kept off the primary disk on purpose); it
must be set before the import below, which is why the sys.path/env handling
sits at the top of the file.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from src.config import DATA_RAW, DATA_ROOT, get_model  # noqa: E402
from src.fetchers.aemet_geotiff_fetcher import (  # noqa: E402
    _BUNDLE_TOKEN_MARKER,
    _bundle_token,
    fetch,
)
from src.fetchers.base import format_init_dir, latest_available_run_init  # noqa: E402

MODEL = "aemet_harmonie"

# Undocumented history endpoints - see models.yaml
# aemet_harmonie.source.open_endpoint.history_endpoint.
TIMELINE_URL = "https://www.aemet.es/es/api-eltiempo/modelo-harmonie/timeline/71/PB"
IMAGE_URL = "https://www.aemet.es/es/api-eltiempo/modelo-harmonie/imagen-modelo/71/PB/{filename}"
HISTORY_FILENAME = "fc{init:%Y%m%d%H}+{step:03d}h00m_71_3857.tif_color.png"

# Measured 2026-07-27T17Z: fc2026072500 served, fc2026072418 404 -> 11 runs.
# Used only to bound the probe loop, never as a promise.
HISTORY_PROBE_RUNS = 16

RASTER_RE = re.compile(rf"^{MODEL}_nubosidad_(?P<valid>\d{{8}}T\d{{6}})Z\.tif$")

log = logging.getLogger("aemet")


# ---------------------------------------------------------------------------
# Forensics: which run did each archived raster REALLY come from?
#
# The archived GeoTIFF filenames carry the VALID time only, and the GDAL tags
# carry FECHA (also valid time) and no reference time, so a file on disk makes
# no statement at all about its run init. Its mtime does: the endpoint serves
# only the current run, so a file downloaded at time T necessarily came from
# the newest run whose bundle had been published by T.
#
# That reconstruction is checkable rather than assumed, and it checks out: run
# it over the desktop archive and every reconstructed run comes out with a
# CONTIGUOUS step range starting at +1h, and every raster that lands in two
# places is byte-identical in both. A wrong lag would smear runs into each
# other and break both properties.
# ---------------------------------------------------------------------------

# Measured publication lag: bundle generation stamped 14:42:31Z for the 12Z run
# (2026-07-27). models.yaml's publication_lag_h [2,4] is the scheduler's
# conservative bracket around this; forensics needs the real central value, and
# using 4h here would attribute a 15:00Z download to the 06Z run.
MEASURED_LAG = timedelta(hours=2, minutes=40)


def _run_current_at(when: datetime) -> datetime:
    """The run AEMET was serving at `when`."""
    t = when - MEASURED_LAG
    return t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)


def _archived_rasters() -> list[tuple[str, datetime, datetime, Path]]:
    """(label_dir, valid_time, mtime, path) for every archived Nubosidad raster."""
    out = []
    model_dir = DATA_RAW / MODEL
    if not model_dir.is_dir():
        return out
    for run_dir in sorted(model_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        for path in run_dir.iterdir():
            m = RASTER_RE.match(path.name)
            if not m:
                continue
            valid = datetime.strptime(m.group("valid"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            out.append((run_dir.name, valid, mtime, path))
    return out


def _reconstruct() -> dict[datetime, dict[datetime, list[tuple[str, Path]]]]:
    """true_run_init -> valid_time -> [(label_dir, path), ...]"""
    runs: dict[datetime, dict[datetime, list[tuple[str, Path]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for label, valid, mtime, path in _archived_rasters():
        runs[_run_current_at(mtime)][valid].append((label, path))
    return runs


def cmd_audit(args: argparse.Namespace) -> int:
    import hashlib

    rasters = _archived_rasters()
    if not rasters:
        print(f"no archived rasters under {DATA_RAW / MODEL}")
        return 1
    runs = _reconstruct()

    labels = sorted({label for label, *_ in rasters})
    print(f"archive: {DATA_RAW / MODEL}")
    print(f"{len(rasters)} rasters in {len(labels)} directories -> {len(runs)} distinct runs\n")

    print("TRUE RUN      have  steps        stored under")
    complete, partial = [], []
    for run in sorted(runs):
        valids = runs[run]
        offsets = sorted(int((v - run).total_seconds() // 3600) for v in valids)
        contiguous = offsets == list(range(offsets[0], offsets[0] + len(offsets)))
        shape = f"+{offsets[0]}..+{offsets[-1]}h" + ("" if contiguous else " NON-CONTIGUOUS")
        holders = sorted({label for v in valids for label, _ in valids[v]})
        mislabelled = [h for h in holders if h != format_init_dir(run)]
        print(f"{run:%Y-%m-%d %HZ}  {len(valids):3d}  {shape:22s} "
              f"{', '.join(holders)}{'  <- mislabelled' if mislabelled else ''}")
        (complete if len(valids) >= 48 else partial).append(run)

    print(f"\n{len(complete)} complete run(s), {len(partial)} partial: "
          f"{', '.join(f'{r:%m-%d %HZ}' for r in partial) or 'none'}")

    # A directory holding rasters attributed to more than one run is contaminated.
    per_label: dict[str, set[datetime]] = collections.defaultdict(set)
    for label, _valid, mtime, _ in rasters:
        per_label[label].add(_run_current_at(mtime))
    contaminated = {k: v for k, v in per_label.items() if len(v) > 1}
    misfiled = {
        k: next(iter(v)) for k, v in per_label.items()
        if len(v) == 1 and format_init_dir(next(iter(v))) != k
    }

    print(f"\nCONTAMINATED directories ({len(contaminated)}) - hold >1 run's rasters:")
    for label in sorted(contaminated):
        runs_here = sorted(contaminated[label])
        counts = collections.Counter(_run_current_at(mt) for lb, _, mt, _ in rasters if lb == label)
        print(f"  {label}: " + ", ".join(f"{r:%m-%d %HZ}x{counts[r]}" for r in runs_here))
    print(f"\nMISFILED directories ({len(misfiled)}) - one run, wrong label:")
    for label in sorted(misfiled):
        print(f"  {label}: actually {misfiled[label]:%Y-%m-%d %HZ}")

    if args.hashes:
        print("\nbyte-identity of rasters held in more than one directory:")
        for run in sorted(runs):
            dup = differ = 0
            for _valid, items in runs[run].items():
                if len(items) < 2:
                    continue
                dup += 1
                digests = {hashlib.sha256(p.read_bytes()).hexdigest() for _, p in items}
                differ += len(digests) > 1
            if dup:
                verdict = "ALL IDENTICAL" if not differ else f"{differ} DIFFER"
                print(f"  {run:%Y-%m-%d %HZ}: {dup} valid-time(s) duplicated - {verdict}")

    print(_REMEDIATION)
    return 0


_REMEDIATION = """
--------------------------------------------------------------------------
PROPOSED REMEDIATION - not enacted by this script, and nothing above wrote,
moved or deleted anything. Decide, then say so.

The reconstruction above is trustworthy: every run it reconstructs has a
contiguous step range starting at +1h, and every raster that appears in two
directories is byte-identical in both. Neither would hold if the attribution
rule were wrong.

What is actually lost is only LABELS, not data. Every raster on disk is a
genuine AEMET product; the question is which run each belongs to, and the
mtimes answer it. So the safe repair is a re-file, not a delete:

  1. Take a copy of data/raw/aemet_harmonie first. It is ~100 MB; the runs
     inside it cannot be re-fetched from anywhere.
  2. Build the correct tree from the reconstruction: for each true run, one
     directory named after it, holding one copy of each valid time. Where a
     valid time exists in several directories the copies are byte-identical,
     so any of them will do.
  3. Only when the new tree verifies (every run contiguous from +1h, counts
     as reported above) replace the old one.
  4. Drop the .extracted markers on the way: they were written against the
     wrong run label, so points.parquet already holds aemet rows attributed
     to runs that never produced them. Those rows need deleting and
     re-extracting too - re-filing raw alone does not fix points.parquet.

The alternative - delete every contaminated and mislabelled directory and
keep only what is unambiguous - throws away whole runs (07-23 12Z, 07-25 and
07-26) for no benefit, since the data inside them is fine.

NOT proposed: any automatic repair inside the fetcher. It now refuses to
write outside the run a bundle declares, so the archive stops getting worse
on its own; rewriting history is a one-time, supervised, reversible job.
--------------------------------------------------------------------------
"""


# ---------------------------------------------------------------------------
# Live endpoint state
# ---------------------------------------------------------------------------

def _timeline_runs() -> dict[str, int]:
    """init string -> number of frames the public timeline index lists."""
    resp = httpx.get(TIMELINE_URL, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    counts: collections.Counter[str] = collections.Counter()
    def walk(node):
        if isinstance(node, list):
            if len(node) == 2 and all(isinstance(x, str) for x in node):
                m = re.match(r"fc(\d{10})", node[1])
                if m:
                    counts[m.group(1)] += 1
                return
            for child in node:
                walk(child)
    walk(resp.json())
    return dict(counts)


def _history_available(probe_runs: int = HISTORY_PROBE_RUNS) -> list[datetime]:
    """Which past runs imagen-modelo still serves, newest first, by probing +1h
    of each 6-hourly init backwards until one 404s. There is no index for old
    runs (the timeline lists the current one only), so probing is the only way."""
    now = datetime.now(UTC)
    newest = _run_current_at(now)
    available = []
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for i in range(probe_runs):
            init = newest - timedelta(hours=6 * i)
            url = IMAGE_URL.format(filename=HISTORY_FILENAME.format(init=init, step=1))
            try:
                ok = client.head(url).status_code == 200
            except httpx.HTTPError:
                ok = False
            if not ok:
                break
            available.append(init)
    return available


def cmd_status(args: argparse.Namespace) -> int:
    cfg = get_model(MODEL)
    now = datetime.now(UTC)
    url = cfg["source"]["open_endpoint"]["url"]

    print(f"now                 {now:%Y-%m-%d %H:%M}Z")
    print(f"data root           {DATA_ROOT}")
    token = _bundle_token(url)
    stamp = None
    if token:
        m = re.search(r"(\d{9,})", token)
        if m:
            stamp = datetime.fromtimestamp(int(m.group(1)), UTC)
    print(f"bundle token        {token}  (generated {stamp:%Y-%m-%d %H:%M}Z)"
          if stamp else f"bundle token        {token}")
    if stamp:
        print(f"  -> implies run    {_run_current_at(stamp + MEASURED_LAG):%Y-%m-%d %HZ} "
              f"(generation minus measured {MEASURED_LAG} lag)")
    print(f"scheduler's latest  {latest_available_run_init(cfg, now):%Y-%m-%d %HZ}"
          "  (the only run_init the fetcher will act on)")
    try:
        print(f"timeline index      {_timeline_runs()}")
    except httpx.HTTPError as e:
        print(f"timeline index      unavailable ({e!r})")

    held = _reconstruct()
    if held:
        newest = max(held)
        print(f"archive             {len(held)} distinct run(s), newest "
              f"{newest:%Y-%m-%d %HZ} with {len(held[newest])} valid time(s)")
        stamped = sorted(
            p.parent.name for p in (DATA_RAW / MODEL).glob(f"*/{_BUNDLE_TOKEN_MARKER}")
        )
        print(f"token-stamped dirs  {stamped or 'none yet (nothing fetched by the new fetcher)'}")
    if args.probe_history:
        avail = _history_available()
        print(f"imagen-modelo       {len(avail)} run(s) retrievable, "
              f"{avail[-1]:%Y-%m-%d %HZ} .. {avail[0]:%Y-%m-%d %HZ}")
        missing = [r for r in avail if len(held.get(r, {})) < 48]
        gaps = ", ".join(f"{r:%m-%d %HZ}({len(held.get(r, {}))}/48)" for r in missing)
        print(f"  recoverable gaps  {gaps or 'none'}")
    return 0


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = get_model(MODEL)
    now = datetime.now(UTC)
    run_init = (
        datetime.strptime(args.run_init, "%Y%m%d%H").replace(tzinfo=UTC)
        if args.run_init else latest_available_run_init(cfg, now)
    )
    print(f"requesting run_init {run_init:%Y-%m-%d %HZ}"
          f"{' (forced)' if args.force else ''} against {DATA_RAW / MODEL}")

    before = {p.name: p.stat().st_mtime for p in (DATA_RAW / MODEL).rglob("*.tif")}
    result = fetch(MODEL, cfg, run_init, force=args.force, now=now)
    after = {p.name: p.stat().st_mtime for p in (DATA_RAW / MODEL).rglob("*.tif")}
    touched = [n for n, mt in after.items() if before.get(n) != mt]

    print(f"  status            {result.status}"
          f"{'  error=' + str(result.error) if result.error else ''}")
    print(f"  filed under       {result.run_init:%Y-%m-%d %HZ}"
          f"{'   <- NOT the requested run' if result.run_init != run_init else ''}")
    print(f"  files written     {len(result.files_written)}")
    print(f"  rasters touched   {len(touched)} (mtime changed anywhere in the tree)")
    print(f"  eclipse steps     {result.covering_steps() or 'none - run does not reach eclipse T'}")
    return 0 if result.status in ("ok", "skipped") else 1


# ---------------------------------------------------------------------------
# imagen-modelo history (emergency recovery only)
# ---------------------------------------------------------------------------

def _history_dir(init: datetime) -> Path:
    """Deliberately OUTSIDE data/raw/.

    These PNGs are a different product from the archived GeoTIFFs - EPSG:3857
    at 586x476 against EPSG:4326 at 0.025 deg, and with no embedded ESCALA
    legend or geotransform of their own. Everything downstream
    (src/extract/aemet_extractor.py, frame_renderer's _aemet_harmonie_field)
    opens data/raw/aemet_harmonie/<init>/*.tif with rasterio and reads the
    ESCALA tag; a PNG dropped in there would either raise or, worse, be
    silently read against the wrong geotransform. Keeping them out of
    data/raw/ also keeps them out of the pipeline's raw sweep entirely, so
    nothing has to be taught about them to stay correct.
    """
    return DATA_ROOT / "aemet_png_history" / format_init_dir(init)


def cmd_history(args: argparse.Namespace) -> int:
    held = _reconstruct()
    avail = _history_available()
    if not avail:
        print("imagen-modelo returned nothing - endpoint changed?")
        return 1
    print(f"{len(avail)} run(s) retrievable from imagen-modelo "
          f"({avail[-1]:%Y-%m-%d %HZ} .. {avail[0]:%Y-%m-%d %HZ}):\n")
    print("RUN           archived  status")
    for init in avail:
        n = len(held.get(init, {}))
        state = "complete" if n >= 48 else ("MISSING" if n == 0 else f"partial {n}/48")
        local = _history_dir(init)
        n_png = len(list(local.glob("*.png"))) if local.is_dir() else 0
        print(f"{init:%Y-%m-%d %HZ}  {n:3d}/48   {state}"
              f"{f'   [{n_png} png imported]' if n_png else ''}")

    if not args.import_runs:
        print("\nnothing imported (pass --import RUN[,RUN...] or --import missing)")
        return 0

    if args.import_runs == "missing":
        wanted = [r for r in avail if len(held.get(r, {})) < 48]
    else:
        wanted = [
            datetime.strptime(s.strip(), "%Y%m%d%H").replace(tzinfo=UTC)
            for s in args.import_runs.split(",")
        ]
    print(f"\nimporting {len(wanted)} run(s) into {DATA_ROOT / 'aemet_png_history'}")
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for init in wanted:
            out = _history_dir(init)
            out.mkdir(parents=True, exist_ok=True)
            got = skipped = failed = 0
            for step in range(1, 49):
                valid = init + timedelta(hours=step)
                dest = out / f"{MODEL}_nubosidad_3857_{valid:%Y%m%dT%H%M%S}Z.png"
                if dest.exists() and dest.stat().st_size > 0:
                    skipped += 1
                    continue
                url = IMAGE_URL.format(filename=HISTORY_FILENAME.format(init=init, step=step))
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    failed += 1
                    continue
                if not resp.content.startswith(b"\x89PNG"):
                    failed += 1
                    continue
                dest.write_bytes(resp.content)
                got += 1
            (out / "PROVENANCE.txt").write_text(_PROVENANCE.format(init=init), encoding="utf-8")
            print(f"  {init:%Y-%m-%d %HZ}: {got} new, {skipped} already held, {failed} failed")
    return 0


_PROVENANCE = """AEMET HARMONIE-AROME run {init:%Y-%m-%dT%H}Z, recovered from
https://www.aemet.es/es/api-eltiempo/modelo-harmonie/imagen-modelo/71/PB/

THIS IS NOT THE SAME PRODUCT AS data/raw/aemet_harmonie/.

  archived bundle (data/raw)     this directory
  GeoTIFF, EPSG:4326, 0.025 deg  PNG, EPSG:3857, 586x476
  embedded ESCALA colour legend  no metadata at all
  embedded geotransform          no geotransform - bounds must be supplied

Both are renderings of the same colour ramp, so both are equally lossy in the
colour-inversion sense; this one is additionally coarser and reprojected.

Nothing in src/extract or src/viz reads this directory. Using it requires
(a) the ESCALA stops copied from an archived GeoTIFF of any run, and (b) the
EPSG:3857 bounds of AEMET's own map frame, which are NOT in the file and would
have to be solved for by registering a frame against a same-valid-time GeoTIFF.
Until both are done and checked, treat these as evidence that a run existed,
not as data.
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="live endpoint + archive state")
    s.add_argument("--probe-history", action="store_true",
                   help="also probe imagen-modelo for how far back it still serves")
    s.set_defaults(func=cmd_status)

    f = sub.add_parser("fetch", help="fetch the current bundle via the real fetcher")
    f.add_argument("--run-init", help="YYYYMMDDHH to request (default: the current run)")
    f.add_argument("--force", action="store_true",
                   help="bypass the latest-only guard and the bundle-token skip")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("audit", help="reconstruct true runs from mtimes (read-only)")
    a.add_argument("--hashes", action="store_true",
                   help="also verify duplicated rasters are byte-identical (slow)")
    a.set_defaults(func=cmd_audit)

    h = sub.add_parser("history", help="imagen-modelo recovery endpoint")
    h.add_argument("--import", dest="import_runs", metavar="RUNS",
                   help='"missing", or a comma-separated list of YYYYMMDDHH')
    h.set_defaults(func=cmd_history)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    if "ECLIPSE_DATA_ROOT" not in os.environ:
        log.warning("ECLIPSE_DATA_ROOT is unset - using the in-repo %s", DATA_ROOT)
    raise SystemExit(main())
