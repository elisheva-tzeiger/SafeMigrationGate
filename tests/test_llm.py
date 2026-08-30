import pytest
from langchain_core.language_models import FakeListChatModel

from agent.llm import (
    DEFAULT_MODEL,
    MissingCredentials,
    extract_json,
    invoke_json,
    invoke_text,
    make_llm,
    scrub,
)


def test_make_llm_without_a_key_names_the_environment_variable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(MissingCredentials) as excinfo:
        make_llm()
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_make_llm_points_at_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    llm = make_llm()
    assert llm.openai_api_base == "https://openrouter.ai/api/v1"
    assert llm.model_name == DEFAULT_MODEL
    assert llm.temperature == 0.0


def test_make_llm_honours_an_explicit_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert make_llm("anthropic/claude-sonnet-4.5").model_name == \
        "anthropic/claude-sonnet-4.5"


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict": "proceed"}',
        '```json\n{"verdict": "proceed"}\n```',
        'Sure! Here it is:\n{"verdict": "proceed"}\nHope that helps.',
        '```\n{"verdict": "proceed"}\n```',
    ],
)
def test_extract_json_survives_the_usual_model_packaging(raw):
    assert extract_json(raw) == {"verdict": "proceed"}


def test_extract_json_handles_nested_braces():
    assert extract_json('{"a": {"b": 1}}') == {"a": {"b": 1}}


def test_extract_json_raises_when_there_is_no_object():
    with pytest.raises(ValueError):
        extract_json("I cannot help with that.")


def test_invoke_json_parses_a_fake_model_response():
    llm = FakeListChatModel(responses=['{"severity": "CRITICAL"}'])
    assert invoke_json(llm, "sys", "user", run_name="classify_risk") == \
        {"severity": "CRITICAL"}


def test_invoke_json_raises_value_error_on_prose():
    llm = FakeListChatModel(responses=["I'd rather not."])
    with pytest.raises(ValueError):
        invoke_json(llm, "sys", "user", run_name="classify_risk")


def test_invoke_text_returns_the_content():
    llm = FakeListChatModel(responses=["# Risk report"])
    assert invoke_text(llm, "sys", "user", run_name="report") == "# Risk report"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("connecting to postgresql://alice:s3cr3t@db/prod now",
         "connecting to postgresql://alice:***@db/prod now"),
        ("key sk-or-v1-abcdef0123456789abcdef done", "key sk-*** done"),
        ("nothing sensitive here", "nothing sensitive here"),
    ],
)
def test_scrub_removes_secrets_before_they_reach_logs(raw, expected):
    assert scrub(raw) == expected


def test_install_tls_trust_is_idempotent():
    from agent.llm import install_tls_trust

    install_tls_trust()
    install_tls_trust()  # must not raise on the second call
