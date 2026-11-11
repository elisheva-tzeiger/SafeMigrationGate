"""Eval 2 — token cost, broken down by graph node.

Prices come from OpenRouter's catalogue at run time rather than from a constant,
so the numbers do not rot. Free models report an actual cost of 0 alongside the
theoretical cost of their paid equivalent.
"""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.graph import build_graph
from agent.state import initial_state
from evals.model_registry import (
    equivalent_paid_model,
    fetch_catalogue,
    load_config,
    price_per_token,
)

FIXTURES = Path("evals/fixtures")
RESULTS = Path("evals/results")

CSV_COLUMNS = ["task", "node", "model", "prompt_tokens", "completion_tokens",
               "actual_cost_usd", "theoretical_cost_usd"]

TASKS = ["01_delete_no_where.sql", "03_tautological_where.sql",
         "05_safe_migration.sql", "07_mixed_batch.sql"]


class NodeUsageCollector(BaseCallbackHandler):
    """Attributes token usage to the run_name each node passes to with_config.

    Implementation note: in langchain-core, ``on_llm_end`` does **not** receive
    ``name`` or ``metadata`` in its ``**kwargs`` — those keys are absent from the
    signature.  The ``name`` kwarg *is* present in ``on_chat_model_start``, so we
    capture the ``run_id → name`` mapping there and look it up in ``on_llm_end``
    using the matching ``run_id``.  (Measured with a FakeListChatModel probe on
    langchain-core 1.6.0; see probe_attribution.py in the task-17 report.)
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
        # ``name`` is NOT in on_llm_end kwargs; look it up via run_id captured
        # in on_chat_model_start above.
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


def _run(model: str, fixture: Path, collector: NodeUsageCollector) -> None:
    os.environ["SMG_MODEL"] = model
    graph = build_graph(checkpointer=MemorySaver())
    # Amendment 1: callbacks must be at the TOP level of config, not nested
    # inside configurable — nested callbacks receive zero events silently.
    config = {"configurable": {"thread_id": f"cost:{model}:{fixture.name}"},
              "callbacks": [collector]}
    list(graph.stream(initial_state(str(fixture), "", "", None),
                      config=config, stream_mode="updates"))
    while graph.get_state(config).interrupts:
        list(graph.stream(Command(resume=True), config=config,
                          stream_mode="updates"))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    config = load_config()
    catalogue = fetch_catalogue()
    models = [m for m in config["candidates"] if m in catalogue]
    if not models:
        print("[ERROR] none of the configured models are available.")
        return 1

    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / "cost_eval.csv"
    grand_actual = grand_theoretical = 0.0

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for model in models:
            prices = price_per_token(model, catalogue)
            twin = (equivalent_paid_model(model, catalogue)
                    or (config.get("theoretical_reference")
                        if model.endswith(":free") else None))
            theoretical_prices = (price_per_token(twin, catalogue) if twin
                                  else prices)

            for name in TASKS:
                collector = NodeUsageCollector()
                try:
                    _run(model, FIXTURES / name, collector)
                    if not collector.totals()["prompt_tokens"]:
                        raise RuntimeError("model returned no usage - "
                                           "it never answered, so no cost to report")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {model} / {name}: {exc}")
                    continue

                task_actual = task_theoretical = 0.0
                for node, usage in sorted(collector.usage.items()):
                    actual = cost_for(usage["prompt_tokens"],
                                      usage["completion_tokens"], prices)
                    theoretical = cost_for(usage["prompt_tokens"],
                                           usage["completion_tokens"],
                                           theoretical_prices)
                    task_actual += actual
                    task_theoretical += theoretical
                    writer.writerow({
                        "task": name, "node": node, "model": model,
                        "prompt_tokens": usage["prompt_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "actual_cost_usd": f"{actual:.6f}",
                        "theoretical_cost_usd": f"{theoretical:.6f}"})

                totals = collector.totals()
                writer.writerow({
                    "task": name, "node": "TASK TOTAL", "model": model,
                    "prompt_tokens": totals["prompt_tokens"],
                    "completion_tokens": totals["completion_tokens"],
                    "actual_cost_usd": f"{task_actual:.6f}",
                    "theoretical_cost_usd": f"{task_theoretical:.6f}"})
                grand_actual += task_actual
                grand_theoretical += task_theoretical
                print(f"  {model} / {name}: ${task_actual:.6f} actual")

        writer.writerow({"task": "ALL", "node": "GRAND TOTAL", "model": "",
                         "prompt_tokens": "", "completion_tokens": "",
                         "actual_cost_usd": f"{grand_actual:.6f}",
                         "theoretical_cost_usd": f"{grand_theoretical:.6f}"})

    print(f"wrote {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
