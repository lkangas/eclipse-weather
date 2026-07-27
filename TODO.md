# TODO — eclipse-weather

The live work list. **TASKS.md is now history**, not a todo: it holds the
research record (T01–T45) that `config/models.yaml` and code comments cite by
task number, so it stays where it is and keeps those references working. Look
there for *why* something is the way it is; look here for what's left.

Eclipse: 2026-08-12, totality ~18:25–18:33 UTC — but that is not the planning
horizon. The intent is to have everything important **built in the next few
days**; the archiver has been collecting since Jul 27, so data capture is
already handled and nothing below is paced by the eclipse date.

---

## Where things stand

All 15 fetchable models archive continuously (~21–44 runs each). Fetch →
top-up → render → manifest runs unattended: corrupt files are re-fetched,
runs that gain steps re-render, and all three tool manifests regenerate
themselves after every render pass. Tools 1/2/3 share one quantity control,
one cloud field per model, and an eclipse marker on their time axes.

The whole model-onboarding calendar TASKS.md scheduled across Jul 27 – Aug 11
is finished ahead of time.

---

## Blocked on a human

- [ ] **Reserve the VPS** (TASKS.md T25). Box decided 2026-07-22; nothing
      provisioned. Everything below under "VPS migration" waits on this.
- [ ] **Healthcheck service account.** `src/scheduler/run.py` already pings
      `HEALTHCHECK_URL` every tick; without an account a *stalled* archiver is
      silent. Reboots are covered (Task Scheduler), a hang is not. Minutes of
      work.
- [ ] **Sign off on how renderings look** (TASKS.md rollout step 2). The plan
      gates production migration on this, explicitly on judgement rather than
      a date.
- [ ] *Optional:* Météo-France API key. The unauthenticated data.gouv.fr
      mirror works and is what's in use; its automation terms are unconfirmed.

## VPS migration

**Shape (decided 2026-07-27):** the VPS runs fetch → render → delete with a
bounded disk; the desktop keeps running the archiver with its keep-forever
behaviour, as the raw archive of record. Anything that needs raw data — the
29-place series, line-of-sight from the full grids, pressure-level work — gets
developed and back-filled against the desktop, then shipped. That is what makes
deletion on the VPS safe: it stops being a one-way door, so neither the site
list nor T44 gates deployment any more.

Three things the insurance depends on, or it is not insurance:
- The desktop must not delete runs that matter. Routine bulk cleanup is fine,
  but exempt anything whose forecast reaches the eclipse valid time — those are
  the runs the whole archive exists for.
- The desktop must stay up. It does: Task Scheduler task `\WSL Autostart -
  eclipse-weather` fires **at system startup** (not at logon, deliberately),
  boots the distro, and Docker's `restart: unless-stopped` brings the archiver
  back. Verified 2026-07-27, last result 0. A *stalled* archiver is still
  silent though — that is the healthcheck item above, and it matters more now
  that this box holds the only copy of raw.
- Both instances write `points.parquet` and they will diverge. Treat the
  desktop's as authoritative (it is strictly more complete) and the VPS's as
  disposable.


- [ ] **Deploy the pipeline in dry-run** and watch it. Built and verified:
      `src/pipeline/`, `docker-compose.prod.yml`, `config/production.yaml`,
      55 fixture checks, peak in-flight raw 0.26 GB vs 16 GB per aifs_ens run.
      Never deletes without `--apply` **and** `reclaim.enabled`.
- [ ] **Seed production with existing renders** (~11k PNGs, ~540 MB). Decide:
      everything, or a recent window?
- [ ] **Let it delete for real.** Not a code or config change: it means
      starting the production container with the command
      `docker-compose.prod.yml` already carries —
      `python -m src.pipeline.run --loop --apply` — after reading a `--sweep`
      on that box and believing it. Without `--apply` every pass is plan-only:
      it works out exactly what it would delete, logs each file with a reason,
      and removes nothing. Two gates must both be open (`--apply` on the
      command line *and* `reclaim.enabled` in `config/production.yaml`, already
      true), so a box can be locked read-only from config alone.
      The site-list blocker is cleared — extraction now uses the curated 29
      places (145 series incl. WNW strips).
- [ ] **Status/monitoring page.** `rendered_index.json` and
      `pipeline_status.json` already exist as the data; this is mostly a page.
      Matters once the box is unattended.
- [ ] **Render-priority scheme** for the run-up: prefer dense short-range
      models (AROME, ICON-EU, HARMONIE) over coarse long-range ones once
      throughput can't keep up. Not needed until it is.

## Layers

- [ ] **Rain and temperature** — designs settled and demonstrated on real
      data; no code yet. Full spec, per-model costs and the implementation
      traps are in the task tracker (task #8) and the two review pages:
      `/rain_overlay_review.html`, `/temp_panel_review.html`.
      One fetch of Météo-France **SP1** delivers both temperature *and* rain
      for AROME and ARPEGE — AROME being the highest-resolution model over
      Iberia at 0.025°.
- [ ] **Line-of-sight calculation** (TASKS.md T44). Today's `wnw_strip` is a
      ground-projected line that treats every cloud level alike; at ~11° sun
      elevation low and high cloud cross the real sightline at very different
      distances. T44's pressure-level survey is the groundwork. *User has
      ideas here — hold until they raise it.*

## Housekeeping

- [ ] **Rename `viz/tool1_frames/`** to something tool-neutral. Rendering was
      decoupled from Tool 1 and the module already became `frame_renderer.py`;
      only the directory name is stale. Touches ~12k files, the served URLs
      and the static server's root, so it wants a quiet moment.
- [ ] **Verify anything still built on T37.** Three of its `status: confirmed`
      entries have now proved wrong — ARPEGE rain was Evaporation, ECMWF's
      param was `t2m` not `2t`, Météo-France's temperature was skin and in the
      wrong package. All three came from confirming a field's *name* without
      confirming what it *is*.

## Nowcast (eclipse day)

- [ ] **Aug 12 nowcast mode** — Meteosat imagery plus AEMET observations and
      radar alongside the model view. Genuinely future work; nothing before it
      depends on it.
