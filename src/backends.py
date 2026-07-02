"""
Backend selection + health checks.

Two chat backends are supported, both OpenAI-compatible:
  - LM Studio  (default, http://localhost:1234/v1)
  - Ollama     (http://localhost:11434/v1)

Because both speak the OpenAI chat API, the existing LMStudioClient works
against either one — only the base_url/model differ. `make_chat_client` reads
`lm_studio.backend` and selects the right config block.

Health checks back the UI's status lights and "Test Connection" buttons. They
never raise; failures are classified through src.errors.classify() so the UI
gets an actionable fix instead of a stack trace.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .anythingllm_client import AnythingLLMClient
from .lm_studio_client import LMStudioClient
from .errors import classify


# ── Config helpers ──────────────────────────────────────────────────────────────

def active_chat_cfg(config: dict) -> dict:
    """
    Return the config block for the selected chat backend, merging shared keys
    (temperature/max_tokens/timeout) from the lm_studio block so an Ollama block
    only needs base_url + model.
    """
    lm_cfg = config.get("lm_studio", {})
    backend = (lm_cfg.get("backend") or "lm_studio").lower()

    if backend == "ollama":
        ollama = config.get("ollama", {})
        return {
            "backend":     "ollama",
            "base_url":    ollama.get("base_url", "http://localhost:11434/v1"),
            "model":       ollama.get("model", "qwen2.5:7b"),
            "temperature": ollama.get("temperature", lm_cfg.get("temperature", 0.7)),
            "max_tokens":  ollama.get("max_tokens", lm_cfg.get("max_tokens", 8192)),
            "timeout":     ollama.get("timeout", lm_cfg.get("timeout", 600.0)),
            "seed":        ollama.get("seed", lm_cfg.get("seed")),
            "concept_temperature": lm_cfg.get("concept_temperature", 0.85),
            "schema_temperature":  lm_cfg.get("schema_temperature", 0.2),
        }

    # default: lm_studio
    return {
        "backend":     "lm_studio",
        "base_url":    lm_cfg.get("base_url", "http://localhost:1234/v1"),
        "model":       lm_cfg.get("model", "local-model"),
        "temperature": lm_cfg.get("temperature", 0.7),
        "max_tokens":  lm_cfg.get("max_tokens", 8192),
        "timeout":     lm_cfg.get("timeout", 600.0),
        "seed":        lm_cfg.get("seed"),
        "concept_temperature": lm_cfg.get("concept_temperature", 0.85),
        "schema_temperature":  lm_cfg.get("schema_temperature", 0.2),
    }


def make_chat_client(config: dict) -> LMStudioClient:
    """Build the chat client for whichever backend is selected."""
    cfg = active_chat_cfg(config)
    return LMStudioClient(
        base_url    = cfg["base_url"],
        model       = cfg["model"],
        temperature = cfg["temperature"],
        max_tokens  = cfg["max_tokens"],
        timeout     = cfg["timeout"],
        seed        = cfg.get("seed"),
    )


def make_rag_client(config: dict):
    """
    Build the resolver RAG client.

    rag:
      backend: local        -> RagClient  (local ChromaDB, no API key)
      backend: anythingllm  -> AnythingLLMClient (legacy / remote)
      (absent or basic_mode)-> None  (resolver falls back to local lookups only)
    """
    if config.get("basic_mode", False):
        return None

    rag_cfg = config.get("rag", {})
    backend = (rag_cfg.get("backend") or "anythingllm").lower()

    if backend == "local":
        from rag.query import RagClient  # lazy: only paid when local backend chosen
        idx_dir = rag_cfg.get("index_dir")   # optional override
        return RagClient(index_dir=idx_dir)

    # anythingllm (default, backward-compatible)
    llm_cfg = config.get("anythingllm", {})
    base_url = llm_cfg.get("base_url", "")
    if not base_url:
        return None   # not configured — resolver uses local lookups
    return AnythingLLMClient(
        base_url       = base_url,
        api_key        = llm_cfg.get("api_key", ""),
        workspace_slug = llm_cfg.get("workspace_slug", ""),
    )


def rag_enabled(config: dict) -> bool:
    """RAG is on unless Basic Mode is explicitly set."""
    return not bool(config.get("basic_mode", False))


# ── Health checks ─────────────────────────────────────────────────────────────

def check_chat(config: dict) -> dict[str, Any]:
    """
    GET {base_url}/models — works for both LM Studio and Ollama.
    Returns {ok, latency_ms, backend, base_url, models, [error]}.
    """
    cfg = active_chat_cfg(config)
    base = cfg["base_url"].rstrip("/")
    url = f"{base}/models"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url)
            r.raise_for_status()
        data = r.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        latency = round((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {
            "ok": True,
            "latency_ms": latency,
            "backend": cfg["backend"],
            "base_url": base,
            "models": models,
        }
        if not models:
            result["ok"] = False
            result["error"] = classify(
                "backend reachable but no model loaded / empty model list",
                target="chat",
            )
        return result
    except Exception as exc:  # noqa: BLE001 — health check must never raise
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "backend": cfg["backend"],
            "base_url": base,
            "models": [],
            "error": classify(exc, target="chat"),
        }


def _extract_documents(workspace_payload: dict) -> list:
    """
    AnythingLLM's GET /api/v1/workspace/{slug} response shape varies by
    version. We've seen both:
        {"workspace": [{"documents": [...]}]}
        {"workspace": {"documents": [...]}}
    Walk both safely and return the documents list (or []).
    """
    ws = workspace_payload.get("workspace", workspace_payload)
    if isinstance(ws, list):
        ws = ws[0] if ws else {}
    if isinstance(ws, dict):
        docs = ws.get("documents") or []
        if isinstance(docs, list):
            return docs
    return []


def check_rag(config: dict) -> dict[str, Any]:
    """
    Check the AnythingLLM server and both workspaces (gta + unity).
    Returns:
        {
          ok:          bool,                # server reachable AND every required workspace exists
          basic_mode:  bool,                # when True, RAG is skipped (always ok)
          base_url:    str,
          workspaces:  ["llm_gta", ...],    # all slugs returned by the server
          gta:   {slug, present, [error]},
          unity: {slug, present, [error]},
          [error]:     {...catalog entry, server-level error},
        }
    Designed so the UI can show one combined RAG light (overall ok) plus two
    sub-lights (gta + unity).
    """
    llm_cfg = config.get("anythingllm", {})
    base = llm_cfg.get("base_url", "http://localhost:3001").rstrip("/")
    gta_slug = llm_cfg.get("workspace_slug", "llm_gta")
    unity_slug = llm_cfg.get("unity_workspace_slug", "llm_unity")
    key = llm_cfg.get("api_key", "")

    if not rag_enabled(config):
        return {
            "ok": True,
            "basic_mode": True,
            "base_url": base,
            "workspaces": [],
            "gta":   {"slug": gta_slug,   "present": False},
            "unity": {"slug": unity_slug, "present": False},
            "detail": "Basic Mode is on — RAG is skipped; local lookups are used.",
        }

    url = f"{base}/api/v1/workspaces"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
        data = r.json()
        workspaces = data.get("workspaces", data) if isinstance(data, dict) else data
        slugs = []
        if isinstance(workspaces, list):
            slugs = [w.get("slug") for w in workspaces if isinstance(w, dict)]
        gta_present = gta_slug in slugs
        unity_present = unity_slug in slugs

        def _sub(slug: str, present: bool) -> dict:
            d: dict[str, Any] = {"slug": slug, "present": present}
            if present:
                # Best-effort document count; never fails the health check.
                try:
                    client = AnythingLLMClient(base_url=base, api_key=key, workspace_slug=slug)
                    details = client.workspace_details(slug)
                    docs = _extract_documents(details)
                    d["doc_count"] = len(docs)
                except Exception:  # noqa: BLE001
                    d["doc_count"] = 0
            else:
                d["error"] = classify(
                    f"workspace '{slug}' not found in AnythingLLM (404)",
                    target="rag",
                )
            return d

        return {
            "ok": gta_present and unity_present,
            "basic_mode": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "base_url": base,
            "workspaces": slugs,
            "gta":   _sub(gta_slug, gta_present),
            "unity": _sub(unity_slug, unity_present),
        }
    except Exception as exc:  # noqa: BLE001
        err = classify(f"anythingllm: {exc}", target="rag")
        return {
            "ok": False,
            "basic_mode": False,
            "base_url": base,
            "workspaces": [],
            "gta":   {"slug": gta_slug,   "present": False, "error": err},
            "unity": {"slug": unity_slug, "present": False, "error": err},
            "error": err,
        }
