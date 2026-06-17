"""
Pre-built AnythingLLM workspace bootstrap.

A one-call setup that creates both workspaces and uploads their RAG docs:
  - 'llm_gta'    ← rag_docs/*.txt          (GTA5 model lookup docs)
  - 'llm_unity'  ← rag_docs/unity/*.txt    (Unity package lookup docs)

Used by the web UI's "Create pre-built workspace" buttons and the CLI command
`python main.py setup-workspace`.

`bootstrap()` returns a structured result (never raises) so the web layer can
report progress and errors cleanly.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import httpx

import setup_rag


# Default doc bundles per workspace (overridable for tests).
GTA_DOCS = [
    "rag_docs/peds.txt",
    "rag_docs/vehicles.txt",
    "rag_docs/objects.txt",
    "rag_docs/weapons.txt",
    "rag_docs/weather.txt",
    "rag_docs/schema_guide.txt",
]
UNITY_DOCS = sorted(
    str(p) for p in Path("rag_docs/unity").glob("*.txt")
) if Path("rag_docs/unity").exists() else []


def ensure_workspace(config: dict, slug: str) -> dict:
    """Create the workspace if absent and set query mode. Returns a status dict."""
    llm_cfg = config["anythingllm"]
    base = llm_cfg["base_url"].rstrip("/")
    key = llm_cfg.get("api_key", "")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{base}/api/v1/workspace/new",
            headers=headers,
            json={"name": slug},
        )
        created = r.status_code in (200, 201)
        existed = r.status_code == 409
        if not (created or existed):
            return {
                "ok": False,
                "step": "create_workspace",
                "slug": slug,
                "detail": f"{r.status_code} {r.text[:200]}",
            }

        # Set query mode so the workspace answers from documents, not free chat.
        client.post(
            f"{base}/api/v1/workspace/{slug}/update",
            headers=headers,
            json={"chatMode": "query"},
        )

    return {"ok": True, "slug": slug, "created": created, "existed": existed}


def _upload_one(config: dict, slug: str, doc_paths: list[str]) -> tuple[bool, str]:
    """
    Upload one bundle of docs into one workspace, returning (ok, captured_log).
    Reuses setup_rag.upload_all by temporarily overriding its module-level
    constants — cleaner than duplicating its retry/embedding logic here.
    """
    cfg_copy = {**config, "anythingllm": {**config["anythingllm"], "workspace_slug": slug}}
    saved_docs = setup_rag.RAG_DOCS
    setup_rag.RAG_DOCS = doc_paths
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            setup_rag.upload_all(cfg_copy)
        return True, buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        return False, buf.getvalue() + f"\nFAILED: {type(exc).__name__}: {exc}"
    finally:
        setup_rag.RAG_DOCS = saved_docs


def bootstrap(config: dict, *, target: str = "both") -> dict:
    """
    Bootstrap one or both workspaces. `target`: "both" | "gta" | "unity".
    Never raises; returns a structured per-workspace result.
    """
    llm_cfg = config["anythingllm"]
    gta_slug = llm_cfg.get("workspace_slug", "llm_gta")
    unity_slug = llm_cfg.get("unity_workspace_slug", "llm_unity")

    out: dict = {"ok": True, "results": {}}
    plan: list[tuple[str, str, list[str]]] = []
    if target in ("both", "gta"):
        plan.append(("gta", gta_slug, GTA_DOCS))
    if target in ("both", "unity"):
        plan.append(("unity", unity_slug, UNITY_DOCS))

    for kind, slug, docs in plan:
        ws = ensure_workspace(config, slug)
        if not ws.get("ok"):
            out["ok"] = False
            out["results"][kind] = {"phase": "workspace", **ws}
            continue
        ok, log = _upload_one(config, slug, docs)
        out["ok"] = out["ok"] and ok
        out["results"][kind] = {
            "ok": ok,
            "workspace": ws,
            "slug": slug,
            "log": log,
            "doc_count": len(docs),
        }

    return out


if __name__ == "__main__":
    import yaml

    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    result = bootstrap(cfg)
    for kind, r in result["results"].items():
        print(f"\n=== {kind} workspace: {r.get('slug')} ===")
        print(r.get("log", ""))
    print("\nOK" if result["ok"] else "\nFAILED")
