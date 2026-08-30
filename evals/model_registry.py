"""Model availability and pricing, read from OpenRouter at run time.

Free-model availability on OpenRouter changes often, and prices change too.
Hard-coding either is how an eval quietly starts reporting nonsense.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml

from agent.llm import install_tls_trust

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"


def load_config(path: str | Path = "evals/models.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def fetch_catalogue() -> dict[str, dict]:
    """Every model OpenRouter currently serves, keyed by id."""
    install_tls_trust()
    with urllib.request.urlopen(CATALOGUE_URL, timeout=60) as response:
        payload = json.loads(response.read())
    return {entry["id"]: entry for entry in payload["data"]}


def verify_models(candidates: list[str],
                  catalogue: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Split the configured models into those still served and those that are not.
    A missing model is reported, never skipped silently."""
    available = [m for m in candidates if m in catalogue]
    missing = [m for m in candidates if m not in catalogue]
    return available, missing


def price_per_token(model: str, catalogue: dict[str, dict]) -> tuple[float, float]:
    """(prompt, completion) USD per token. (0.0, 0.0) for free models."""
    pricing = (catalogue.get(model) or {}).get("pricing") or {}
    return float(pricing.get("prompt") or 0.0), float(pricing.get("completion") or 0.0)


def equivalent_paid_model(model: str, catalogue: dict[str, dict]) -> str | None:
    """The paid twin of a `:free` id, used for the theoretical cost column."""
    if not model.endswith(":free"):
        return None
    base = model[: -len(":free")]
    return base if base in catalogue else None
