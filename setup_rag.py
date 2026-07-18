"""
One-time setup: upload all RAG documents to AnythingLLM workspace.
Run: python main.py setup-rag
Or:  python setup_rag.py
"""

import sys
from pathlib import Path

import httpx
import yaml

# Ensure UTF-8 output on Windows so checkmark/cross characters don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


RAG_DOCS = [
    "rag_docs/peds.txt",
    "rag_docs/vehicles.txt",
    "rag_docs/objects.txt",
    "rag_docs/weapons.txt",
    "rag_docs/weather.txt",
    "rag_docs/schema_guide.txt",
]


def upload_all(config: dict) -> None:
    llm_cfg = config["anythingllm"]
    base    = llm_cfg["base_url"].rstrip("/")
    slug    = llm_cfg["workspace_slug"]
    key     = llm_cfg["api_key"]
    headers = {"Authorization": f"Bearer {key}"}

    with httpx.Client(timeout=60.0) as client:
        # 1. Create workspace (returns existing if slug already used)
        print(f"Creating workspace '{slug}' ...")
        r = client.post(
            f"{base}/api/v1/workspace/new",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": slug},
        )
        if r.status_code not in (200, 201, 409):
            print(f"  ✗ Workspace creation failed: {r.status_code} {r.text}")
        else:
            print("  ✓ Workspace ready")

        # 2. Upload each document
        added_files: list[str] = []
        for doc_path in RAG_DOCS:
            p = Path(doc_path)
            if not p.exists():
                print(f"  ⚠  {doc_path} not found — skipping")
                continue
            print(f"Uploading {doc_path} ...")
            with open(p, "rb") as f:
                r = client.post(
                    f"{base}/api/v1/document/upload",
                    headers=headers,
                    files={"file": (p.name, f, "text/plain")},
                )
            if r.status_code in (200, 201):
                doc_name = r.json().get("documents", [{}])[0].get("location", p.name)
                added_files.append(doc_name)
                print(f"  ✓ {p.name}")
            else:
                print(f"  ✗ Failed ({r.status_code}): {r.text[:120]}")

        # 3. Embed documents into workspace
        if added_files:
            print(f"Embedding {len(added_files)} documents into workspace ...")
            r = client.post(
                f"{base}/api/v1/workspace/{slug}/update-embeddings",
                headers={**headers, "Content-Type": "application/json"},
                json={"adds": added_files, "deletes": []},
            )
            if r.status_code in (200, 201):
                print("  ✓ Embeddings updated")
            else:
                print(f"  ✗ Embedding failed ({r.status_code}): {r.text[:120]}")

    print("\nSetup complete. Run: python main.py generate \"Your mission idea\"")


if __name__ == "__main__":
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    upload_all(cfg)
