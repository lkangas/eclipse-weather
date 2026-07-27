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

All four models additionally fetch 2 m temperature (models.yaml's
`surface_temp.param`, i.e. `2t`) into its own temp_f{step}.grib2 - see
_temp_requests for why it is a separate retrieve rather than one more param on
the cloud request. It is NOT cheap for the ensembles: 33 MB/step for ecmwf_ens
and 32 MB/step for aifs_ens (50-51 members on the 0.25 deg global grid),
against 0.6 MB/step for the two deterministic models.

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
from datetime import datetime, timedelta
from pathlib import Path

from ecmwf.opendata import Client

from src.fetchers.base import FetchResult, full_range_steps, have_usable_file, raw_output_dir
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


def _temp_requests(
    model_config: dict, run_init: datetime, step: int, out_dir: Path
) -> list[RequestSpec]:
    """2 m temperature, as its own retrieve into its own temp_f{step}.grib2.

    Appended to every model's own cloud request list rather than folded into
    it: adding `2t` to, say, _aifs_requests' param list would silently change
    what the EXISTING cloud_f{step}.grib2 file contains, and this fetcher is
    the archiver's critical path - a new field must not be able to disturb a
    file something else already reads. Separate file, separate retrieve, so a
    temp failure costs only temp.

    The param comes from models.yaml's `surface_temp.param` (2t for all four
    ecmwf-opendata models) - single source of truth for field identity. A
    model without a surface_temp block simply gets no temp request.
    """
    param = (model_config.get("surface_temp") or {}).get("param")
    if not param:
        return []
    req = {
        **_base_request(model_config),
        "date": run_init.date(),
        "time": run_init.hour,
        "step": step,
        "param": param,
    }
    return [(req, out_dir / f"temp_f{step:03d}.grib2")]


_REQUEST_BUILDERS = {
    "ecmwf_hres": _hres_requests,
    "ecmwf_ens": _ens_requests,
    "aifs_single": _aifs_requests,
    "aifs_ens": _aifs_requests,
}

# Request builders every model gets on top of its own cloud builder above.
_EXTRA_REQUEST_BUILDERS = (_temp_requests,)


def _download_steps(
    *, model_name: str, model_config: dict, run_init: datetime, steps: list[int], out_dir: Path,
    steps_map: dict, client: Client,
) -> FetchResult:
    """Shared download loop: fetch every (step, request) combo `builder`
    produces for `steps` into `out_dir`, idempotently."""
    builder = _REQUEST_BUILDERS.get(model_name)
    if builder is None:
        raise ValueError(
            f"ecmwf_opendata_fetcher has no request builder for model '{model_name}' "
            f"(known: {sorted(_REQUEST_BUILDERS)})"
        )

    files_written: list[Path] = []
    errors: list[str] = []

    for step in steps:
        specs = list(builder(model_config, run_init, step, out_dir))
        for extra in _EXTRA_REQUEST_BUILDERS:
            specs.extend(extra(model_config, run_init, step, out_dir))
        for req, target in specs:
            # Not exists()/size>0 but a structural GRIB check - a retrieve that
            # died mid-transfer leaves a non-empty but truncated file, which
            # this loop then skipped on every later top-up pass, freezing the
            # run broken (real case: ecmwf_hres 2026072612 tcc_f123.grib2, and
            # two truncated aifs_ens files; see have_usable_file's note). Files
            # it judges broken are deleted, so the retrieve below recreates them.
            if have_usable_file(target):
                files_written.append(target)  # already fetched - politeness/idempotency
                continue
            try:
                client.retrieve(request=req, target=str(target))
            except Exception as e:
                errors.append(f"step {step} ({target.name}): {e}")
                continue
            # Same check on the fresh download, so a truncated retrieve is
            # reported now rather than looking complete until something tries
            # to open it hours later. min_age_s=0: this retrieve has returned,
            # so the "might still be being written" grace must not apply.
            if have_usable_file(target, min_age_s=0):
                files_written.append(target)
            else:
                errors.append(
                    f"step {step} ({target.name}): retrieve produced no usable data"
                )

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


@register("ecmwf-opendata")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch every step this run_init publishes via the ecmwf-opendata
    client, for whichever of the four ecmwf-opendata models `model_name`
    names. One GRIB2 file is written per (step, field-group) under
    raw_output_dir(model_name, run_init) - see the per-model
    _*_requests() builders above for exactly which field groups/filenames.
    """
    reachable = full_range_steps(model_config, run_init)
    # No eclipse valid-time targets here - each step's own natural valid
    # time, zero misalignment, keeps FetchResult.steps meaningful anyway.
    # Point-extraction (which valid times matter for the eclipse archive)
    # is a downstream concern of the extractor, not this fetcher - see
    # steps_for_run() in src/fetchers/base.py.
    steps_map = {
        (run_init + timedelta(hours=h)).isoformat(): (h, 0.0)
        for h in reachable
    }

    if not reachable:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps_map, status="not_yet_covering"
        )

    out_dir = raw_output_dir(model_name, run_init)
    return _download_steps(
        model_name=model_name, model_config=model_config, run_init=run_init,
        steps=reachable, out_dir=out_dir, steps_map=steps_map, client=Client(),
    )
