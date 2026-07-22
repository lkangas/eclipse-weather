"""Fetcher for ECMWF's open-data feed (data.ecmwf.int + AWS/Azure/GCS mirrors),
covering every models.yaml entry whose `fetch:` value is "ecmwf-opendata":
ecmwf_hres, ecmwf_ens, aifs_single, aifs_ens.

Per CLAUDE.md hard constraint #2 (models.yaml is the single source of truth),
this module reads cycles/steps/request shape/cloud params from model_config
rather than hardcoding a second copy of them:

  - total-cloud param + request stream/type: model_config["cloud"]["total"]["param"]
    and model_config["source"]["request"] (ecmwf_hres, ecmwf_ens).
  - HRES's derived-L/M/H pressure levels: parsed out of the bracketed level
    list embedded in model_config["cloud"]["levels"]["method"]'s prose, so a
    future edit to that string (e.g. T22 revising the level set) doesn't
    require a matching code change here.
  - AIFS's native L/M/H params: model_config["cloud"]["levels"]["params"]
    (aifs_single, aifs_ens).

Two extra fields are fetched beyond what models.yaml's structured `cloud:`
section names for their model - flagged here, not silently assumed correct:

  - HRES pressure-level "z" (geopotential), alongside "q"/"t": Iberia's
    orography (Meseta, Pyrenees) means some of these levels (1000/925 hPa)
    are below-ground over part of the bbox; z lets T22's derive step detect/
    mask that. Live-tested 2026-07-22: z values at 1000 hPa over the full
    global grid do go negative (extrapolated-below-ground geopotential),
    consistent with this being a real, not hypothetical, need.
  - AIFS "tcc": models.aifs_single/aifs_ens's cloud section only lists
    levels.params ([lcc, mcc, hcc]), no `total` entry (unlike ens/hres).
    Fetched anyway per this fetcher's build spec - it is cheap, native, and
    useful as an independent cross-check outside the derive path.

NOTE (fixed after initial build+review): the first version of this fetcher
only requested q+z for HRES, matching models.yaml's cloud.levels.method
string at the time, which named only "q". But src/derive/humidity_to_cloud.py
(T22)'s Murphy & Koop saturation-vapor-pressure calculation hard-requires
temperature too - it raises KeyError without a "t" variable. Added "t" to
the pressure-level request and updated models.yaml's method string to name
it explicitly, so config stays the single source of truth for what this
fetcher actually needs to pull.

Request/response mechanics (ecmwf.opendata.Client):
  - client.retrieve(request=..., target=<path>) accepts a `param` list and,
    for pressure levels, `levtype`/`levelist` lists - one retrieve call can
    span multiple params x multiple levels x multiple ensemble members and
    still writes a single merged GRIB2 file to `target` (live-verified: a
    6-level q+z HRES request produced one 12-message file; an aifs-ens
    4-param request produced one 204-message file covering all 51 members).
  - Ensemble requests (`type: [cf, pf]`) degrade gracefully when a member
    type is genuinely absent for that run: the client logs a warning and
    still writes whatever matched, rather than raising. Confirmed live
    2026-07-22: today's classic ecmwf_ens (`enfo`) runs carry 50 `pf`
    members and ZERO `cf` messages, at every step/cycle checked - this
    fetcher still succeeds and archives the 50 pf members it does find.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from ecmwf.opendata import Client

from src.fetchers.base import FetchResult, raw_output_dir, steps_for_run
from src.fetchers.registry import register

log = logging.getLogger(__name__)

# "q" and "t" come from models.yaml's cloud.levels.method prose; "z" is an
# extra this fetcher adds for terrain-masking - see module docstring.
_HRES_PL_EXTRA_PARAM = "z"
# Not present anywhere in models.yaml's aifs_single/aifs_ens cloud section
# (only cloud.levels.params) - see module docstring.
_AIFS_EXTRA_TOTAL_PARAM = "tcc"

RequestSpec = tuple[dict, Path]


def _parse_pressure_levels(method: str) -> list[int]:
    """Extract the bracketed pressure-level list out of a cloud.levels.method
    string, e.g. "...pressure levels [1000,925,850,700,500,300] -> RH...".
    Reading it out of the prose (rather than hardcoding a parallel list here)
    keeps models.yaml the single place that level set is defined.
    """
    match = re.search(r"\[([\d,\s]+)\]", method)
    if not match:
        raise ValueError(
            f"could not find a bracketed pressure-level list in method string: {method!r}"
        )
    return [int(x) for x in match.group(1).split(",")]


def _base_request(model_config: dict) -> dict:
    return dict(model_config["source"]["request"])


def _hres_requests(
    model_config: dict, run_init: datetime, step: int, out_dir: Path
) -> list[RequestSpec]:
    common = {
        **_base_request(model_config),
        "date": run_init.date(),
        "time": run_init.hour,
        "step": step,
    }

    total_param = model_config["cloud"]["total"]["param"]
    tcc_req = {**common, "param": total_param}
    tcc_target = out_dir / f"tcc_f{step:03d}.grib2"

    levels = _parse_pressure_levels(model_config["cloud"]["levels"]["method"])
    # q + t are the physical inputs Murphy & Koop's method (src/derive/humidity_to_cloud.py,
    # T22) requires; z is this fetcher's own addition for terrain-masking (see docstring).
    pl_req = {
        **common,
        "levtype": "pl",
        "levelist": levels,
        "param": ["q", "t", _HRES_PL_EXTRA_PARAM],
    }
    pl_target = out_dir / f"pl_f{step:03d}.grib2"

    return [(tcc_req, tcc_target), (pl_req, pl_target)]


def _ens_requests(
    model_config: dict, run_init: datetime, step: int, out_dir: Path
) -> list[RequestSpec]:
    total_param = model_config["cloud"]["total"]["param"]
    req = {
        **_base_request(model_config),
        "date": run_init.date(),
        "time": run_init.hour,
        "step": step,
        "param": total_param,
    }
    target = out_dir / f"tcc_f{step:03d}.grib2"
    return [(req, target)]


def _aifs_requests(
    model_config: dict, run_init: datetime, step: int, out_dir: Path
) -> list[RequestSpec]:
    level_params = list(model_config["cloud"]["levels"]["params"])
    params = [*level_params, _AIFS_EXTRA_TOTAL_PARAM]
    req = {
        **_base_request(model_config),
        "date": run_init.date(),
        "time": run_init.hour,
        "step": step,
        "param": params,
    }
    target = out_dir / f"cloud_f{step:03d}.grib2"
    return [(req, target)]


_REQUEST_BUILDERS = {
    "ecmwf_hres": _hres_requests,
    "ecmwf_ens": _ens_requests,
    "aifs_single": _aifs_requests,
    "aifs_ens": _aifs_requests,
}


@register("ecmwf-opendata")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch every eclipse-archive-relevant step of one run_init via the
    ecmwf-opendata client, for whichever of the four ecmwf-opendata models
    `model_name` names. One GRIB2 file is written per (step, field-group)
    under raw_output_dir(model_name, run_init) - see the per-model
    _*_requests() builders above for exactly which field groups/filenames.
    """
    steps_map = steps_for_run(model_config, run_init)
    covering_steps = {vt: s[0] for vt, s in steps_map.items() if s is not None}

    if not covering_steps:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps_map, status="not_yet_covering"
        )

    builder = _REQUEST_BUILDERS.get(model_name)
    if builder is None:
        raise ValueError(
            f"ecmwf_opendata_fetcher has no request builder for model '{model_name}' "
            f"(known: {sorted(_REQUEST_BUILDERS)})"
        )

    out_dir = raw_output_dir(model_name, run_init)
    client = Client()

    files_written: list[Path] = []
    errors: list[str] = []

    for step in sorted(set(covering_steps.values())):
        for req, target in builder(model_config, run_init, step, out_dir):
            if target.exists() and target.stat().st_size > 0:
                files_written.append(target)  # already fetched - politeness/idempotency
                continue
            try:
                client.retrieve(request=req, target=str(target))
            except Exception as e:
                errors.append(f"step {step} ({target.name}): {e}")
                continue
            if target.exists() and target.stat().st_size > 0:
                files_written.append(target)
            else:
                errors.append(f"step {step} ({target.name}): retrieve produced no data")

    status = "ok" if not errors else "error"
    error_msg = "; ".join(errors) if errors else None
    return FetchResult(
        model=model_name,
        run_init=run_init,
        steps=steps_map,
        files_written=files_written,
        status=status,
        error=error_msg,
    )
