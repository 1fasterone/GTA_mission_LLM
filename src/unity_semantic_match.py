"""
Embedding glue between the deterministic keyword scorers
(unity_entity_asset_resolver._score, unity_scenebind.bind_score) and the
embedding primitive (unity_semantic_embed.text_similarity /
_bge_text_embedder). Closes docs/COMPILER_AUDIT.md Gap E's remaining half
(solution #7) — "reuse chromadb+fastembed for model resolution & scene
binding" — deliberately WITHOUT chromadb: see the module-level rationale
below before reaching for an ANN index here.

Three sections:

  SHARED    entity_embedding_text() / group_embedding_text() /
            catalog_entry_embedding_text() — deterministic, fixed-field-order
            text builders. Never a bare set() — the same PYTHONHASHSEED
            landmine unity_entity_asset_resolver._stable_seed was already
            built to dodge (a set's iteration order is salted per-process;
            this module's job is to feed a scorer whose determinism tests
            assert byte-identical output across runs).

  CATALOG   precompute/cache/matrix machinery for the ~3k-entry local
            catalog. Built offline (`unity-catalog --embed`), cached to
            lookups/unity/asset_catalog_embeddings.{npz,meta.json} (gitignored
            — a binary blob tied to one pinned model version, trivially
            rebuildable, following the rag/index/ precedent rather than the
            small hand-curated catalog JSONs that ARE committed), loaded once
            per process (load_catalog_embeddings, lru_cache-keyed by
            lookups_dir, mirrors unity_concept_layers.load_concept_layers
            exactly). At match time: one query embed + one vectorized
            matrix-vector cosine over every row — never a live re-embed of
            the whole catalog, never a per-entry Python call.

            Why not chromadb, given the catalog IS a stable, reusable corpus
            (unlike scenebind's novel-per-run text below)? Scale: 2953
            Kenney entries sits well under where this repo's own RAG corpus
            says an approximate HNSW index starts earning its keep (~20k,
            per rag/query.py's own comment on its animation collection).
            Brute-force cosine over a precomputed matrix is sub-millisecond
            at this N, simpler to build (no persistent-index lifecycle to
            manage), and avoids adding a SECOND nondeterminism surface
            (HNSW's own approximate recall, index-build order) on top of the
            one this tier already has to protect — its seeded-softmax
            variety-rotation sampling (_stable_seed, _register in
            unity_entity_asset_resolver.py).

  SCENEBIND embed_query_cached()/_embed_query() are catalog-only. Scene-bind's
            bind_score() calls unity_semantic_embed.text_similarity()
            directly, pairwise, uncached: real scenes have 5-11 entities
            against 1-4 authored groups (tiny-N), and the text (group
            filenames, entity names) is novel every run — there is no stable
            corpus here to precompute against, so a persistent cache would
            only add complexity for no benefit.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from rag.embedder import EMBED_MODEL as BGE_MODEL_NAME

log = logging.getLogger(__name__)

CATALOG_EMBEDDINGS_FILENAME = "asset_catalog_embeddings.npz"
CATALOG_EMBEDDINGS_META_FILENAME = "asset_catalog_embeddings.meta.json"


# ── shared text builders ────────────────────────────────────────────────────

def entity_embedding_text(entity, kind: Optional[str] = None) -> str:
    """The same strong-signal fields _query_weights()/bind_score() already
    treat as an entity's identity (name, kind, model_hint, sketchfab_query),
    joined as plain text instead of scored as a token set. Fixed field order
    -> the same entity always embeds to the same text regardless of process/
    PYTHONHASHSEED. `kind` defaults to the entity's own field but accepts an
    override so callers that already normalized it (lowercased, defaulted to
    "prop" — see unity_entity_asset_resolver's `kind = (e.kind or
    "prop").lower()`) don't compute it twice or risk it drifting."""
    k = kind if kind is not None else (getattr(entity, "kind", "") or "")
    parts = [
        entity.name or "",
        k,
        getattr(entity, "model_hint", "") or "",
        getattr(entity, "sketchfab_query", "") or "",
    ]
    return " ".join(p for p in parts if p).strip()


def group_embedding_text(group: dict) -> str:
    """Mirrors bind_score's gterms fields (name, source_file/file)."""
    parts = [
        group.get("name") or "",
        group.get("source_file") or group.get("file") or "",
    ]
    return " ".join(p for p in parts if p).strip()


def catalog_entry_embedding_text(entry: dict) -> str:
    """keywords (curated, already an ordered list) + filename-stem tokens +
    category. Deliberately NOT `description`: measured across the real
    catalog, only 104/2953 (3.5%) entries have hand-written descriptions —
    the other 96.5% are auto-templated "<filename words> — <category> model"
    strings carrying zero information beyond what keywords/filename already
    give (see docs/COMPILER_AUDIT.md Gap E)."""
    from .unity_asset_catalog import tokenize
    stem_tokens = tokenize(Path(entry.get("filename", "")).stem)
    parts = list(entry.get("keywords") or []) + stem_tokens + [entry.get("category") or ""]
    return " ".join(p for p in parts if p)


# ── catalog: precompute / cache / matrix ────────────────────────────────────

@dataclass(frozen=True)
class CatalogEmbeddings:
    vectors: "np.ndarray"        # (N, D) float32, L2-normalized rows
    keys: tuple[str, ...]        # relpath per row, aligned to vectors
    catalog_hash: str
    model: str


def _non_sketchfab(assets: list[dict]) -> list[dict]:
    """Kenney tiers must never see Sketchfab-sourced entries — mirrors
    resolve_entity_assets' own split (unity_entity_asset_resolver.py, the
    `assets = [a for a in _all_assets if a.get("source") != "sketchfab"]`
    line) so the embedding matrix is built over exactly the same pool the
    keyword scorer matches against."""
    return [a for a in assets if a.get("source") != "sketchfab"]


def catalog_content_hash(assets: list[dict]) -> str:
    """sha1 over (relpath, filename, category, keywords) for every
    non-Sketchfab entry, sorted by relpath — a plain list, never a set (see
    module docstring). Deliberately ignores fields that don't feed
    catalog_entry_embedding_text (bounds, generic_score, pack, ...) so an
    unrelated `--measure` re-run doesn't spuriously invalidate this cache."""
    rows = sorted(
        (a.get("relpath", ""), a.get("filename", ""), a.get("category", ""),
         tuple(a.get("keywords", [])))
        for a in _non_sketchfab(assets))
    payload = json.dumps(rows, sort_keys=False).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def build_catalog_embeddings(assets: list[dict]) -> CatalogEmbeddings:
    """Offline, one-time — called by `unity-catalog --embed`, never at
    resolve time. ONE batched .embed() call over every entry's text, not one
    call per entry: embeddings are never cached per-call in
    unity_semantic_embed.text_similarity(), so calling it 2953 times per
    resolve would scale linearly with catalog size for no reason when the
    catalog barely changes."""
    kenney = sorted(_non_sketchfab(assets), key=lambda a: a.get("relpath", ""))
    texts = [catalog_entry_embedding_text(a) for a in kenney]
    keys = tuple(a.get("relpath", "") for a in kenney)

    from . import unity_semantic_embed as sem
    raw = list(sem._bge_text_embedder().embed(texts)) if texts else []
    vectors = np.asarray(raw, dtype=np.float32) if raw else np.zeros((0, 384), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return CatalogEmbeddings(
        vectors=vectors / norms, keys=keys,
        catalog_hash=catalog_content_hash(assets), model=BGE_MODEL_NAME)


def write_catalog_embeddings(ce: CatalogEmbeddings, lookups_dir: str) -> Path:
    base = Path(lookups_dir) / "unity"
    base.mkdir(parents=True, exist_ok=True)
    npz_path = base / CATALOG_EMBEDDINGS_FILENAME
    np.savez_compressed(npz_path, vectors=ce.vectors)
    meta_path = base / CATALOG_EMBEDDINGS_META_FILENAME
    meta_path.write_text(json.dumps({
        "version": 1,
        "model": ce.model,
        "catalog_hash": ce.catalog_hash,
        "count": len(ce.keys),
        "keys": list(ce.keys),
    }, indent=2, sort_keys=True), encoding="utf-8")
    load_catalog_embeddings.cache_clear()
    return npz_path


def build_and_write_catalog_embeddings(catalog: dict, lookups_dir: str) -> Path:
    """CLI-facing convenience: build + write in one call (`unity-catalog --embed`)."""
    return write_catalog_embeddings(
        build_catalog_embeddings(catalog.get("assets", [])), lookups_dir)


@lru_cache(maxsize=8)
def load_catalog_embeddings(lookups_dir: str = "lookups") -> Optional[CatalogEmbeddings]:
    """Load the precomputed catalog embedding matrix, or None on ANY
    failure (missing files, corrupt npz, malformed json, shape mismatch, a
    model-name mismatch against the currently active BGE_MODEL_NAME) — never
    raises. Cached by directory, mirroring unity_concept_layers.
    load_concept_layers exactly. Content-staleness (this cache vs. the
    currently-loaded catalog.json) is the CALLER's job via
    catalog_content_hash() — this function has no access to the live catalog
    to compare against."""
    base = Path(lookups_dir) / "unity"
    try:
        meta = json.loads((base / CATALOG_EMBEDDINGS_META_FILENAME).read_text(encoding="utf-8"))
        if meta.get("model") != BGE_MODEL_NAME:
            return None
        with np.load(base / CATALOG_EMBEDDINGS_FILENAME) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
        keys = tuple(meta["keys"])
        if vectors.shape[0] != len(keys):
            return None
        return CatalogEmbeddings(vectors=vectors, keys=keys,
                                 catalog_hash=str(meta.get("catalog_hash", "")),
                                 model=str(meta["model"]))
    except Exception as exc:  # noqa: BLE001 — any failure degrades to None
        log.debug("[semantic_match] catalog embeddings unavailable: %s", exc)
        return None


def catalog_similarity_vector(ce: CatalogEmbeddings, query_vec: "np.ndarray") -> dict[str, float]:
    """ONE vectorized matrix-vector cosine over all N precomputed rows — the
    whole point of precomputing: sub-millisecond, no per-entry Python call.
    `ce.vectors` rows are already L2-normalized (build_catalog_embeddings),
    so this is a plain dot product once the query is normalized too."""
    qn = float(np.linalg.norm(query_vec))
    if qn == 0.0:
        return {}
    sims = ce.vectors @ (np.asarray(query_vec, dtype=np.float32) / qn)
    return dict(zip(ce.keys, sims.tolist()))


# ── query embedding (catalog tier only — see module docstring) ─────────────

def embed_query_cached(text: str, cache: dict[str, Optional["np.ndarray"]]
                       ) -> Optional["np.ndarray"]:
    """Memoized query embed, keyed by exact text. `cache` is CALLER-owned
    (one dict per resolve_entity_assets() call, never module-global) —
    avoids re-embedding identical query text for entities that share a
    name/kind/model_hint within one resolve call, without pretending
    caching is valid across separate resolve calls (different schemas,
    different entities)."""
    if text in cache:
        return cache[text]
    vec = _embed_query(text)
    cache[text] = vec
    return vec


def _embed_query(text: str) -> Optional["np.ndarray"]:
    if not text:
        return None
    from . import unity_semantic_embed as sem   # deferred: tests monkeypatch
                                                 # "src.unity_semantic_embed._bge_text_embedder"
                                                 # at its origin; a top-level
                                                 # import would bind the name
                                                 # before that patch applies.
    try:
        return np.asarray(next(iter(sem._bge_text_embedder().embed([text]))), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        log.debug("[semantic_match] query embed failed: %s", exc)
        return None
