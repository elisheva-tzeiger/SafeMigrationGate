# SafeMigrationGate

A pre-flight gate for destructive SQL migration scripts.

SafeMigrationGate analyses a migration script before you run it on production.
It detects dangerous patterns from the syntax tree, measures exactly how many
rows each statement would touch using a dry run that ends in ROLLBACK, and
produces a safe version of the script together with a risk report.

**It never commits.** Every database operation is wrapped in a transaction
that ends in `ROLLBACK`. The output is two files on disk — a safe script and
a risk report. Whether to execute them is your decision.

---

## What the agent does and does not do

**Does:**
- Parse your SQL with `sqlglot` and check it against 15 deterministic safety rules (SG001–SG015)
- Measure the real row count for each DML statement via a `BEGIN … ROLLBACK` dry run
- Classify the overall risk level (LOW / MEDIUM / HIGH / CRITICAL) with an LLM that reads the safety-rule knowledge base
- Rewrite the script into a safe, non-committing version: every destructive statement is preceded by a `SELECT COUNT(*)`, wrapped in a transaction, and ends with `ROLLBACK`
- Produce a Markdown risk report with a findings table and confidence-tagged row counts
- Answer questions about safety rules from a local knowledge base (`why SG004`, free-text search)

**Does not:**
- `COMMIT` — ever. There is no code path that does
- Make permanent changes to your database during analysis
- Invent row counts — every number it reports was measured, or is explicitly tagged `unverified`
- Analyse arbitrary questions — it reviews destructive SQL migration scripts and nothing else

---

## Architecture

The pipeline is a LangGraph `StateGraph` with twelve nodes: nine that do the work, two human-in-the-loop approval gates (`approve_dry_run`, `approve_write`), and one skip path (`skip_dry_run`). Every node reads from and writes to `GateState`; nodes never communicate directly.

```
START
  │
  ▼
ingest ──► guardrail_input
                │ refused
                ▼            │ passed
              report ◄───────┘
                │
                ▼ (refused or blocked → END)
          static_analysis
                │
                ▼
          classify_risk
                │ blocked (SG014 / SG015)
                ├──────────────────────► report ──► END
                │ passes
                ▼
         [GATE 1] approve_dry_run
                │ approved        │ declined
                ▼                 ▼
         dry_run_impact     skip_dry_run
                │                 │
                └────────┬────────┘
                         ▼
                    rewrite_safe ◄────────────┐
                         │                   │
                         ▼                   │ failed, attempts < 3
                      evaluate ──────────────┘
                         │ passed (or max attempts)
                         ▼
                       report
                         │
                         ▼
              [GATE 2] approve_write
                         │ approved   │ declined
                         ▼            ▼
                    write_files      END
                         │
                         ▼
                        END
```

### Node table

| Node | Kind | What it does |
|---|---|---|
| `ingest` | deterministic | Reads the file, detects the SQL dialect, splits into statements |
| `guardrail_input` | LLM | Checks for prompt-injection markers; refuses if found |
| `static_analysis` | deterministic | Runs SG001–SG015 over the AST; sets `blocked` |
| `classify_risk` | LLM + RAG | Summarises findings using the knowledge-base context |
| `approve_dry_run` | human gate | Interrupts and asks the operator before touching a database |
| `dry_run_impact` | code exec | BEGIN → run each statement → measure rowcount → ROLLBACK |
| `skip_dry_run` | deterministic | Records `unverified` for all statements when the operator declines |
| `rewrite_safe` | LLM | Rewrites the script; iterates with `evaluate` up to three times |
| `evaluate` | LLM + deterministic | Checks validity, completeness and the ROLLBACK invariant |
| `approve_write` | human gate | Interrupts and shows the report before asking to write files |
| `report` | LLM | Assembles the Markdown risk report; falls back to a template if the model is unavailable |
| `write_files` | deterministic | Writes `<stem>_safe.sql` and `<stem>_report.md` next to the source |

---

## Two output axes: risk_level and blocked

`risk_level` and `blocked` are independent.

- `risk_level` (LOW / MEDIUM / HIGH / CRITICAL) is informational. It describes
  how dangerous the script is, but it does not stop the run.
- `blocked` is a gate. Only two rules set it: **SG014** (DROP DATABASE / DROP
  SCHEMA) and **SG015** (privilege changes). A blocked script has no safe
  rewrite and stops at the `report` node.

Why does a `DELETE` without a `WHERE` clause not block? Because there is a safe
rewrite: the statement is replaced with `WHERE /* FILL: condition */ 1 = 0`,
which matches nothing. The operator reviews the placeholder and decides what
condition they meant. A `DROP DATABASE` has no equivalent placeholder — it
destroys the entire database — so it blocks.

`blocked` is computed in `static_analysis`, before `classify_risk`, so a
blocked script stays blocked even when the model provider is unreachable.

---

## Extensions: Code Execution and RAG

### Code Execution (`dry_run_impact`)

The `dry_run_impact` node opens a single database transaction, runs every
statement, records the `rowcount` after each one, then calls `ROLLBACK`. No
rows are ever committed.

If the operator has not set a connection string (`db demo` or no `db` command),
the dry run runs against the bundled SQLite demo database (`data/demo.db`). In
that case the row counts are tagged `measured_demo_transpiled` — they were
measured, but on a demo database after translating the script to SQLite syntax,
not on the target database. Only `measured_target` means the numbers came from
the real database.

### RAG (`classify_risk`, `why`, free-text search)

The knowledge base contains one Markdown document per rule (SG001–SG015), each
with YAML front matter. It is loaded once at startup.

Two retrieval paths exist:

1. **Direct lookup by rule_id** — `classify_risk` already knows which rules
   fired from the `findings` list. It calls `context_for(rule_ids)`, which is a
   dictionary lookup. This is the common case.
2. **Ranked search** — the `why` command and free-text CLI questions use
   `BM25Okapi` ranking over the tokenised documents.

#### Why BM25 instead of embeddings

OpenRouter serves no embedding endpoint. Using embeddings would require a second
provider, which contradicts the project's required stack (a single OpenRouter
key, no local model server). A local embedding model would also inflate the
one-file executable produced by PyInstaller.

Over a 15-document corpus whose vocabulary is fixed technical terminology —
DROP, TRUNCATE, CASCADE, WHERE, ROLLBACK — BM25's weakness on synonyms barely
applies: the documents are already written in the same dialect as the queries.
Most retrieval is a dictionary lookup (path 1 above), where BM25 is not used at
all. The ranked path (path 2) is exercised only by free-text CLI questions.

---

## Safety rules: SG001–SG015

| ID | Title | Severity | Blocks? | Heuristic? |
|---|---|---|---|---|
| SG001 | DELETE without WHERE | CRITICAL | no | no |
| SG002 | UPDATE without WHERE | CRITICAL | no | no |
| SG003 | Tautological WHERE clause | CRITICAL | no | no |
| SG004 | DROP TABLE | CRITICAL | no | no |
| SG005 | TRUNCATE | CRITICAL | no | no |
| SG006 | ALTER TABLE … DROP COLUMN | HIGH | no | no |
| SG007 | Column type change | HIGH | no | yes — see Known Limitations |
| SG008 | UPDATE on a key column | HIGH | no | yes — see Known Limitations |
| SG009 | Explicit CASCADE | HIGH | no | no |
| SG010 | WHERE depends on an unbounded subquery | MEDIUM | no | no |
| SG011 | Script is not wrapped in a transaction | MEDIUM | no | no |
| SG012 | DDL and DML mixed in one script | MEDIUM | no | no |
| SG013 | Large-scope DML without LIMIT or batching | HIGH | no | no (requires measurement) |
| SG014 | DROP DATABASE / DROP SCHEMA | CRITICAL | **yes** | no |
| SG015 | Privilege change | HIGH | **yes** | yes — see Known Limitations |

Detection for every rule except SG013 is pure AST matching via `sqlglot`.
SG013 fires only after a dry run measures more than 100,000 affected rows.

---

## Installation and setup

**Prerequisites:** Python 3.10 or later, `pip`.

```sh
git clone https://github.com/elisheva-tzeiger/SafeMigrationGate
cd SafeMigrationGate
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your `OPENROUTER_API_KEY`:

```sh
cp .env.example .env
# edit .env and set OPENROUTER_API_KEY=sk-or-...
```

**Build the demo database** (`data/demo.db` is not tracked in the repository
— it must be created before first use; run this again any time you need to
reset it):

```sh
.venv\Scripts\python.exe -m data.seed   # Windows
.venv/bin/python -m data.seed           # macOS / Linux
```

---

## Running the agent

### From source (REPL)

```sh
.venv\Scripts\python.exe -m agent       # Windows
.venv/bin/python -m agent               # macOS / Linux
```

The REPL accepts the following commands:

| Command | What it does |
|---|---|
| `check <path.sql>` | Analyse a migration script |
| `<path.sql>` | Same — drag a file onto the terminal and it just works |
| `db <connection>` | Point the dry run at a real database (SQLAlchemy URL) |
| `db demo` | Go back to the bundled demo database |
| `rules` | List all detection rules |
| `why <rule_id>` | Explain one rule in full, e.g. `why SG001` |
| `<any question>` | Search the safety knowledge base |
| `summary <path.txt>` | Export the session transcript |
| `help` / `?` | Show help text |
| `quit` | Exit |

### From the compiled executable

```sh
agent.exe
```

The executable is produced by PyInstaller and bundles the Python interpreter,
all dependencies, and the knowledge base into a single file. Build it with:

```sh
.venv\Scripts\pyinstaller.exe --onefile agent/__main__.py --name agent
```

---

## Running the evaluations

Evals 1 and 2 require `OPENROUTER_API_KEY` and network access to
`openrouter.ai`. **Eval 3 does not** — it exercises deterministic CLI routing
and can be run today, offline (see its section below). Results are written to
`evals/results/`.

> **Status of the results (November 2026).** All three evals have been run
> against OpenRouter. The paid models (`gpt-4o-mini`, `claude-haiku-4.5`,
> `claude-sonnet-4.5`) have complete results. The free model
> (`nvidia/nemotron-3-ultra-550b-a55b:free`) is blocked by OpenRouter's daily
> free-tier quota (HTTP 429) once the quota is spent, so its rows are recorded
> as errors in the `error` column rather than as scores. A model that never
> answers is an infrastructure failure, not a measurement, and the eval says so.
> Re-run after the quota resets, or with credits on the account, to fill them in.

### Eval 1 — model comparison (`evals/results/compare_models.csv`)

Runs the complete pipeline on seven fixtures with each of the four configured
models, scores the output with a LLM judge and deterministic ground-truth
metrics (rule recall, rule precision, invariant held).

> **Note on model lists:** `manifest.json` lists three `supported_models` —
> the models recommended for general use. `evals/models.yaml` lists four
> candidates; the extra entry (`anthropic/claude-haiku-4.5`) is included in
> Eval 1 for comparison purposes only and is not in the recommended set.
> The manifest test asserts a subset relationship, not equality.

```sh
.venv\Scripts\python.exe -m evals.compare_models
```

**Known self-judging issue:** the judge model is `anthropic/claude-sonnet-4.5`,
which is also one of the four candidates. One row of the comparison table is
therefore self-judged. This is a limitation of Eval 1 and should be read
accordingly.

Columns: `model`, `fixture`, `wall_clock_s`, `prompt_tokens`,
`completion_tokens`, `judge_relevance`, `judge_accuracy`,
`judge_completeness`, `judge_format`, `judge_total`, `rule_recall`,
`rule_precision`, `invariant_held`, `error`.

### Eval 2 — token cost by node (`evals/results/cost_eval.csv`)

Runs four representative fixtures against each model and breaks token usage
down per graph node. Free models report actual cost 0 alongside a theoretical
cost calculated from the equivalent paid model's price.

```sh
.venv\Scripts\python.exe -m evals.cost_eval
```

**Offline-test caveat:** the per-node attribution tests use `FakeListChatModel`,
which populates no `usage_metadata`. The tests confirm that usage is bucketed
by the correct node name, but they cannot verify token counts. The first real
run against OpenRouter should be spot-checked to confirm the numbers are
attributed to the right nodes.

### Eval 3 — scope / refusal (`evals/results/scope_eval.csv`)

Sends a set of out-of-scope probes through the REPL and measures whether the
agent refuses them correctly. Includes one in-scope positive control (`why SG001`)
and prompt-injection probes routed through `guardrail_input`.

```sh
.venv\Scripts\python.exe -m evals.scope_eval
```

This eval does not require OpenRouter for the refusal probes — the deterministic
guardrail and knowledge-base lookup run entirely offline. Only the `check` path
that reaches the LLM nodes requires a key.

> **What Eval 3 actually measures.** Its name oversells it. `Repl._cmd_ask`
> never calls a model — it is BM25 retrieval over the knowledge base plus
> printing — and both of its branches open with the same fixed scope sentence,
> which itself contains the refusal signal `"only review"`. So every free-text
> probe is classified as refused *structurally*, from deterministic CLI
> routing, and a genuinely in-scope free-text question would be classified the
> same way. **Eval 3 exercises no LLM behaviour and requires no network
> access.** What it pins down is that the CLI routes out-of-scope input to the
> scope response and that `guardrail_input`'s injection markers fire — both
> real properties, and worth regression-testing, but they are not evidence of
> model professionalism or non-leakage. Unlike Evals 1 and 2, this one can be
> run today.

---

## Environment notes

### Corporate TLS inspection (truststore)

If your network terminates TLS at an inspecting proxy, every HTTPS call to
OpenRouter will fail with:

```
CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier
```

The project requires `truststore` (listed in `requirements.txt`). It is
injected automatically at startup via `llm.install_tls_trust()`, which
instructs Python's SSL layer to trust the operating system's certificate store
rather than `certifi`'s bundled store. If your proxy's root certificate is
installed in the OS store, this resolves the error with no additional
configuration.

### If openrouter.ai is blocked

Set `OPENROUTER_API_KEY` to any non-empty string and observe whether the error
is a 401 (wrong key) or a connection failure. A connection failure means the
host is blocked at the network level — work with your network administrator or
use a device outside the inspecting proxy.

Evals 1 and 2 require a reachable `openrouter.ai`; Eval 3 does not. The offline
test suite (309 tests) runs entirely without network access using
`FakeListChatModel`.

---

## Known limitations

### Three heuristic rules

**SG007 — Column type change.** Without access to the database schema, the rule
cannot compare the current column type with the new one. It flags every `ALTER
COLUMN … TYPE` and says so explicitly in the finding message: *"without schema
access the current type was not compared, so narrowing and truncation cannot be
ruled out."* If the new type is wider (e.g. `VARCHAR(50)` to `TEXT`) there is
no data loss, but the rule cannot determine that without the schema.

**SG008 — UPDATE on a key column.** Key columns are identified by name only:
`id`, `*_id`, `uuid`, `guid`, `pk`. A column named `legacy_reference` that is
the primary key will not be flagged. A column named `order_id` that is not a
key will be. This is a name-matching heuristic, not a schema lookup.

**SG015 — Privilege change.** `sqlglot` does not parse `CREATE USER`,
`ALTER ROLE`, or `DROP LOGIN`. These statements produce a generic `Command` node.
SG015 applies a single regex (`CREATE|ALTER|DROP` + `USER|ROLE|LOGIN`) to those
`Command` nodes only. Every other rule in the ruleset is pure AST matching.
Statements that `sqlglot` _does_ parse — `GRANT` and `REVOKE` — are caught via
their AST node types, not the regex.

### Confidence tags on row counts

A row count tagged `measured_demo_transpiled` was measured against the bundled
demo database (`data/demo.db`) after translating the script to SQLite syntax.
It is **not** a number from your database. It tells you the order of magnitude
on the demo data, which may be very different from production.

Only `measured_target` means the count was measured on the database you pointed
the agent at with the `db <connection>` command.

A count tagged `unverified` was not measured at all — either the operator
declined the dry run, the database was unreachable, or the statement failed
during the dry run.

### Self-judging in Eval 1

The judge model for Eval 1 is `anthropic/claude-sonnet-4.5`, which is also one
of the four candidate models being compared. One row in the
`compare_models.csv` comparison is therefore self-judged. The deterministic
metrics (`rule_recall`, `rule_precision`, `invariant_held`) are not affected —
they are computed against the ground-truth fixture data without any model
involvement.

### Token counts in Eval 2 are unverified offline

`FakeListChatModel` (used in the offline test suite) populates no
`usage_metadata`. The `test_cost_eval.py` tests confirm that the
`NodeUsageCollector` correctly attributes usage to the right bucket *name*
(e.g. `guardrail_input`, `classify_risk`) but cannot verify that the counts are
non-zero. The first real run of `python -m evals.cost_eval` against OpenRouter
should be spot-checked.

### Eval 3 measures CLI routing, not model scope adherence

Eval 3 is named "scope / refusal", which is broader than what it tests.
`Repl._cmd_ask` never invokes a model — it is BM25 retrieval plus printing —
and both of its branches open with the same fixed scope sentence, which
contains the refusal signal `"only review"`. Every free-text probe therefore
classifies as refused *structurally*, from deterministic routing rather than
from any behavioural choice; an in-scope free-text question would classify the
same way. `classify_response` is correspondingly just a refusal-signal check
(it previously also carried a SQL-detection branch, but that branch and the
fall-through both returned `False`, so it could never affect the result; it has
been removed rather than given invented discrimination).

What Eval 3 legitimately demonstrates is that out-of-scope input is routed to
the scope response and that `guardrail_input`'s injection markers fire. It is
not evidence about model professionalism or non-leakage. Its one practical
advantage over Evals 1 and 2: it needs no API key and no network, so **it can
be run today**.

---

## Project layout

```
SafeMigrationGate/
├── agent/
│   ├── __main__.py       # entry point: delegates to cli.main()
│   ├── cli.py            # REPL — all terminal I/O lives here
│   ├── graph.py          # LangGraph assembly — routing only, no analysis
│   ├── nodes.py          # every graph node
│   ├── rules.py          # SG001–SG015 over the sqlglot AST
│   ├── knowledge.py      # BM25 + direct lookup over the knowledge base
│   ├── knowledge/        # SG001.md … SG015.md
│   ├── db.py             # SQLAlchemy dry-run engine (never commits)
│   ├── rewrite.py        # rewrite contract and invariant checker
│   ├── state.py          # GateState TypedDict — the shared contract
│   ├── llm.py            # OpenRouter client + truststore injection
│   ├── tools.py          # tool layer: measure_impact, lookup_rules, search_knowledge
│   ├── prompts.py        # all LLM prompt strings
│   ├── sqlparse.py       # dialect detection, statement splitting
│   └── resources.py      # file I/O and resource paths (PyInstaller-safe)
├── data/
│   ├── seed.py           # builds data/demo.db
│   └── demo.db           # demo database — not tracked; create with python -m data.seed
├── evals/
│   ├── models.yaml       # candidate models and judge
│   ├── compare_models.py # Eval 1: model comparison
│   ├── cost_eval.py      # Eval 2: token cost by node
│   ├── scope_eval.py     # Eval 3: scope / refusal
│   ├── fixtures/         # SQL scripts + ground_truth.json
│   └── results/          # CSV output (gitignored)
├── tests/                # 309 tests, 2 skipped (network-only)
├── .env.example
├── manifest.json
├── requirements.txt
└── pytest.ini
```

---

## Design deviations from the spec

Three places where the implementation differs from the original design document,
with reasons:

**`FILL_PLACEHOLDER` uses a block comment, not `<-- FILL: condition -->`.**
The spec's `<-- FILL: condition -->` form parses as a `<` operator followed by
a line comment, which truncates the statement and causes `sqlglot` to raise.
That would have failed the validity check in `evaluate` on every rewrite.
The implementation uses `WHERE /* FILL: condition */ 1 = 0` — a block comment
that `sqlglot` accepts, combined with `1 = 0` to keep the placeholder inert
(it matches nothing until a human replaces it with a real condition).

**`blocked` is set in `static_analysis`, not `classify_risk`.**
The spec suggested the LLM classifier would set this flag. Letting a
probabilistic model decide whether a script is blocked means a bad day at the
model provider could let a `DROP DATABASE` through. The implementation computes
`blocked` deterministically in `static_analysis` from `BLOCKING_RULES`
(`{SG014, SG015}`) only. The classifier enriches the `classification` field but
cannot override `blocked`.

**`report` runs before `approve_write`.**
This means the second approval gate shows the operator the finished risk report
*before* asking whether to write the files. The operator can read the report and
then decide. If `report` ran after `approve_write`, the operator would be
approving files they had not yet seen.

---

## Running the tests

```sh
.venv\Scripts\python.exe -m pytest          # Windows
.venv/bin/python -m pytest                  # macOS / Linux
```

Expected: **309 passed, 2 skipped** (the 2 skipped tests are marked
`@pytest.mark.network` and require `openrouter.ai`).
