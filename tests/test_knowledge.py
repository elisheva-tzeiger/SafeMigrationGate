import pytest

from agent.knowledge import KnowledgeBase, get_knowledge_base
from agent.rules import RULE_TITLES


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return get_knowledge_base()


def test_every_rule_has_a_document(kb):
    missing = [rule_id for rule_id in RULE_TITLES if kb.get(rule_id) is None]
    assert missing == []


def test_doc_count_matches_the_rule_count(kb):
    assert kb.doc_count() == len(RULE_TITLES)


def test_get_returns_a_rule_ref_with_a_source_document(kb):
    ref = kb.get("SG004")
    assert ref["rule_id"] == "SG004"
    assert ref["title"]
    assert ref["source_doc"] == "SG004.md"


def test_get_is_case_insensitive_and_tolerates_whitespace(kb):
    assert kb.get(" sg004 ") == kb.get("SG004")


def test_get_on_an_unknown_rule_returns_none(kb):
    assert kb.get("SG999") is None


def test_every_document_has_the_four_required_sections(kb):
    for rule_id in RULE_TITLES:
        body = kb.body(rule_id)
        for heading in ("## Why it is dangerous", "## The safer pattern",
                        "## A real incident"):
            assert heading in body, f"{rule_id}.md is missing {heading}"


def test_search_finds_the_right_document_for_a_free_text_question(kb):
    hits = kb.search("what happens if I truncate a table", k=3)
    assert "SG005" in {h["rule_id"] for h in hits}


def test_search_ranks_the_best_match_first(kb):
    hits = kb.search("drop database", k=3)
    assert hits[0]["rule_id"] == "SG014"


def test_search_returns_at_most_k(kb):
    assert len(kb.search("delete", k=2)) == 2


def test_context_for_concatenates_documents_with_their_ids(kb):
    context = kb.context_for(["SG001", "SG004"])
    assert "SG001" in context and "SG004" in context
    assert "## Why it is dangerous" in context


def test_context_for_skips_unknown_ids_without_failing(kb):
    context = kb.context_for(["SG001", "SG999"])
    assert "SG001" in context
    assert "SG999" not in context


def test_get_knowledge_base_is_a_singleton():
    assert get_knowledge_base() is get_knowledge_base()
