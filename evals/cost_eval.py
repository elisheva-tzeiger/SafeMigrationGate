"""Eval 2 — token cost, broken down by graph node.

Prices come from OpenRouter's catalogue at run time rather than from a constant,
so the numbers do not rot. Free models report an actual cost of 0 alongside the
theoretical cost of their paid equivalent.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.graph import build_graph
from agent.usage import NodeUsageCollector, cost_for  # noqa: F401  (re-exported)
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
