"""Loads config/production.yaml (deployment policy, NOT model metadata) with
env-var overrides, so a compose file can tune a box without editing the repo.

Absent config file == every default below, which is what makes this package
importable (and testable) on the dev desktop without pretending the desktop
is production: importing this module has no side effects and reclaim still
requires an explicit --apply at the CLI regardless of what this says.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.config import REPO_ROOT

PRODUCTION_YAML = Path(
    os.environ.get("ECLIPSE_PRODUCTION_YAML", REPO_ROOT / "config" / "production.yaml")
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    reclaim_enabled: bool = True
    min_file_age_seconds: int = 120
    min_no_data_observations: int = 2
    require_extraction_for_eclipse_steps: bool = True

    min_free_gb: float = 15.0
    fallback_bytes_per_step_mb: float = 350.0

    default_chunk_hours: int = 24
    per_model_chunk_hours: dict[str, int] = field(default_factory=dict)
    max_chunks_per_pass: int | None = None

    max_run_age_days: int | None = None

    def chunk_hours(self, model_id: str) -> int:
        return int(self.per_model_chunk_hours.get(model_id, self.default_chunk_hours))

    @property
    def min_free_bytes(self) -> int:
        return int(self.min_free_gb * 1024**3)

    @property
    def fallback_bytes_per_step(self) -> int:
        return int(self.fallback_bytes_per_step_mb * 1024**2)


def load_settings(path: Path | None = None) -> Settings:
    """Read production.yaml (if present) then apply env overrides.

    Env names mirror the YAML path in SCREAMING_SNAKE, prefixed ECLIPSE_ -
    e.g. reclaim.min_file_age_seconds -> ECLIPSE_RECLAIM_MIN_FILE_AGE_SECONDS.
    """
    path = path or PRODUCTION_YAML
    raw: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    reclaim = raw.get("reclaim") or {}
    disk = raw.get("disk") or {}
    chunking = raw.get("chunking") or {}
    frames = raw.get("frames") or {}

    max_chunks = chunking.get("max_chunks_per_pass")
    env_max_chunks = os.environ.get("ECLIPSE_MAX_CHUNKS_PER_PASS")
    if env_max_chunks not in (None, ""):
        max_chunks = int(env_max_chunks)

    max_age = frames.get("max_run_age_days")
    env_max_age = os.environ.get("ECLIPSE_FRAMES_MAX_RUN_AGE_DAYS")
    if env_max_age not in (None, ""):
        max_age = int(env_max_age)

    return Settings(
        reclaim_enabled=_env_bool(
            "ECLIPSE_RECLAIM_ENABLED", bool(reclaim.get("enabled", True))
        ),
        min_file_age_seconds=_env_int(
            "ECLIPSE_RECLAIM_MIN_FILE_AGE_SECONDS", int(reclaim.get("min_file_age_seconds", 120))
        ),
        min_no_data_observations=_env_int(
            "ECLIPSE_RECLAIM_MIN_NO_DATA_OBSERVATIONS",
            int(reclaim.get("min_no_data_observations", 2)),
        ),
        require_extraction_for_eclipse_steps=_env_bool(
            "ECLIPSE_RECLAIM_REQUIRE_EXTRACTION",
            bool(reclaim.get("require_extraction_for_eclipse_steps", True)),
        ),
        min_free_gb=_env_float("ECLIPSE_DISK_MIN_FREE_GB", float(disk.get("min_free_gb", 15))),
        fallback_bytes_per_step_mb=_env_float(
            "ECLIPSE_DISK_FALLBACK_BYTES_PER_STEP_MB",
            float(disk.get("fallback_bytes_per_step_mb", 350)),
        ),
        default_chunk_hours=_env_int(
            "ECLIPSE_DEFAULT_CHUNK_HOURS", int(chunking.get("default_chunk_hours", 24))
        ),
        per_model_chunk_hours=dict(chunking.get("per_model_chunk_hours") or {}),
        max_chunks_per_pass=max_chunks,
        max_run_age_days=max_age,
    )
