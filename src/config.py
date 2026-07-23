import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
SITES_YAML = REPO_ROOT / "config" / "sites.yaml"

# Defaults to <repo>/data (also what's baked into the Docker image as /app/data,
# via docker-compose.yml's own ./data:/app/data bind mount - production has no
# reason to look anywhere else). Override for local dev where the repo itself
# lives somewhere sync-managed (OneDrive) that shouldn't be accumulating
# large, constantly-changing raw archive data - see ECLIPSE_DATA_ROOT usage
# in dev-machine docker/script invocations.
DATA_ROOT = Path(os.environ.get("ECLIPSE_DATA_ROOT", REPO_ROOT / "data"))
DATA_RAW = DATA_ROOT / "raw"
POINTS_PARQUET = DATA_ROOT / "points.parquet"

# Historical note (2026-07-23): there used to be a separate DATA_RAW_LATEST
# tree here for Tool 1's full-forecast-range fetches, kept apart from this
# eclipse-cropped DATA_RAW. Retired the same day - the eclipse archiver now
# always fetches the full range too (see TASKS.md's archiver-consolidation
# note), so there's no longer a narrower fetch to keep separate from. Both
# trees were merged back into this one DATA_RAW.


def load_models() -> dict:
    with open(MODELS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sites() -> dict:
    with open(SITES_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_model(name: str) -> dict:
    models = load_models()["models"]
    if name not in models:
        raise KeyError(f"No model '{name}' in {MODELS_YAML}")
    return models[name]


def eclipse_config() -> dict:
    return load_models()["eclipse"]
