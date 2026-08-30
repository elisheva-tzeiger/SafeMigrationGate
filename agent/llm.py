"""The OpenRouter client, plus the plumbing every LLM node shares.

Nodes call invoke_json / invoke_text rather than with_structured_output. That is
deliberate: FakeListChatModel does not implement structured output (and the test
suite is built on it), and many of the free OpenRouter models that Eval 1 must
compare do not support tool calling or JSON-schema mode at all.
"""

from __future__ import annotations

import json
import os
import re

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-chat-v3-0324:free"

_CONNECTION_PASSWORD = re.compile(r"://([^:/@\s]+):([^@\s]+)@")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")

_tls_installed = False


class MissingCredentials(Exception):
    """No API key. The message names the environment variable to set."""


def install_tls_trust() -> None:
    """Trust the operating system's certificate store rather than certifi's.

    Corporate networks terminate TLS at an inspecting proxy whose root is
    installed in the OS store but absent from certifi, so without this every
    HTTPS call fails with 'CERTIFICATE_VERIFY_FAILED: Missing Authority Key
    Identifier'. Measured on this machine against palo-decrypt.scp.co.il.
    """
    global _tls_installed
    if _tls_installed:
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # pragma: no cover - truststore is optional at runtime
        pass
    _tls_installed = True


def scrub(text: str) -> str:
    """Mask secrets. Every string that reaches a log, the terminal, or LangSmith
    goes through this first."""
    text = _CONNECTION_PASSWORD.sub(r"://\1:***@", text)
    return _API_KEY.sub("sk-***", text)


def make_llm(model: str | None = None, temperature: float = 0.0) -> BaseChatModel:
    """An OpenRouter-backed chat model. Raises MissingCredentials naming the
    variable, rather than failing later with an opaque 401."""
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise MissingCredentials(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    install_tls_trust()
    return ChatOpenAI(
        model=model or os.environ.get("SMG_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        temperature=temperature,
        timeout=120,
        max_retries=3,
    )


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in prose or fences however they like; this tolerates that
    without pretending prose is JSON.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text

    start = candidate.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model response: {text[:200]!r}")

    depth, in_string, escaped = 0, False, False
    for offset, char in enumerate(candidate[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(candidate[start:offset + 1])

    raise ValueError(f"unbalanced JSON in model response: {text[:200]!r}")


def _invoke(llm: BaseChatModel, system: str, user: str, run_name: str) -> str:
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    # run_name makes the LangSmith trace readable: one span per graph node.
    response = llm.with_config(run_name=run_name).invoke(messages)
    return response.content if isinstance(response.content, str) else str(response.content)


def invoke_text(llm: BaseChatModel, system: str, user: str, *, run_name: str) -> str:
    return _invoke(llm, system, user, run_name).strip()


def invoke_json(llm: BaseChatModel, system: str, user: str, *, run_name: str) -> dict:
    return extract_json(_invoke(llm, system, user, run_name))
