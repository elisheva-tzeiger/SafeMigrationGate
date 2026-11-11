"""The Track A REPL. Every print and every prompt in the project lives here.

The graph does not know a terminal exists; this module streams its updates and
answers its interrupts. That is what lets the desktop track reuse the graph
untouched.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from rich.console import Console
from rich.table import Table

from agent import tools
from agent.db import mask_url
from agent.graph import build_graph
from agent.knowledge import get_knowledge_base
from agent.llm import DEFAULT_MODEL, MissingCredentials, scrub
from agent.nodes import IngestFailure
from agent.rules import RULE_SEVERITY, RULE_TITLES, BLOCKING_RULES
from agent.state import initial_state
from agent.usage import NodeUsageCollector, cost_for, fetch_prices

HELP_TEXT = """\
SafeMigrationGate — a pre-flight gate for destructive SQL.

  check <path.sql>      analyse a script and produce a safe version + risk report
  <path.sql>            the same thing; drag a file onto the terminal
  db <connection>       point the dry run at a real database
  db demo               go back to the bundled demo database (the default)
  rules                 list the detection rules
  why <rule_id>         explain one rule, e.g. `why SG001`
  <any question>        search the safety knowledge base
  summary <path.txt>    export this session's transcript
  help  /  ?            this text
  quit                  exit

This tool never COMMITs. Every database run is a transaction that ends in
ROLLBACK, and the output is files on disk. Whether to run them is your call.

Privacy: the text of a script you check is sent to OpenRouter and to the model
provider behind it. Free models may log or train on what they receive. Do not
check scripts that contain secrets or personal data.
"""

_COMMANDS = {"check", "db", "rules", "why", "summary", "help", "quit", "exit", "?"}


def parse_command(line: str) -> tuple[str, str]:
    """Split a REPL line into a verb and its argument.

    A bare path is treated as `check`, so dragging a file in just works. Quotes
    are stripped first: Windows wraps dropped paths that contain spaces.
    """
    stripped = line.strip()
    if not stripped:
        return "", ""

    unquoted = stripped.strip('"').strip("'")
    if Path(unquoted).is_file():
        return "check", unquoted

    verb, _, argument = stripped.partition(" ")
    verb = verb.lower()
    if verb in {"?", "help"}:
        return "help", argument.strip()
    if verb == "exit":
        return "quit", argument.strip()
    if verb in _COMMANDS:
        return verb, argument.strip().strip('"').strip("'")
    return verb, ""


class Repl:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.db_url: str | None = None
        self.transcript: list[str] = []
        self.graph = build_graph(checkpointer=MemorySaver())
        self.ask_line = input

    # -- output helpers --------------------------------------------------------
    def say(self, message: str) -> None:
        self.console.print(scrub(message))

    def _confirm(self, question: str) -> bool:
        try:
            answer = self.ask_line(f"{question} [Y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    # -- commands --------------------------------------------------------------
    def handle(self, line: str) -> bool:
        command, argument = parse_command(line)

        if command in {"", "help"}:
            self.say(HELP_TEXT if command == "help" else "")
            return True
        if command == "quit":
            return False
        if command == "db":
            return self._cmd_db(argument)
        if command == "rules":
            return self._cmd_rules()
        if command == "why":
            return self._cmd_why(argument)
        if command == "summary":
            return self._cmd_summary(argument)
        if command == "check":
            return self._cmd_check(argument)
        return self._cmd_ask(line.strip())

    def _cmd_db(self, argument: str) -> bool:
        if not argument or argument.lower() == "demo":
            self.db_url = None
            self.say("[green]Target: the bundled demo database.[/green]")
        else:
            self.db_url = argument
            self.say(f"[green]Target: {mask_url(argument)}[/green]")
        self.transcript.append(f"db -> {mask_url(self.db_url or 'demo')}")
        return True

    def _cmd_rules(self) -> bool:
        table = Table(title="Detection rules")
        table.add_column("ID")
        table.add_column("Rule")
        table.add_column("Severity")
        table.add_column("Blocks?")
        for rule_id, title in sorted(RULE_TITLES.items()):
            table.add_row(rule_id, title, RULE_SEVERITY[rule_id],
                          "yes" if rule_id in BLOCKING_RULES else "no")
        self.console.print(table)
        return True

    def _cmd_why(self, argument: str) -> bool:
        if not argument:
            self.say("Usage: why <rule_id>, for example `why SG001`.")
            return True
        body = get_knowledge_base().body(argument)
        if body is None:
            self.say(f"[yellow]No knowledge document for {argument}.[/yellow] "
                     "Try `rules` for the list.")
            return True
        self.say(body)
        self.transcript.append(f"why {argument}")
        return True

    def _cmd_ask(self, question: str) -> bool:
        hits = tools.search_knowledge(question, k=3)
        if not hits:
            self.say("I only review destructive SQL migration scripts. "
                     "[yellow]Nothing in the knowledge base matches that.[/yellow] "
                     "Try `help`.")
            return True
        self.say("I only review destructive SQL migration scripts. "
                 "The closest entries in the safety knowledge base are:")
        for ref in hits:
            self.say(f"  [bold]{ref['rule_id']}[/bold] — {ref['title']} "
                     f"([dim]{ref['source_doc']}[/dim])")
        self.say("Use `why <rule_id>` to read one in full.")
        self.transcript.append(f"ask: {question}")
        return True

    def _cmd_summary(self, argument: str) -> bool:
        if not argument:
            self.say("Usage: summary <path.txt>")
            return True
        from agent.resources import write_text_file

        # scrub before writing: the transcript holds whatever the user typed,
        # which may include an API key or a connection string pasted by hand.
        path = write_text_file(Path(argument),
                               scrub("\n".join(self.transcript)) + "\n")
        self.say(f"[green]Transcript written to {path}[/green]")
        return True

    # -- the main flow ---------------------------------------------------------
    def _cmd_check(self, argument: str) -> bool:
        if not argument:
            self.say("Usage: check <path.sql>")
            return True

        collector = NodeUsageCollector()
        # callbacks go at the top level of the config, not inside `configurable`,
        # or LangChain delivers no events to them
        config = {"configurable": {"thread_id": str(uuid.uuid4())},
                  "callbacks": [collector]}
        state = initial_state(argument, "", "", self.db_url)

        try:
            self._pump(self.graph.stream(state, config=config,
                                         stream_mode="updates"))
            while self._answer_interrupt(config):
                pass
        except IngestFailure as exc:
            self.say(f"[red]Cannot analyse {argument}:[/red] {exc}")
            return True
        except MissingCredentials as exc:
            self.say(f"[red]{exc}[/red]")
            return True
        except Exception as exc:  # noqa: BLE001
            if "--debug" in sys.argv:
                raise
            self.say(f"[red]Unexpected failure:[/red] {exc}\n"
                     "Re-run with --debug for the full traceback.")
            return True

        self._summarise(self.graph.get_state(config).values)
        self._show_usage(collector)
        return True

    def _pump(self, stream) -> None:
        for chunk in stream:
            for node, update in chunk.items():
                if node.startswith("__"):
                    continue
                self.say(f"[dim][INFO][/dim] Entering node: [bold]{node}[/bold]")
                if node == "static_analysis":
                    ids = sorted({f["rule_id"] for f in update.get("findings") or []})
                    self.say(f"        risk={update.get('risk_level')} "
                             f"blocked={update.get('blocked')} "
                             f"rules={', '.join(ids) or 'none'}")
                if node == "classify_risk":
                    self.say("        [dim](RAG: knowledge base consulted)[/dim]")
                for error in update.get("errors") or []:
                    self.say(f"        [yellow][WARN][/yellow] {error['node']}: "
                             f"{error['message']}")

    def _answer_interrupt(self, config) -> bool:
        """Return True if another interrupt may follow."""
        snapshot = self.graph.get_state(config)
        if not snapshot.interrupts:
            return False
        payload = snapshot.interrupts[0].value

        if payload["kind"] == "dry_run":
            target = mask_url(payload["db_url"] or "the bundled demo database")
            self.say(f"\n[bold]About to measure impact against {target}.[/bold]")
            self.say("Nothing will be committed: the run is a transaction that "
                     "ends in ROLLBACK.")
            approved = self._confirm("Run the dry run?")
        else:
            self._impact_table(payload["impact"])
            self.say(f"\nAbout to write:\n  {payload['safe_path']}\n"
                     f"  {payload['report_path']}")
            if payload["safe_exists"] or payload["report_exists"]:
                self.say("[yellow]One or more of these files already exists and "
                         "will be overwritten.[/yellow]")
            approved = self._confirm("Write these files?")

        self._pump(self.graph.stream(Command(resume=approved), config=config,
                                     stream_mode="updates"))
        return True

    def _impact_table(self, impact) -> None:
        table = Table(title="Measured impact")
        table.add_column("Statement")
        table.add_column("Rows affected", justify="right")
        table.add_column("Confidence")
        table.add_column("Note")
        for row in impact:
            count = ("not measured" if row["rows_affected"] is None
                     else f"{row['rows_affected']:,}")
            table.add_row(str(row["statement_index"]), count,
                          row["confidence"], row["note"])
        self.console.print(table)

    def _show_usage(self, collector: NodeUsageCollector) -> None:
        """Tokens and cost for the run just finished, per node and in total."""
        totals = collector.totals()
        if not totals["prompt_tokens"] and not totals["completion_tokens"]:
            return
        model = os.environ.get("SMG_MODEL") or DEFAULT_MODEL
        prices = fetch_prices(model)

        table = Table(title=f"Usage - {model}")
        table.add_column("Node")
        table.add_column("Prompt tokens", justify="right")
        table.add_column("Completion tokens", justify="right")
        for node, usage in sorted(collector.usage.items()):
            table.add_row(node, f"{usage['prompt_tokens']:,}",
                          f"{usage['completion_tokens']:,}")
        table.add_row("[bold]total[/bold]", f"{totals['prompt_tokens']:,}",
                      f"{totals['completion_tokens']:,}")
        self.console.print(table)

        if prices is None:
            line = "Cost: unavailable (could not read the price list)."
        else:
            cost = cost_for(totals["prompt_tokens"], totals["completion_tokens"], prices)
            line = f"Cost: ${cost:.4f}" + (" (free model)" if model.endswith(":free") else "")
        self.say(line)
        self.transcript.append(
            f"usage: {totals['prompt_tokens']} prompt + "
            f"{totals['completion_tokens']} completion tokens; {line}")

    def _summarise(self, values: dict) -> None:
        if values.get("verdict") == "refused":
            self.say(f"\n[red]Refused:[/red] {values.get('refusal_reason')}")
            self.transcript.append(f"check -> refused: {values.get('refusal_reason')}")
            return
        risk = values.get("risk_level", "LOW")
        colour = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow"}.get(risk, "green")
        self.say(f"\n[{colour}]Overall risk: {risk}[/{colour}] | "
                 f"Blocked: {'yes' if values.get('blocked') else 'no'}")
        for path in values.get("written_files") or []:
            self.say(f"  wrote {path}")
        if not values.get("written_files"):
            self.say("  [dim]nothing written[/dim]")
        self.transcript.append(
            f"check {values.get('source_path')} -> {risk}, "
            f"blocked={values.get('blocked')}, files={values.get('written_files')}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from dotenv import load_dotenv

    load_dotenv()

    console = Console(no_color="--no-color" in argv)
    repl = Repl(console=console)
    console.print("[bold]SafeMigrationGate[/bold] — type `help` for commands, "
                  "`quit` to exit.")

    while True:
        try:
            line = input("smg> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not repl.handle(line):
            return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
