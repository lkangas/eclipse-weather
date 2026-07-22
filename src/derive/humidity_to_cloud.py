"""Humidity -> low/mid/high cloud-fraction derivation.

Per CLAUDE.md Hard Constraint #3 ("provenance on every row") and
config/models.yaml's `ecmwf_hres.cloud.levels` entry:

    status: derived
    method: "q on pressure levels [1000,925,850,700,500,300] -> RH
             (Murphy&Koop) -> layer max/overlap"
    task: T22

ecmwf_hres has native total cloud cover (tcc, confirmed) but NO native
low/mid/high split -- this module fills that gap from specific humidity (q)
and temperature (t) on pressure levels. It does no fetching: it is pure
computation on an already-opened xarray Dataset/DataArray (or a path to a
GRIB2/NetCDF file xarray can open), which src/fetchers/ and src/extract/ are
responsible for producing/consuming.

Pipeline
--------
1. q, p        -> vapor pressure e            (exact specific-humidity inversion)
2. t           -> saturation vapor pressure e_sat   (Murphy & Koop 2005)
3. e / e_sat   -> relative humidity RH
4. RH          -> per-level cloud fraction     (Sundqvist et al. 1989 RH-threshold)
5. per-level fractions -> low/mid/high band fractions (maximum-overlap
   reduction across the levels within each band)

Every output carries provenance="derived" (CLAUDE.md Hard Constraint #3): the
flag travels as an attr on the returned Dataset AND on each cloud_* variable
individually, so it survives being pulled apart downstream (e.g. src/extract/
building per-site point rows for data/points.parquet).

ACCEPTANCE TEST (T22 / TASKS.md)
---------------------------------
GFS carries BOTH native L/M/H (LCDC/MCDC/HCDC) and the pressure-level q/t this
module needs as input. The real acceptance test is running this module on a
real GFS sample and diffing the derived L/M/H against GFS's own native L/M/H
at the same point/time -- that comparison is the calibration gate before this
module can be trusted on ecmwf_hres, which has no native L/M/H to check
against. This module ships the derivation only; the calibration run itself
(with actual comparison numbers) is a one-off test script, not part of the
importable module -- see the implementing agent's test evidence.

SIMPLIFICATIONS (read before trusting derived values)
------------------------------------------------------
- Saturation vapor pressure: Murphy & Koop (2005) formulas over both water
  (their eq. 10) and ice (their eq. 7) are implemented in full, not
  approximated. *Which one applies* at a given level is a simplification:
  water is used for t >= 273.15 K, ice for t < 273.15 K -- a hard switch at
  freezing, not a blended mixed-phase treatment (real mixed-phase cloud
  regimes blend RH-over-ice/RH-over-water across roughly -20..0 C). Adequate
  for cloud-fraction diagnosis at 6 fixed pressure levels; revisit if this
  module is ever pushed to finer vertical resolution near the freezing level.
- Cloud-fraction-from-RH: Sundqvist et al. (1989)'s classic RH-threshold
  form, C = 1 - sqrt(1 - (RH-RHc)/(1-RHc)), with a FIXED critical RH per
  band (not a sigma/pressure-dependent profile the way operational IFS uses
  internally). RHc = 0.80 / 0.75 / 0.55 for low/mid/high (DEFAULT_RH_CRIT)
  started from literature-typical constants (Sundqvist 1989; a similar range
  is cited in Walcek 1994) and were then NUDGED using the T22 GFS calibration
  run below (2026-07-22 12Z, f024, full Iberia bbox, ~2000 grid points): a
  small grid search over RHc per band picked the value minimizing RMSE
  against GFS's own native LCDC/MCDC/HCDC at that run. This is a
  single-sample tune, not a robust climatological calibration -- re-run the
  grid search as more real runs get archived (T22/T23) rather than trusting
  these three constants indefinitely.
- Band assignment and multi-level combination: the 6 fixed pressure levels
  are grouped low={1000,925,850}, mid={700,500}, high={300} hPa, mirroring
  the sfc-800 / 800-400 / 400-0 hPa split config/models.yaml already uses for
  icon_global's native CLCL/CLCM/CLCH bands (kept consistent with that
  existing project convention rather than inventing a second one here).
  Levels within a band are combined via MAXIMUM overlap (models.yaml's own
  "layer max/overlap" wording for ecmwf_hres) -- i.e. band fraction = max
  over the band's levels of that level's cloud fraction. This is the
  simplest standard overlap assumption; relative to random/max-random
  overlap schemes it is a known upper bound when a band's levels are only
  partially, non-identically cloud-filled.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rd/Rv (dry-air gas constant / water-vapor gas constant) used to invert
#: specific humidity into vapor pressure.
EPSILON = 0.621981

#: Pressure levels this module is designed around (hPa) -- matches
#: config/models.yaml's ecmwf_hres.cloud.levels.method spec exactly.
DEFAULT_LEVELS_HPA = (1000, 925, 850, 700, 500, 300)

#: Band <- level grouping. Mirrors models.yaml's icon_global CLCL/CLCM/CLCH
#: split (sfc-800 / 800-400 / 400-0 hPa) -- see module docstring.
DEFAULT_BANDS: dict[str, tuple[float, ...]] = {
    "low": (1000, 925, 850),
    "mid": (700, 500),
    "high": (300,),
}

#: Critical relative humidities per band, as fractions (0-1) -- started from
#: Sundqvist (1989)-typical literature values, then nudged by a small grid
#: search against a real GFS calibration run. See module docstring
#: SIMPLIFICATIONS for how these were picked and their (single-sample) caveat.
DEFAULT_RH_CRIT: dict[str, float] = {"low": 0.80, "mid": 0.75, "high": 0.55}

#: The provenance value CLAUDE.md Hard Constraint #3 requires on every value
#: this module produces.
PROVENANCE = "derived"

_FREEZING_K = 273.15


def _where(cond, x, y):
    """xr.where if any operand is an xarray object, else np.where -- lets
    every function below work on both plain numpy arrays/scalars (for unit
    tests) and xarray DataArrays (for real use), preserving dims/coords."""
    xr_types = (xr.DataArray, xr.Dataset)
    if isinstance(cond, xr_types) or isinstance(x, xr_types) or isinstance(y, xr_types):
        return xr.where(cond, x, y)
    return np.where(cond, x, y)


# ---------------------------------------------------------------------------
# Step 1: saturation vapor pressure -- Murphy & Koop (2005)
# ---------------------------------------------------------------------------


def saturation_vapor_pressure_water(t_kelvin):
    """Murphy & Koop (2005) eq. 10: saturation vapor pressure over liquid
    (supercooled or not) water. Valid 123 K < T < 332 K. Returns Pa."""
    t = t_kelvin
    log_p = (
        54.842763
        - 6763.22 / t
        - 4.210 * np.log(t)
        + 0.000367 * t
        + np.tanh(0.0415 * (t - 218.8))
        * (53.878 - 1331.22 / t - 9.44523 * np.log(t) + 0.014025 * t)
    )
    return np.exp(log_p)


def saturation_vapor_pressure_ice(t_kelvin):
    """Murphy & Koop (2005) eq. 7: saturation vapor pressure over ice.
    Valid T > 110 K. Returns Pa."""
    t = t_kelvin
    log_p = 9.550426 - 5723.265 / t + 3.53068 * np.log(t) - 0.00728332 * t
    return np.exp(log_p)


def saturation_vapor_pressure(t_kelvin, ice_below_freezing: bool = True):
    """Saturation vapor pressure (Pa) per Murphy & Koop (2005). Uses the
    ice-phase formula below 273.15 K and the water-phase formula at/above it
    -- a hard switch, see module docstring SIMPLIFICATIONS. Pass
    ice_below_freezing=False to force the water-phase formula everywhere."""
    e_water = saturation_vapor_pressure_water(t_kelvin)
    if not ice_below_freezing:
        return e_water
    e_ice = saturation_vapor_pressure_ice(t_kelvin)
    return _where(t_kelvin < _FREEZING_K, e_ice, e_water)


# ---------------------------------------------------------------------------
# Steps 2-3: q, t, p -> RH
# ---------------------------------------------------------------------------


def vapor_pressure_from_specific_humidity(q_kg_per_kg, pressure_pa):
    """Exact inversion of specific humidity q = epsilon*e / (p - (1-epsilon)*e)
    for the actual (non-saturation) vapor pressure e, given q and total
    pressure p. Returns e in the same pressure units as `pressure_pa`."""
    return q_kg_per_kg * pressure_pa / (EPSILON + (1.0 - EPSILON) * q_kg_per_kg)


def relative_humidity(q_kg_per_kg, t_kelvin, pressure_pa, ice_below_freezing: bool = True):
    """RH as a fraction. Not clipped here (clipping happens in the
    cloud-fraction step) so intermediate RH stays inspectable, including
    values >1 (supersaturation, physically real e.g. w.r.t. ice in cirrus)."""
    e = vapor_pressure_from_specific_humidity(q_kg_per_kg, pressure_pa)
    e_sat = saturation_vapor_pressure(t_kelvin, ice_below_freezing=ice_below_freezing)
    return e / e_sat


# ---------------------------------------------------------------------------
# Step 4: RH -> per-level cloud fraction -- Sundqvist et al. (1989)
# ---------------------------------------------------------------------------


def cloud_fraction_from_rh(rh, rh_crit):
    """Sundqvist et al. (1989) RH-threshold cloud fraction:

        C = 1 - sqrt(1 - (RH - RHc) / (1 - RHc))   for RH >= RHc
        C = 0                                       for RH <  RHc
        C saturates at 1 for RH >= 1 (and beyond, for supersaturation)

    `rh_crit` may be a scalar or an array/DataArray broadcastable against
    `rh` (e.g. one RHc per pressure level). Returns a fraction (0-1)."""
    ratio = (rh - rh_crit) / (1.0 - rh_crit)
    ratio = np.clip(ratio, 0.0, 1.0)
    return 1.0 - np.sqrt(1.0 - ratio)


# ---------------------------------------------------------------------------
# Step 5: per-level fractions -> low/mid/high bands
# ---------------------------------------------------------------------------


def levels_to_bands(
    cloud_fraction_by_level: xr.DataArray,
    level_dim: str,
    bands: dict[str, tuple[float, ...]],
) -> dict[str, xr.DataArray]:
    """Reduce a per-level cloud-fraction DataArray (dim `level_dim`, coordinate
    values in hPa) down to one DataArray per band, via a maximum-overlap max()
    across each band's levels (see module docstring)."""
    available = set(np.asarray(cloud_fraction_by_level[level_dim].values).tolist())
    result = {}
    for band_name, band_levels in bands.items():
        levels_present = [lv for lv in band_levels if lv in available]
        if not levels_present:
            raise ValueError(
                f"None of band '{band_name}'s levels {band_levels} were found in "
                f"the input's '{level_dim}' coordinate {sorted(available)}"
            )
        result[band_name] = cloud_fraction_by_level.sel({level_dim: levels_present}).max(
            dim=level_dim
        )
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_cloud_fractions(
    ds: xr.Dataset | str | Path,
    *,
    q_var: str = "q",
    t_var: str = "t",
    level_dim: str = "isobaricInhPa",
    level_hpa_to_pa: float = 100.0,
    bands: dict[str, tuple[float, ...]] | None = None,
    rh_crit: dict[str, float] | None = None,
    ice_below_freezing: bool = True,
) -> xr.Dataset:
    """Full q,t -> low/mid/high cloud-fraction pipeline (see module docstring).

    Parameters
    ----------
    ds
        An already-opened xr.Dataset containing `q_var`/`t_var` on a
        pressure-level dimension, OR a path to a file xarray can open
        directly (opened here with engine="cfgrib", filtered to
        typeOfLevel="isobaricInhPa" -- appropriate for a GRIB2 file that
        contains ONLY the pressure-level q/t messages, e.g. a dedicated
        per-model humidity-fetch slice; a file mixing multiple GRIB
        hypercubes should be opened by the caller instead and passed in as
        a Dataset).
    q_var, t_var
        Variable names for specific humidity (kg/kg) and temperature (K).
        Matches cfgrib's default shortName-based naming ("q", "t") for both
        GFS's and ECMWF's pressure-level GRIB output.
    level_dim
        The pressure-level coordinate name. cfgrib names this
        "isobaricInhPa" (values in hPa) for both models above.
    level_hpa_to_pa
        Multiplier turning `level_dim`'s values into Pascals for the vapor
        -pressure formula (cfgrib's isobaricInhPa is already hPa, so 100.0).
    bands, rh_crit
        Override DEFAULT_BANDS / DEFAULT_RH_CRIT if needed.
    ice_below_freezing
        See saturation_vapor_pressure().

    Returns
    -------
    xr.Dataset with cloud_low, cloud_mid, cloud_high data variables (percent,
    0-100), each carrying attrs["provenance"] = "derived" (CLAUDE.md Hard
    Constraint #3) and attrs["method"]; the same provenance attr is also set
    on the Dataset itself.
    """
    if bands is None:
        bands = DEFAULT_BANDS
    if rh_crit is None:
        rh_crit = DEFAULT_RH_CRIT

    if isinstance(ds, (str, Path)):
        ds = xr.open_dataset(
            ds, engine="cfgrib", filter_by_keys={"typeOfLevel": "isobaricInhPa"}
        )

    if q_var not in ds:
        raise KeyError(f"'{q_var}' not found in dataset variables {list(ds.data_vars)}")
    if t_var not in ds:
        raise KeyError(f"'{t_var}' not found in dataset variables {list(ds.data_vars)}")
    if level_dim not in ds[q_var].dims:
        raise KeyError(f"'{level_dim}' is not a dimension of '{q_var}' (dims: {ds[q_var].dims})")
    if level_dim not in ds[t_var].dims:
        raise KeyError(f"'{level_dim}' is not a dimension of '{t_var}' (dims: {ds[t_var].dims})")

    level_values = np.asarray(ds[level_dim].values).tolist()
    band_of_level: dict[float, str] = {
        lv: band for band, levels in bands.items() for lv in levels
    }
    unmapped = [lv for lv in level_values if lv not in band_of_level]
    if unmapped:
        raise ValueError(
            f"Input has levels {unmapped} not assigned to any band in {bands} -- "
            f"extend `bands` or drop these levels before calling derive_cloud_fractions()."
        )

    q = ds[q_var]
    t = ds[t_var]
    pressure_pa = ds[level_dim] * level_hpa_to_pa

    rh = relative_humidity(q, t, pressure_pa, ice_below_freezing=ice_below_freezing)

    # One RHc per level (broadcasts against rh's level_dim via xarray coord
    # alignment), so cloud_fraction_from_rh runs once, vectorized.
    rhc_by_level = xr.DataArray(
        [rh_crit[band_of_level[lv]] for lv in level_values],
        dims=[level_dim],
        coords={level_dim: ds[level_dim].values},
    )
    frac_per_level = cloud_fraction_from_rh(rh, rhc_by_level)

    band_fractions = levels_to_bands(frac_per_level, level_dim, bands)

    out = xr.Dataset(
        {
            "cloud_low": band_fractions["low"] * 100.0,
            "cloud_mid": band_fractions["mid"] * 100.0,
            "cloud_high": band_fractions["high"] * 100.0,
        }
    )
    method = (
        "q+t on pressure levels -> RH (Murphy & Koop 2005) -> cloud fraction "
        "(Sundqvist et al. 1989 RH-threshold) -> band max-overlap"
    )
    for var in out.data_vars:
        out[var].attrs["provenance"] = PROVENANCE
        out[var].attrs["units"] = "%"
        out[var].attrs["method"] = method
    out.attrs["provenance"] = PROVENANCE
    out.attrs["method"] = method
    return out
