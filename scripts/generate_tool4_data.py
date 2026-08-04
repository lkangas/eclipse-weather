"""Tool 4 data export: points.parquet -> a small index + per-(model,run) JSON.

Layout written under DATA_ROOT/viz/frames/tool4/ (the served tree):

    tool4/index.json                 # models, their runs, available quantities
    tool4/<model>/<run>.json         # one immutable file per (model, run)

See docs/tool4-point-forecast-plan.md (v2) for the why: per-(model,run) files
are immutable (long-cacheable) and let the client fetch only what it plots.
Deterministic models store one value per (site, valid); ensemble models
(ecmwf_ens, aifs_ens) additionally store per-member arrays plus the mean.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import polars as pl

from src.config import DATA_ROOT, POINTS_PARQUET, eclipse_config, load_models

OUT_DIR = DATA_ROOT / "viz" / "frames" / "tool4"

# quantity key -> points.parquet column
QMAP = {
    "temp": "temp_c",
    "total": "cloud_total",
    "low": "cloud_low",
    "mid": "cloud_mid",
    "high": "cloud_high",
}
# ensemble model -> its paired high-res deterministic model (for the overlay line)
DET_PAIR = {"ecmwf_ens": "ecmwf_hres", "aifs_ens": "aifs_single"}

# A run older than this (hours) is past its fetch top-up window, so its
# extracted rows no longer change - its JSON need not be rewritten each pass.
_STABLE_AFTER_H = 50.0


def _iso(v: datetime) -> str:
    return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_stamp(v: datetime) -> str:
    return v.astimezone(UTC).strftime("%Y%m%d%H")


def _model_label(model_id: str, models_cfg: dict) -> str:
    # A short display label; fall back to the id if models.yaml has no nicer name.
    return models_cfg.get(model_id, {}).get("display_name", model_id)


def _mean(cols: list[list[float | None]], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(n):
        vals = [c[i] for c in cols if c[i] is not None]
        out.append(round(sum(vals) / len(vals), 1) if vals else None)
    return out


def export_run(model: str, run_init: datetime, sub: pl.DataFrame) -> dict:
    """Build the per-(model,run) JSON payload for one run's rows."""
    valids = sorted(sub["valid"].unique().to_list())
    vindex = {v: i for i, v in enumerate(valids)}
    n = len(valids)
    qcols = list(QMAP.values())

    # one pass: (site, member) -> {qkey: [values aligned to valid axis]}
    acc: dict[tuple[str, int], dict[str, list]] = {}
    for row in sub.select(["site", "member", "valid", *qcols]).iter_rows():
        site, member, valid = row[0], row[1], row[2]
        vi = vindex[valid]
        obj = acc.get((site, member))
        if obj is None:
            obj = {q: [None] * n for q in QMAP}
            acc[(site, member)] = obj
        for j, qkey in enumerate(QMAP):
            val = row[3 + j]
            if val is not None:
                obj[qkey][vi] = round(float(val), 1)

    sites = sorted({s for (s, _m) in acc})
    all_members = sorted({m for (_s, m) in acc})
    # "ensemble" means MORE THAN ONE member. A single non-(-1) member is a
    # control-only model (gefs_extended is member 0 = GEFS c00 control), which
    # is deterministic for display purposes - not spaghetti-of-one.
    is_ens = len(all_members) > 1
    sole_member = all_members[0] if all_members else -1
    sites_out: dict[str, dict] = {}
    for site in sites:
        site_obj: dict[str, list] = {}
        if not is_ens:
            det = acc.get((site, sole_member), {})
            for qkey in QMAP:
                arr = det.get(qkey)
                if arr and any(v is not None for v in arr):
                    site_obj[qkey] = arr
        else:
            members = sorted({m for (s, m) in acc if s == site})
            for qkey in QMAP:
                mem_arrays = [acc[(site, m)][qkey] for m in members]
                mem_arrays = [a for a in mem_arrays if any(v is not None for v in a)]
                if not mem_arrays:
                    continue
                site_obj[qkey] = _mean(mem_arrays, n)          # ensemble mean (bold line)
                site_obj[qkey + "_members"] = mem_arrays        # spaghetti
        sites_out[site] = site_obj

    return {
        "model": model,
        "run_init": _run_stamp(run_init),
        "kind": "ensemble" if is_ens else "deterministic",
        "valid": [_iso(v) for v in valids],
        "sites": sites_out,
    }


def main() -> int:
    if not POINTS_PARQUET.exists():
        print(f"{POINTS_PARQUET} does not exist -- nothing to export")
        return 1
    df = pl.read_parquet(POINTS_PARQUET)
    models_cfg = load_models()["models"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)

    index_models: dict[str, dict] = {}
    newest_run_first_valids = []  # earliest valid of each model's newest run
    for model in sorted(df["model"].unique().to_list()):
        mdf = df.filter(pl.col("model") == model)
        quantities = [
            q for q, col in QMAP.items()
            if col in mdf.columns and mdf[col].is_not_null().any()
        ]
        runs = sorted(mdf["run_init"].unique().to_list(), reverse=True)
        is_ens = mdf["member"].n_unique() > 1
        newest_first_valid = mdf.filter(pl.col("run_init") == runs[0])["valid"].min()
        if newest_first_valid is not None:
            newest_run_first_valids.append(newest_first_valid)

        model_dir = OUT_DIR / model
        model_dir.mkdir(parents=True, exist_ok=True)
        for run_init in runs:
            out_file = model_dir / f"{_run_stamp(run_init)}.json"
            # A run's JSON only changes while the run is still gaining steps
            # (inside its top-up window). Once past that it is immutable, so on
            # the per-pass regeneration clock we rewrite only recent or
            # not-yet-written runs - not the whole growing history every pass.
            age_h = (now - run_init).total_seconds() / 3600
            if out_file.exists() and age_h > _STABLE_AFTER_H:
                continue
            payload = export_run(model, run_init, mdf.filter(pl.col("run_init") == run_init))
            out_file.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

        index_models[model] = {
            "label": _model_label(model, models_cfg),
            "kind": "ensemble" if is_ens else "deterministic",
            "deterministic_pair": DET_PAIR.get(model),
            "quantities": quantities,
            "runs": [_run_stamp(r) for r in runs],
        }
        print(f"  {model:<16} {len(runs)} run(s), quantities={quantities}, "
              f"{'ensemble' if is_ens else 'deterministic'}")

    # Fixed x-axis domain so the chart does not rescale when the model changes.
    # Right edge = data_horizon (eclipse + margin, the same cap every other
    # tool uses); left edge = the earliest valid time among the models' NEWEST
    # runs (so old accumulated runs don't stretch the axis weeks to the left).
    horizon_raw = eclipse_config().get("data_horizon")
    horizon_iso = (
        _iso(datetime.fromisoformat(horizon_raw.replace("Z", "+00:00")))
        if horizon_raw else _iso(df["valid"].max())
    )
    x_min = min(newest_run_first_valids) if newest_run_first_valids else df["valid"].min()
    index = {
        "generated_at": _iso(df["fetched_at"].max()),
        "valid_range": [_iso(x_min), horizon_iso],
        "models": index_models,
    }
    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'index.json'} ({len(index_models)} model(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
