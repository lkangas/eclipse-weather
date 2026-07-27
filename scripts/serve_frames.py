"""Static file server for the Tool 1/2/3 rendered-frame directories, used for
the remote preview at http://redacted-host.example:8137/ (port forwarded from
this box - see TASKS.md/private ops notes).

Replaces plain `python -m http.server`, which defaults to HTTP/1.0 with no
keep-alive (a fresh TCP handshake per request) and sends no Cache-Control
header. Over a WAN round trip that makes scrubbing back to already-viewed
frames feel like it "freezes" - the browser's heuristic freshness window
for a just-written file is only a few seconds, so revisited frames get
silently revalidated (or refetched) one full handshake at a time instead of
served straight from cache.

PNG frames are content-addressed by (model, field, run_init, step) and are
never rewritten once produced by a given manifest-generation run, so they're
safe to mark immutable. manifest.json and index.html DO get overwritten in
place (regen runs, UI redeploys) and must not be cached.

Usage:
    python3 scripts/serve_frames.py [port] [directory]
    (defaults: 8137, cwd)
"""

from __future__ import annotations

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_NO_CACHE_NAMES = {
    "manifest.json", "index.html",
    "tool2_manifest.json", "tool2_index.html",
    "tool3_manifest.json", "tool3_index.html",
    "backfill_progress.json", "backfill_progress.html", "rendered_index.json",
    "render_status.html", "rendered_history.jsonl",
    # Production pipeline dashboard - all of these are rewritten in place every
    # pass, so none may be served immutable like the frames are.
    "pipeline.html", "pipeline_status.json", "pipeline_status.dryrun.json",
    "pipeline_activity.json", "pipeline_coverage.json", "pipeline_history.jsonl",
    "fetch_failures.jsonl",
    # Review/experiment pages are re-rendered in place while a design is being
    # iterated on, so they must never be served immutable like the frames are.
    "review.html", "review_grid.json",
    "rain_overlay_review.html", "rain_overlay_grid.json",
    "temp_panel_review.html", "temp_panel_grid.json",
}


class FrameRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # enables keep-alive - no per-request TCP handshake

    def guess_type(self, path):
        """Force UTF-8 on HTML.

        SimpleHTTPRequestHandler sends bare `text/html`, leaving the browser to
        guess the encoding - and it guessed Latin-1, so every en dash and middle
        dot in the dashboards rendered as mojibake. A <meta charset> in the page
        fixes one page; this fixes every page served from here.
        """
        t = super().guess_type(path)
        if t in ("text/html", "text/css") or str(t).startswith("text/html"):
            return t + "; charset=utf-8"
        return t

    def end_headers(self):
        if self.path.rsplit("/", 1)[-1].split("?", 1)[0] in _NO_CACHE_NAMES:
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        super().end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8137
    directory = sys.argv[2] if len(sys.argv) > 2 else "."
    def handler(*args, **kwargs):
        return FrameRequestHandler(*args, directory=directory, **kwargs)

    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"serving {directory!r} on 0.0.0.0:{port} "
          "(HTTP/1.1 keep-alive, immutable cache for frames)")
    server.serve_forever()


if __name__ == "__main__":
    main()
