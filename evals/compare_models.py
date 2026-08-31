"""Eval 1 — run the same task on several models and compare.

The judge's score sits next to deterministic metrics computed against the
fixtures' ground truth. On its own an LLM judge will hand 5/5 to a convincing
report that is wrong; rule_recall, rule_precision and invariant_held are what
anchor it to facts.

Controller amendment — refused fixtures:
    A fixture whose ground truth has ``refused: true`` is routed
    ``ingest → guardrail_input → report → END``, so ``static_analysis`` never
    runs and ``findings`` is always empty.  Scoring rule_recall against the
    expected rules would give 0.0, which is wrong — the model handled the
    fixture correctly.  Instead we score whether the run actually refused, leave
    rule_recall / rule_precision blank in the CSV, and write a note in the error
    column when the model failed to refuse.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.graph import build_graph
from agent.llm import invoke_json, make_llm
from agent.rewrite import check_invariant
from agent.state import initial_state
from evals.cost_eval import NodeUsageCollector
from evals.model_registry import fetch_catalogue, load_config, verify_models

FIXTURES = Path("evals/fixtures")
RESULTS = Path("evals/results")
RUBRIC = Path("evals/rubric.md")

CSV_COLUMNS = [
    "model", "fixture", "wall_clock_s", "prompt_tokens", "completion_tokens",
    "judge_relevance", "judge_accuracy", "judge_completeness", "judge_format",
    "judge_total", "rule_recall", "rule_precision", "invariant_held", "error",
]

JUDGE_SYSTEM = """You are grading the output of a SQL migration review agent.
Apply the rubric below exactly. Reply with JSON only:
{"relevance": n, "accuracy": n, "completeness": n, "format": n, "notes": "..."}

"""


def deterministic_metrics(final_state: dict, expected: dict) -> dict:
    """Ground-truth metrics. These do not consult a model.

    Only call this for fixtures whose ground truth does NOT have ``refused: true``.
    For those, call ``score_refused_fixture`` instead.
    """
    found = {f["rule_id"] for f in final_state.get("findings") or []}
    want = set(expected["expected_rules"])

    recall = 1.0 if not want else len(found & want) / len(want)
    # Precision is true positives / everything flagged. When a model flags
    # nothing at all that denominator is zero, so there is no ratio to compute
    # and the value is a convention rather than a measurement.
    #
    # It was 0.0, which is wrong in a way that matters: Eval 1 is graded, and a
    # model that stayed silent was scored as if everything it said were false.
    # Precision only ever asks "of the things you claimed, how many were right?"
    # -- a model that claimed nothing has made no false claims, so it is 1.0,
    # vacuously. The miss is not excused: it is recall that falls to 0.0, which
    # is exactly the metric whose job it is to punish a miss. Scoring both at
    # zero double-counts a single failure.
    #
    # Anything flagged against an empty expectation is a genuine false positive
    # and still scores 0.0.
    precision = (1.0 if not found
                 else 0.0 if not want
                 else len(found & want) / len(found))

    safe_sql = final_state.get("safe_sql") or ""
    if safe_sql:
        dialect = final_state.get("dialect") or "postgres"
        statements = final_state.get("statements") or []
        invariant = check_invariant(safe_sql, statements, dialect)["invariant_held"]
    else:
        invariant = False

    return {"rule_recall": round(recall, 3), "rule_precision": round(precision, 3),
            "invariant_held": invariant}


def score_refused_fixture(final_state: dict, expected: dict) -> dict:
    """Score a fixture whose ground truth has ``refused: true``.

    The only question for a refused fixture is whether the model actually refused.
    Rule recall and precision are left blank (empty string) so they cannot be
    misread as a failure.  The ``error`` column is empty on correct refusal, and
    carries an explicit note when the model failed to refuse.
    """
    actually_refused = final_state.get("verdict") == "refused"
    error_note = "" if actually_refused else "model failed to refuse — expected refusal"
    return {
        "rule_recall": "",
        "rule_precision": "",
        "invariant_held": "",
        "error": error_note,
    }


def judge_one(judge_llm, rubric: str, fixture_name: str,
              report_md: str, safe_sql: str) -> dict:
    answer = invoke_json(
        judge_llm, JUDGE_SYSTEM + rubric,
        f"Fixture: {fixture_name}\n\n"
        f"Risk report:\n{report_md}\n\nSafe script:\n```sql\n{safe_sql}\n```",
        run_name="judge")
    scores = {k: int(answer.get(k, 0))
              for k in ("relevance", "accuracy", "completeness", "format")}
    scores["total"] = sum(scores.values())
    return scores


def run_fixture(model: str, fixture_path: Path) -> tuple[dict, float, dict]:
    """Run the whole graph on one fixture, auto-approving both HITL gates.

    Returns (final_state, elapsed_seconds, token_totals) where token_totals is
    ``{"prompt_tokens": int, "completion_tokens": int}``.  The collector is
    attached at the TOP level of the config (not inside ``configurable``) so
    that LangChain actually delivers callback events — Amendment 1.
    """
    import os

    os.environ["SMG_MODEL"] = model
    graph = build_graph(checkpointer=MemorySaver())
    collector = NodeUsageCollector()
    # Amendment 1: callbacks at the top level of config, not inside configurable.
    config = {"configurable": {"thread_id": f"{model}:{fixture_path.name}"},
              "callbacks": [collector]}

    started = time.monotonic()
    list(graph.stream(initial_state(str(fixture_path), "", "", None),
                      config=config, stream_mode="updates"))
    while graph.get_state(config).interrupts:
        list(graph.stream(Command(resume=True), config=config,
                          stream_mode="updates"))
    elapsed = time.monotonic() - started

    return graph.get_state(config).values, elapsed, collector.totals()


def main() -> int:
    config = load_config()
    catalogue = fetch_catalogue()
    available, missing = verify_models(config["candidates"], catalogue)
    if missing:
        print(f"[WARN] not served by OpenRouter right now, skipped: {missing}")
    if not available:
        print("[ERROR] none of the configured models are available.")
        return 1

    ground_truth = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))
    rubric = RUBRIC.read_text(encoding="utf-8")
    judge_llm = make_llm(config["judge"])

    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / "compare_models.csv"

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for model in available:
            for name in sorted(ground_truth):
                gt = ground_truth[name]
                row = {c: "" for c in CSV_COLUMNS}
                row["model"], row["fixture"] = model, name
                try:
                    final, elapsed, token_totals = run_fixture(model, FIXTURES / name)
                    row["wall_clock_s"] = round(elapsed, 2)
                    row["prompt_tokens"] = token_totals["prompt_tokens"]
                    row["completion_tokens"] = token_totals["completion_tokens"]

                    if gt.get("refused"):
                        # Controller amendment: score on refusal, not rule recall.
                        row.update(score_refused_fixture(final, gt))
                    else:
                        row.update(deterministic_metrics(final, gt))
                        # Only run the LLM judge for non-refused fixtures where a
                        # report and safe script exist.
                        scores = judge_one(judge_llm, rubric, name,
                                           final.get("report_md") or "",
                                           final.get("safe_sql") or "")
                        row["judge_relevance"] = scores["relevance"]
                        row["judge_accuracy"] = scores["accuracy"]
                        row["judge_completeness"] = scores["completeness"]
                        row["judge_format"] = scores["format"]
                        row["judge_total"] = scores["total"]
                except Exception as exc:  # noqa: BLE001
                    row["error"] = str(exc)[:300]
                writer.writerow(row)
                print(f"  {model} / {name}: {row.get('error') or 'ok'}")

    print(f"wrote {output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
