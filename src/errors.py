"""
Common Errors & Fixes catalog.

Maps known failure signatures (exception types + message fragments) to a
user-facing entry with an actionable fix. Used by:
  - the web UI "Errors & Fixes" reference page (the full CATALOG),
  - health checks and the generation stream, to turn a raw exception into a
    helpful message via `classify()`.

Each entry is intentionally backend/UI-agnostic plain data so it can be served
as JSON or rendered in a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ErrorEntry:
    id: str
    title: str
    symptom: str
    likely_cause: str
    fix_steps: list[str]
    # Substrings (lowercased) that, if present in an exception/message, match this entry.
    match: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ── Catalog ────────────────────────────────────────────────────────────────────
# Order matters: classify() returns the first entry whose `match` fragments hit.

CATALOG: list[ErrorEntry] = [
    ErrorEntry(
        id="chat_connection_refused",
        title="Chat backend isn't running",
        symptom="Connection refused / could not connect to the chat backend "
                "(LM Studio or Ollama).",
        likely_cause="The local LLM server isn't started, or the base URL/port "
                     "in Settings is wrong.",
        fix_steps=[
            "LM Studio: open the app → Developer tab → Start Server "
            "(default http://localhost:1234/v1).",
            "Ollama: run `ollama serve` (default http://localhost:11434/v1); "
            "the OpenAI-compatible API lives under /v1.",
            "Confirm the base URL in Settings matches the running server's "
            "port, then click Test Connection.",
        ],
        match=["connecterror", "connection refused", "connect call failed",
               "max retries", "failed to establish", "all connection attempts failed"],
    ),
    ErrorEntry(
        id="chat_no_model_loaded",
        title="No model loaded in the chat backend",
        symptom="The backend responds but returns an empty message, or /models "
                "lists nothing.",
        likely_cause="LM Studio has no model loaded, or the configured Ollama "
                     "model isn't pulled.",
        fix_steps=[
            "LM Studio: load a model in the Chat/Developer tab before generating.",
            "Ollama: pull the model first, e.g. `ollama pull qwen2.5:7b`, and "
            "make sure the `model` field in Settings matches the pulled tag.",
            "Click Test Connection — the resolved model list should be non-empty.",
        ],
        match=["empty response", "no model", "model not found",
               "model '", "returned an empty"],
    ),
    ErrorEntry(
        id="chat_timeout",
        title="Chat request timed out",
        symptom="The generation hangs and then fails with a timeout.",
        likely_cause="Local models can be slow; the request exceeded the "
                     "configured timeout.",
        fix_steps=[
            "Increase `timeout` (seconds) for the active backend in Settings.",
            "Use a smaller/quantized model, or reduce `max_tokens`.",
            "Close other GPU/CPU-heavy apps so the model can respond faster.",
        ],
        match=["timeout", "timed out", "readtimeout", "connecttimeout"],
    ),
    ErrorEntry(
        id="json_truncated",
        title="LLM response was truncated (JSON incomplete)",
        symptom="Pipeline fails with 'JSON truncated and could not be repaired' "
                "or a parse error mid-schema.",
        likely_cause="The model stopped before finishing the JSON because "
                     "max_tokens was too low.",
        fix_steps=[
            "Increase `max_tokens` in Settings (e.g. 8192 → 12288).",
            "Try again — schema generation runs at low temperature and is "
            "usually deterministic.",
            "If it keeps truncating, switch to a model with a larger context "
            "window.",
        ],
        match=["json truncated", "could not be repaired", "no json object",
               "json parse error"],
    ),
    ErrorEntry(
        id="rag_unauthorized",
        title="AnythingLLM rejected the API key",
        symptom="RAG calls fail with 401/403 Unauthorized.",
        likely_cause="The AnythingLLM API key is missing or incorrect.",
        fix_steps=[
            "In AnythingLLM: Settings → API Keys → generate a key.",
            "Paste it into the AnythingLLM API key field in Settings and save.",
            "Click Test Connection on RAG to confirm.",
            "Or enable Basic Mode to skip RAG entirely and use local lookups.",
        ],
        match=["401", "403", "unauthorized", "forbidden", "invalid api key"],
    ),
    ErrorEntry(
        id="rag_workspace_missing",
        title="AnythingLLM workspace not found",
        symptom="RAG calls fail with 404, or the workspace light stays red even "
                "though the server is up.",
        likely_cause="The configured workspace slug doesn't exist yet.",
        fix_steps=[
            "Click 'Create pre-built workspace' in Settings to bootstrap the "
            "'llm_gta' workspace and upload the asset documents.",
            "Or create the workspace manually in AnythingLLM and set its slug "
            "in Settings.",
            "Or enable Basic Mode to skip RAG entirely.",
        ],
        match=["404", "not found", "workspace", "no workspace"],
    ),
    ErrorEntry(
        id="rag_connection_refused",
        title="AnythingLLM isn't running",
        symptom="RAG status is red with a connection error.",
        likely_cause="The AnythingLLM server isn't started, or its base URL is "
                     "wrong.",
        fix_steps=[
            "Start AnythingLLM (default http://localhost:3001).",
            "Confirm the AnythingLLM base URL in Settings.",
            "RAG is optional — enable Basic Mode to generate using local lookup "
            "tables only.",
        ],
        match=["anythingllm", "3001"],
    ),
]


_FALLBACK = ErrorEntry(
    id="unknown",
    title="Unexpected error",
    symptom="Something failed that isn't in the known-issues catalog.",
    likely_cause="See the raw error detail for specifics.",
    fix_steps=[
        "Check that both the chat backend and (if used) AnythingLLM are running.",
        "Re-run with the CLI `--debug` flag for full request/response logs.",
        "If RAG is the problem, enable Basic Mode to isolate the chat backend.",
    ],
    match=[],
)


def classify(error: BaseException | str, *, target: Optional[str] = None) -> dict:
    """
    Map an exception (or message string) to a catalog entry dict, augmented with
    the raw `detail`. `target` ('chat'|'rag') biases ambiguous matches so a chat
    failure doesn't surface a RAG-specific fix and vice-versa.
    """
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = str(error)

    haystack = detail.lower()

    for entry in CATALOG:
        if target == "chat" and entry.id.startswith("rag_"):
            continue
        if target == "rag" and entry.id.startswith("chat_"):
            continue
        if any(fragment in haystack for fragment in entry.match):
            result = entry.to_dict()
            result["detail"] = detail
            return result

    result = _FALLBACK.to_dict()
    result["detail"] = detail
    return result


def catalog_as_dicts() -> list[dict]:
    """Full catalog for the Errors & Fixes reference page."""
    return [e.to_dict() for e in CATALOG]
