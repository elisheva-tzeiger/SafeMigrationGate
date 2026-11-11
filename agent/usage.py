"""Token usage and cost, per graph node.

Lives in the agent package (not under evals/) because the CLI shows it at the end
of every run, and the packaged executable does not include evals/.
"""

from __future__ import annotations

import json
import urllib.request
from collections import defaultdict

from langchain_core.callbacks import BaseCallbackHandler

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"


class NodeUsageCollector(BaseCallbackHandler):
    """Attributes token usage to the run_name each node passes to with_config.

    In langchain-core, ``on_llm_end`` does not receive ``name`` or ``metadata`` in
    its kwargs. The ``name`` kwarg *is* present in ``on_chat_model_start``, so the
    ``run_id -> name`` mapping is captured there and looked up in ``on_llm_end``.
    """

    def __init__(self) -> None:
        self.usage: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt_tokens": 0, "completion_tokens": 0})
        self._run_name_by_id: dict[str, str] = {}

    def record(self, node: str | None, prompt_tokens: int,
               completion_tokens: int) -> None:
        bucket = self.usage[node or "(unnamed)"]
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens

    def on_chat_model_start(  # noqa: D102
            self, serialized: dict, messages: list, **kwargs: object) -> None:
        run_id = kwargs.get("run_id")
        name = kwargs.get("name")
        if run_id and name:
            self._run_name_by_id[str(run_id)] = str(name)

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001, D102
        name = self._run_name_by_id.get(str(kwargs.get("run_id", "")))
        usage = (response.llm_output or {}).get("token_usage") or {}
        if not usage:
            for generations in response.generations:
                for generation in generations:
                    message = getattr(generation, "message", None)
                    meta = getattr(message, "usage_metadata", None) or {}
                    if meta:
                        usage = {"prompt_tokens": meta.get("input_tokens", 0),
                                 "completion_tokens": meta.get("output_tokens", 0)}
        self.record(name, int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)))

    def totals(self) -> dict[str, int]:
        return {
            "prompt_tokens": sum(v["prompt_tokens"] for v in self.usage.values()),
            "completion_tokens": sum(v["completion_tokens"] for v in self.usage.values()),
        }


def cost_for(prompt_tokens: int, completion_tokens: int,
             prices: tuple[float, float]) -> float:
    return prompt_tokens * prices[0] + completion_tokens * prices[1]


def fetch_prices(model: str) -> tuple[float, float] | None:
    """(prompt, completion) USD per token for *model*, read from OpenRouter at run
    time. None when the catalogue is unreachable or the model is not in it, so the
    caller can say "cost unavailable" rather than print a made-up number."""
    try:
        from agent.llm import install_tls_trust

        install_tls_trust()
        with urllib.request.urlopen(CATALOGUE_URL, timeout=15) as response:
            catalogue = json.loads(response.read())["data"]
        for entry in catalogue:
            if entry["id"] == model:
                pricing = entry.get("pricing") or {}
                return (float(pricing.get("prompt") or 0.0),
                        float(pricing.get("completion") or 0.0))
    except Exception:  # noqa: BLE001
        return None
    return None
