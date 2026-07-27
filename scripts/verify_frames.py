"""Find (and optionally delete) truncated frame PNGs.

render_frame() treats any existing frame file as already drawn, so a truncated
PNG is never redrawn on its own - it is a permanent hole that no amount of
re-running the renderer will fill. Deleting it is what makes it regenerable,
which is the entire point of this script.

That turns "restart the render workers" from a one-way risk into a recoverable
one: kill them, run this with --delete, and the next render pass redraws
whatever was mid-write. Frames written since the atomic-savefig change cannot
be truncated at all (temp file + os.replace), so on a fully restarted archiver
this should find nothing - a non-empty result on new frames means something
other than a killed worker is wrong.

Checks structure, not just size: a PNG must open with the 8-byte signature and
close with an IEND chunk. A file cut mid-write usually keeps a valid header,
so a signature-only check passes exactly the files this exists to catch.

    python -m scripts.verify_frames                  # report
    python -m scripts.verify_frames --delete         # report and remove
"""

from __future__ import annotations

import argparse
import sys

from src.viz.frame_renderer import OUTPUT_DIR

PNG_MAGIC = bytes([137, 80, 78, 71, 13, 10, 26, 10])
PNG_END = b"IEND\xaeB`\x82"


def is_intact(data: bytes) -> bool:
    return len(data) > 16 and data[:8] == PNG_MAGIC and data.rstrip()[-8:] == PNG_END


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--model", action="append", dest="models")
    args = ap.parse_args()

    roots = ([OUTPUT_DIR / m for m in args.models] if args.models
             else [p for p in OUTPUT_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])

    checked = bad = stray = 0
    for root in sorted(roots):
        if not root.is_dir():
            continue
        for png in sorted(root.rglob("*.png")):
            # Leftover temp files from a worker killed mid-write. Harmless -
            # they are dotfiles and never served - but worth clearing out.
            if png.name.startswith("."):
                stray += 1
                if args.delete:
                    png.unlink(missing_ok=True)
                continue
            checked += 1
            try:
                if is_intact(png.read_bytes()):
                    continue
            except OSError as exc:
                print(f"  UNREADABLE {png}: {exc}")
                bad += 1
                continue
            bad += 1
            rel = png.relative_to(OUTPUT_DIR)
            print(f"  TRUNCATED {rel} ({png.stat().st_size} bytes)"
                  + ("  -> deleted" if args.delete else ""))
            if args.delete:
                png.unlink(missing_ok=True)

    print(f"  checked {checked} frame(s): {bad} truncated, {stray} stray temp file(s)")
    if bad and not args.delete:
        print("  rerun with --delete to remove them; the next render pass redraws them")


if __name__ == "__main__":
    sys.exit(main())
