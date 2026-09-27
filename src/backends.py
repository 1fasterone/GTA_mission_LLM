"""
Backend selection + health checks.

Three chat backends are supported, all OpenAI-compatible:
  - LM Studio  (default, http://localhost:1234/v1, local, no auth)
  - Ollama     (http://localhost:11434/v1, local, no auth)
  - OpenRouter (https://openrouter.ai/api/v1, cloud, needs an API key)

Because all three speak the OpenAI chat API, the existing LMStudioClient
works against any of them — base_url/model/api_key differ, the request/
response contract does not. `make_chat_client` reads `lm_studio.backend` and
selects the right config block.

OpenRouter, added 2026-08-28 after a real side-by-side (see
docs/model-pipeline-architecture.md context / session history): free-tier
`:free` models sidestep the local 8GB card's VRAM contention with ComfyUI/
Stable Audio 3 that caused several crashes this session, at the cost of
needing network access and an OpenRouter account. Verified live against this
repo's actual Step 2/3 prompts: nvidia/nemotron-3-super-120b-a12b:free
produced a valid schema on the first attempt (17s concept + 40s schema fill).
Free-tier availability can be inconsistent per-model (a specific upstream
provider can be temporarily saturated, returning HTTP 429) — this is an
OpenRouter/upstream-provider condition, not something retrying in this repo
can fix; LMStudioClient's HTTPStatusError handling surfaces the real
provider message instead of swallowing it.

Health checks back the UI's status lights and "Test Connection" buttons. They
never raise; failures are classified through src.errors.classify() so the UI
gets an actionable fix instead of a stack trace.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .lm_studio_client import LMStudioClient
from .errors import classify

# Env var wins over config.yaml — an API key should not need to live in a
# git-tracked file. Same two-name fallback as src/sprites/openrouter_backend.py.
_OPENROUTER_ENV_KEY_VARS = ("OPENROUTER_API", "OPENROUTER_API_KEY")


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
            "reasoning_effort": ollama.get("reasoning_effort",
                                           lm_cfg.get("reasoning_effort")),
        }

    if backend == "openrouter":
        openrouter = config.get("openrouter", {})
        api_key = ""
        for var in _OPENROUTER_ENV_KEY_VARS:
            if os.environ.get(var):
                api_key = os.environ[var]
                break
        if not api_key:
            api_key = str(openrouter.get("api_key", "") or "")
        return {
            "backend":     "openrouter",
            "base_url":    openrouter.get("base_url", "https://openrouter.ai/api/v1"),
            "model":       openrouter.get("model", "nvidia/nemotron-3-super-120b-a12b:free"),
            "api_key":     api_key,
            "temperature": openrouter.get("temperature", lm_cfg.get("temperature", 0.7)),
            "max_tokens":  openrouter.get("max_tokens", lm_cfg.get("max_tokens", 8192)),
            # A cloud call has no local-VRAM ceiling to hit but real network +
            # provider-queue latency to absorb — longer default than the local
            # backends' timeout, not shorter.
            "timeout":     openrouter.get("timeout", 280.0),
            "seed":        openrouter.get("seed", lm_cfg.get("seed")),
            "concept_temperature": lm_cfg.get("concept_temperature", 0.85),
            "schema_temperature":  lm_cfg.get("schema_temperature", 0.2),
            "reasoning_effort": openrouter.get("reasoning_effort",
                                               lm_cfg.get("reasoning_effort")),
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
        "reasoning_effort": lm_cfg.get("reasoning_effort"),
    }


def make_chat_client(config: dict) -> LMStudioClient:
    """Build the chat client for whichever backend is selected."""
    cfg = active_chat_cfg(config)
    if cfg["backend"] == "openrouter" and not cfg.get("api_key"):
        raise RuntimeError(
            f"lm_studio.backend is 'openrouter' but no API key was found in "
            f"env {_OPENROUTER_ENV_KEY_VARS} or config.openrouter.api_key. "
            f"Abstaining rather than silently falling back to a different "
            f"backend — set the key or switch lm_studio.backend back."
        )
    return LMStudioClient(
        base_url    = cfg["base_url"],
        model       = cfg["model"],
        temperature = cfg["temperature"],
        max_tokens  = cfg["max_tokens"],
        timeout     = cfg["timeout"],
        seed        = cfg.get("seed"),
        reasoning_effort = cfg.get("reasoning_effort"),
        api_key     = cfg.get("api_key"),
        backend     = cfg["backend"],
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
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url, headers=headers)
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
