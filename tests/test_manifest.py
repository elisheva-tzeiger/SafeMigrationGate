import json
from pathlib import Path

MANIFEST = json.loads(Path("manifest.json").read_text(encoding="utf-8"))


def test_all_top_level_fields_from_appendix_1_are_present():
    for field in ("name", "version", "description", "creator_name",
                  "contact_information", "release_date", "repository_url",
                  "download_url", "target_audience", "professional_fields",
                  "tasks_capable_of_performing", "examples_of_suitable_questions",
                  "technical_specifications"):
        assert field in MANIFEST, f"missing {field}"


def test_the_technical_specifications_block_is_complete():
    spec = MANIFEST["technical_specifications"]
    assert spec["framework"] == "LangGraph"
    assert spec["llm_provider"] == "OpenRouter"
    assert spec["supported_models"]
    assert "features_enabled" in spec
    assert "required_permissions" in spec


def test_the_declared_features_match_what_was_actually_built():
    """RAG and code execution are the two extensions this project implements.
    Declaring web search or MCP would be a false claim."""
    features = MANIFEST["technical_specifications"]["features_enabled"]
    assert features["rag"] is True
    assert features["code_execution"] is True
    assert features["web_search"] is False
    assert features["mcp_support"] is False


def test_the_placeholders_from_the_example_were_replaced():
    assert MANIFEST["creator_name"] != "John Doe"
    assert "example.com" not in MANIFEST["contact_information"]
    assert "username" not in MANIFEST["repository_url"]


def test_the_supported_models_are_the_ones_the_evals_use():
    import yaml

    configured = set(yaml.safe_load(
        Path("evals/models.yaml").read_text(encoding="utf-8"))["candidates"])
    assert set(MANIFEST["technical_specifications"]["supported_models"]) <= configured


def test_the_readme_documents_the_bm25_decision():
    """The spec requires this rationale in the README so it reads as engineering
    rather than as a shortcut."""
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "BM25" in readme
    assert "embedding" in readme.lower()


def test_the_readme_no_longer_describes_the_deleted_skeleton():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "src/agent.py" not in readme
    assert "ANTHROPIC_API_KEY" not in readme
