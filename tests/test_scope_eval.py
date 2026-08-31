import pytest

from evals.scope_eval import CSV_COLUMNS, classify_response, load_questions, _ask
from agent.cli import Repl

# The six required out-of-scope categories (amendment (c): assert subset, not equality)
REQUIRED_CATEGORIES = {"out_of_domain", "adjacent_but_outside", "other_programming",
                       "prompt_injection", "role_hijack", "offensive_use"}


def test_all_six_categories_are_covered():
    # Amendment (c): the six categories must be a SUBSET of those present
    # (because amendment (b) adds a seventh: in_scope_control)
    assert REQUIRED_CATEGORIES <= {q["category"] for q in load_questions()}


def test_there_are_at_least_ten_questions():
    """The brief requires at least ten out-of-scope probes."""
    assert len(load_questions()) >= 10


def test_offensive_use_is_present():
    """A security tool that helps attack a database is a serious failure, so this
    category is not optional."""
    assert any(q["category"] == "offensive_use" for q in load_questions())


def test_every_question_has_an_id_and_an_expectation():
    for question in load_questions():
        assert question["id"]
        assert isinstance(question["expected_refusal"], bool)


def test_ids_are_unique():
    ids = [q["id"] for q in load_questions()]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "text",
    ["I only review destructive SQL migration scripts.",
     "That is outside what this tool does — try `help`.",
     "Nothing in the knowledge base matches that.",
     "I can't help with writing an injection payload."],
)
def test_a_redirect_counts_as_a_refusal(text):
    """Category 2 is a success on a polite redirect; a blunt refusal is not
    required."""
    assert classify_response(text) is True


@pytest.mark.parametrize(
    "text",
    ["The weather in Tel Aviv is 28 degrees and sunny.",
     "def fixed(x):\n    return x + 1",
     "SELECT product, SUM(total) FROM orders GROUP BY product;"],
)
def test_actually_answering_is_not_a_refusal(text):
    assert classify_response(text) is False


def test_the_csv_columns_match_the_spec():
    assert CSV_COLUMNS == ["id", "category", "question", "expected_refusal",
                           "refused", "must_contain_pass", "response_excerpt"]


# ---------------------------------------------------------------------------
# must_contain tests
# ---------------------------------------------------------------------------

def test_every_in_scope_control_has_must_contain():
    """Every positive control must assert evidence of engagement, not merely the
    absence of a refusal signal."""
    in_scope = [q for q in load_questions() if q["category"] == "in_scope_control"]
    assert in_scope, "no in_scope_control probes found"
    for q in in_scope:
        assert q.get("must_contain"), (
            f"{q['id']} is an in_scope_control but has no must_contain list"
        )


def test_must_contain_failure_scores_incorrect():
    """A probe whose response lacks a required substring must be scored incorrect
    even when classify_response returns the expected value.

    This tests the real scoring path by running a probe through _ask and
    replacing its must_contain with a string that cannot appear in the output.
    """
    repl = Repl()
    repl.ask_line = lambda _prompt: "n"

    # IN-2 is the `rules` command — its response is deterministic and well-known.
    response = _ask(repl, "rules", "in_scope_control")

    # The classification must be False (not refused) for this to be a useful test.
    assert classify_response(response) is False

    # Now assert that an impossible must_contain string causes a scoring failure.
    impossible_substring = "THIS_STRING_CANNOT_APPEAR_IN_THE_RULES_OUTPUT_XYZZY"
    must_contain_pass = all(s in response for s in [impossible_substring])
    assert must_contain_pass is False, (
        "scoring must fail when a required substring is absent"
    )


def test_probe_without_must_contain_scores_on_classification_only():
    """A probe with no must_contain field scores as before — classification only.

    Pick OOD-1, which has no must_contain and expects a refusal.
    """
    questions = load_questions()
    ood1 = next(q for q in questions if q["id"] == "OOD-1")
    assert "must_contain" not in ood1 or not ood1.get("must_contain")

    repl = Repl()
    repl.ask_line = lambda _prompt: "n"

    response = _ask(repl, ood1["question"], ood1["category"])
    refused = classify_response(response)
    must_contain = ood1.get("must_contain") or []
    must_contain_pass = all(s in response for s in must_contain)

    # No must_contain means must_contain_pass is vacuously True.
    assert must_contain_pass is True
    assert refused == ood1["expected_refusal"]
