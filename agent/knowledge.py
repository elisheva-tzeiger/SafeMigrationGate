"""Retrieval over the safety-rule corpus.

Retrieval is BM25 plus direct lookup by rule_id -- not embeddings. Two query
paths exist, and they are not equally common:

  * classify_risk already knows the rule_ids from `findings`, so its retrieval is
    a dictionary lookup. This is most of the traffic.
  * a free-text question in the CLI is the only path where ranking matters.

Embeddings were considered and rejected: OpenRouter serves no embedding endpoint
(a second provider would contradict the project's stack requirement), and a local
embedding model inflates the one-file executable. Over a 15-document corpus whose
vocabulary is fixed technical terminology -- DROP, TRUNCATE, CASCADE -- BM25's
weakness on synonyms barely applies. This is documented in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from rank_bm25 import BM25Okapi

from agent.resources import resource_path
from agent.state import RuleRef

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TOKEN = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True)
class Document:
    rule_id: str
    title: str
    source_doc: str
    body: str

    def as_ref(self) -> RuleRef:
        return RuleRef(rule_id=self.rule_id, title=self.title,
                       source_doc=self.source_doc)


class KnowledgeBase:
    def __init__(self, documents: list[Document]) -> None:
        self._documents = documents
        self._by_id = {d.rule_id: d for d in documents}
        self._index = BM25Okapi(
            [_tokenize(f"{d.rule_id} {d.title} {d.body}") for d in documents]
        )

    # -- direct lookup: the common path -------------------------------------
    def doc_count(self) -> int:
        return len(self._documents)

    def get(self, rule_id: str) -> RuleRef | None:
        document = self._by_id.get(rule_id.strip().upper())
        return document.as_ref() if document else None

    def body(self, rule_id: str) -> str | None:
        document = self._by_id.get(rule_id.strip().upper())
        return document.body if document else None

    def context_for(self, rule_ids: list[str]) -> str:
        """The RAG context handed to classify_risk. Unknown ids are skipped
        silently -- a missing document must not stop the analysis."""
        chunks = []
        for rule_id in rule_ids:
            document = self._by_id.get(rule_id.strip().upper())
            if document is not None:
                chunks.append(f"=== {document.rule_id}: {document.title} ===\n"
                              f"{document.body}")
        return "\n\n".join(chunks)

    # -- ranked search: the free-text path -----------------------------------
    def search(self, query: str, k: int = 3) -> list[RuleRef]:
        scores = self._index.get_scores(_tokenize(query))
        ranked = sorted(range(len(self._documents)),
                        key=lambda i: scores[i], reverse=True)
        return [self._documents[i].as_ref() for i in ranked[:k] if scores[i] > 0]


def _parse(path) -> Document:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError(f"{path.name} has no YAML front matter")
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return Document(
        rule_id=meta["rule_id"].upper(),
        title=meta["title"],
        source_doc=path.name,
        body=raw[match.end():].strip(),
    )


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    directory = resource_path("agent/knowledge")
    documents = [_parse(p) for p in sorted(directory.glob("SG*.md"))]
    if not documents:
        raise FileNotFoundError(f"no knowledge documents found under {directory}")
    return KnowledgeBase(documents)
