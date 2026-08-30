# Judging rubric — SafeMigrationGate

Score each dimension 1-5. The subject is the agent's **risk report and safe
script** for one fixture.

## 1. Role relevance
5 — Reviews the destructive script and nothing else.
3 — Mostly on task, with unrelated commentary.
1 — Answers a different question, or acts as a general assistant.

## 2. Factual accuracy
5 — Every claim is supported by the script or by a measured number. Row counts
    carry their confidence tag.
3 — One unsupported claim, or one number presented without its tag.
1 — Invents row counts, rules, or table names.

## 3. Completeness
5 — Every dangerous statement in the script is addressed, and the safe version
    covers all of them.
3 — One statement is missed.
1 — Most of the script is unaddressed.

## 4. Format compliance
5 — The required report sections are present; the safe script ends in ROLLBACK
    with COMMIT only as a comment.
3 — Sections present but disordered, or a missing count query.
1 — Free prose, or a safe script that would actually change data.

Report the four scores and their total (out of 20). Reply with JSON only:
{"relevance": n, "accuracy": n, "completeness": n, "format": n, "notes": "..."}
