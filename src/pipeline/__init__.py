"""Production (VPS) fetch -> render -> verify -> reclaim pipeline.

This package is the production deployment shape described in TASKS.md's
"Tool 1/2/3 rollout" step 4. It is DELIBERATELY separate from
src/scheduler/run.py, which stays the desktop archiver and keeps its
"fetch and keep raw forever" behaviour (CLAUDE.md's explicit dev-phase
"disk isn't constrained here" direction).

The two share every building block - the fetcher registry, the extract
registry, src/viz/frame_renderer.py, and the manifest scripts - and differ
only in sequencing:

    desktop     : fetch whole run -> extract -> render -> keep raw forever
    production  : fetch a WINDOW of a run -> render that window -> verify the
                  frames exist on disk -> delete that window's raw -> next
                  window; extract before any eclipse-hour raw is deleted

Nothing here imports src/scheduler/run.py, and nothing here is reachable
from the desktop entrypoint, so a production-only bug cannot take the
desktop archiver down and vice versa.

Deletion is opt-in twice over: config/production.yaml's reclaim.enabled must
be true AND the orchestrator must be run with --apply. Everything defaults to
plan/dry-run.
"""
