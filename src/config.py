from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
SITES_YAML = REPO_ROOT / "config" / "sites.yaml"
DATA_RAW = REPO_ROOT / "data" / "raw"
POINTS_PARQUET = REPO_ROOT / "data" / "points.parquet"


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
