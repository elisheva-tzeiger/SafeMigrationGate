"""The tool layer: the three capabilities the agent reaches outside itself for.

Thin on purpose -- the real work lives in db.py, knowledge.py and resources.py.
This module is the seam the graph talks to, and the one place that knows how a
missing database or a missing document should degrade.
"""

from __future__ import annotations

from pathlib import Path

from agent import resources
from agent.db import DryRunFailure, DryRunRow, dry_run_script, make_engine
from agent.knowledge import get_knowledge_base
from agent.state import RuleRef


def measure_impact(sql_statements: list[str], db_url: str | None) -> list[DryRunRow]:
    """Code-exec extension: BEGIN, run, read rowcount, ROLLBACK. Never commits."""
    target = db_url or resources.demo_db_url()
    engine = make_engine(target)
    try:
        return dry_run_script(engine, sql_statements)
    finally:
        engine.dispose()


def lookup_rules(rule_ids: list[str]) -> list[RuleRef]:
    """RAG extension, lookup path: the rule ids are already known."""
    kb = get_knowledge_base()
    seen, refs = set(), []
    for rule_id in rule_ids:
        if rule_id in seen:
            continue
        seen.add(rule_id)
        ref = kb.get(rule_id)
        if ref is not None:
            refs.append(ref)
    return refs


def search_knowledge(question: str, k: int = 3) -> list[RuleRef]:
    """RAG extension, ranked path: a free-text question from the CLI."""
    return get_knowledge_base().search(question, k=k)


def output_paths(source_path: str) -> tuple[Path, Path]:
    source = Path(source_path)
    stem = source.stem
    return (source.with_name(f"{stem}_safe.sql"),
            source.with_name(f"{stem}_report.md"))


def save_outputs(source_path: str, safe_sql: str | None, report_md: str | None) -> list[str]:
    safe_path, report_path = output_paths(source_path)
    written: list[str] = []
    if safe_sql is not None:
        written.append(str(resources.write_text_file(safe_path, safe_sql)))
    if report_md is not None:
        written.append(str(resources.write_text_file(report_path, report_md)))
    return written


__all__ = ["measure_impact", "lookup_rules", "search_knowledge",
           "save_outputs", "output_paths", "DryRunFailure"]
