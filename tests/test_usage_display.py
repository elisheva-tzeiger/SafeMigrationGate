from agent import cli
from agent.cli import Repl
from agent.usage import NodeUsageCollector, cost_for


def collector_with_usage():
    collector = NodeUsageCollector()
    collector.record("guardrail_input", 200, 10)
    collector.record("report", 800, 400)
    return collector


def test_the_usage_table_shows_each_node_and_the_total(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_prices", lambda model: (0.000_001, 0.000_002))
    repl = Repl()
    repl._show_usage(collector_with_usage())
    out = capsys.readouterr().out
    assert "guardrail_input" in out and "report" in out and "1,000" in out
    # 1000 prompt * 1e-6 + 410 completion * 2e-6 = 0.001820
    assert "$0.0018" in out


def test_an_unreachable_price_list_says_cost_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_prices", lambda model: None)
    Repl()._show_usage(collector_with_usage())
    assert "unavailable" in capsys.readouterr().out


def test_a_free_model_is_labelled_free(monkeypatch, capsys):
    monkeypatch.setenv("SMG_MODEL", "some/model:free")
    monkeypatch.setattr(cli, "fetch_prices", lambda model: (0.0, 0.0))
    Repl()._show_usage(collector_with_usage())
    out = capsys.readouterr().out
    assert "free model" in out and "$0.0000" in out


def test_nothing_is_printed_when_no_model_was_called(capsys):
    Repl()._show_usage(NodeUsageCollector())
    assert capsys.readouterr().out == ""


def test_cost_is_tokens_times_price():
    assert cost_for(1_000, 500, (0.000_001, 0.000_002)) == 0.002
