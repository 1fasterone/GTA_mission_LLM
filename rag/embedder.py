"""
rag/embedder.py — one loader for the fastembed ONNX model, shared by
rag.indexer (build time) and rag.query (resolve time).

Why this module exists — two failure modes, one permanent breakage:

1.  fastembed defaults its cache to ``%TEMP%/fastembed_cache``
    (fastembed.common.utils.define_cache_dir).  Windows Storage Sense / Disk
    Cleanup deletes files under %TEMP%, which strips ``snapshots/<rev>/*``
    while leaving ``refs/`` and ``blobs/`` behind.

2.  fastembed cannot recover from that on its own.  ModelManagement.
    download_model() always tries ``local_files_only=True`` first, and
    huggingface_hub returns the snapshot directory because ``refs/main`` still
    exists — so the real download is never re-attempted and *every* run dies
    with the same error:

        NO_SUCHFILE : Load model ...\\model_optimized.onnx failed. File doesn't exist

    (fastembed logs "Local file sizes do not match the metadata" and carries on
    regardless.)  Deleting the half-cached ``models--*`` directory is the only
    way out.

So: keep the weights outside %TEMP%, verify the cache before use, and purge +
re-download once if it is incomplete.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# fastembed writes <cache>/models--<hf-repo with / -> -->/, e.g.
# BAAI/bge-small-en-v1.5 -> models--qdrant--bge-small-en-v1.5-onnx-q.  The HF
# repo id is not derivable from the model name, but it always contains the
# model's short name — enough to target a purge without touching other models.
_METADATA_FILE = "files_metadata.json"


# ── cache location ────────────────────────────────────────────────────────────

def cache_dir() -> Path:
    """Stable on-disk home for the ONNX weights — deliberately never %TEMP%."""
    env = os.getenv("FASTEMBED_CACHE_PATH")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "fastembed_cache"
    return Path(os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "fastembed"


def _model_dirs(cache: Path, model_name: str) -> list[Path]:
    """Cached copies of one model — `models--*` dirs carrying its short name."""
    slug = model_name.split("/")[-1].lower()
    if not cache.is_dir():
        return []
    return sorted(
        p for p in cache.iterdir()
        if p.is_dir() and p.name.startswith("models--") and slug in p.name.lower()
    )


# ── cache verification ────────────────────────────────────────────────────────

def is_incomplete(model_dir: Path) -> bool:
    """
    True when a cached model is present but unusable.

    fastembed records every downloaded file in files_metadata.json, so a
    missing or short file there is exactly the Storage-Sense footprint.  With
    no manifest, fall back to the one file that has to be on disk: the .onnx
    graph.
    """
    meta_path = model_dir / _METADATA_FILE
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        for rel, info in meta.items():
            # Manifest keys use the writing platform's separator.
            target = model_dir / Path(str(rel).replace("\\", "/"))
            if not target.is_file():
                return True
            size = info.get("size") if isinstance(info, dict) else None
            if isinstance(size, int) and target.stat().st_size != size:
                return True
        return False
    return not any(model_dir.rglob("*.onnx"))


def purge(model_name: str = EMBED_MODEL, cache: Path | None = None) -> list[Path]:
    """Delete every cached copy of `model_name`. Returns what was removed."""
    cache = cache or cache_dir()
    removed = []
    for d in _model_dirs(cache, model_name):
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


def status(model_name: str = EMBED_MODEL, cache: Path | None = None) -> dict[str, Any]:
    """Cache health without loading anything — used by `main.py rag-status`."""
    cache = cache or cache_dir()
    dirs  = _model_dirs(cache, model_name)
    return {
        "model":     model_name,
        "cache_dir": str(cache),
        "cached":    bool(dirs),
        "complete":  bool(dirs) and not any(is_incomplete(d) for d in dirs),
    }


# ── loader ────────────────────────────────────────────────────────────────────

def load_embedder(model_name: str = EMBED_MODEL,
                  cache: Path | None = None,
                  repair: bool = True) -> Any:
    """
    Return a fastembed TextEmbedding, healing a broken cache once if needed.

    repair=False loads exactly what is on disk and lets any error propagate —
    for tests and for callers that must not touch the network.
    """
    from fastembed import TextEmbedding

    cache = cache or cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    if repair:
        for d in _model_dirs(cache, model_name):
            if is_incomplete(d):
                print(f"[embedder] cached model at {d} is incomplete — re-downloading")
                shutil.rmtree(d, ignore_errors=True)

    try:
        return TextEmbedding(model_name, cache_dir=str(cache))
    except Exception as first:
        if not repair:
            raise
        purged = purge(model_name, cache)
        if not purged:
            raise RuntimeError(_failure_text(model_name, cache, first)) from first
        print(f"[embedder] load failed ({type(first).__name__}) — purged "
              f"{len(purged)} cached copy(ies), re-downloading")
        try:
            return TextEmbedding(model_name, cache_dir=str(cache))
        except Exception as second:
            raise RuntimeError(_failure_text(model_name, cache, second)) from second


def _failure_text(model_name: str, cache: Path, err: Exception) -> str:
    return (
        f"Could not load embedding model '{model_name}' from {cache}.\n"
        f"  cause: {type(err).__name__}: {err}\n"
        "  The cache was purged and a fresh download still failed — this needs "
        "network access to huggingface.co on the first run.\n"
        "  Point the cache elsewhere with FASTEMBED_CACHE_PATH if that directory "
        "is not writable."
    )
