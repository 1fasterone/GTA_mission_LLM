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
    Build the resolver RAG client (local ChromaDB index, no API key).

    (absent or basic_mode) -> None  (resolver falls back to local lookups only)
    """
    if config.get("basic_mode", False):
        return None

    rag_cfg = config.get("rag", {})
    from rag.query import RagClient  # lazy: only paid when RAG is actually used
    idx_dir = rag_cfg.get("index_dir")   # optional override
    return RagClient(index_dir=idx_dir)


def rag_enabled(config: dict) -> bool:
    """RAG is on unless Basic Mode is explicitly set."""
    return not bool(config.get("basic_mode", False))


def _default_index_dir() -> str:
    from rag.query import INDEX_DIR  # lazy: avoid importing chromadb/fastembed eagerly
    return str(INDEX_DIR)


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


def check_rag(config: dict) -> dict[str, Any]:
    """
    Check the local ChromaDB RAG index (gta5 + unity domains).
    Returns:
        {
          ok:          bool,                # index built AND both domains populated
          basic_mode:  bool,                # when True, RAG is skipped (always ok)
          index_dir:   str,
          gta:   {domain, present, [doc_count]},
          unity: {domain, present, [doc_count]},
          [error]:     {...catalog entry, index-level error},
        }
    Designed so the UI can show one combined RAG light (overall ok) plus two
    sub-lights (gta + unity).
    """
    rag_cfg = config.get("rag", {})
    idx_dir = rag_cfg.get("index_dir") or _default_index_dir()

    if not rag_enabled(config):
        return {
            "ok": True,
            "basic_mode": True,
            "index_dir": idx_dir,
            "gta":   {"domain": "gta5",  "present": False},
            "unity": {"domain": "unity", "present": False},
            "detail": "Basic Mode is on — RAG is skipped; local lookups are used.",
        }

    started = time.perf_counter()
    try:
        from rag.query import RagClient
        rag = RagClient(index_dir=rag_cfg.get("index_dir"))

        def _sub(domain: str) -> dict:
            ready = rag.is_ready(domain=domain)
            d: dict[str, Any] = {"domain": domain, "present": ready}
            if not ready:
                d["error"] = classify(
                    f"RAG index for domain '{domain}' is empty or not built",
                    target="rag",
                )
            return d

        gta_sub = _sub("gta5")
        unity_sub = _sub("unity")
        return {
            "ok": gta_sub["present"] and unity_sub["present"],
            "basic_mode": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "index_dir": idx_dir,
            "gta":   gta_sub,
            "unity": unity_sub,
        }
    except Exception as exc:  # noqa: BLE001
        err = classify(f"rag index: {exc}", target="rag")
        return {
            "ok": False,
            "basic_mode": False,
            "index_dir": idx_dir,
            "gta":   {"domain": "gta5",  "present": False, "error": err},
            "unity": {"domain": "unity", "present": False, "error": err},
            "error": err,
        }
