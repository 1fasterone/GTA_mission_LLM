"""
CLIP / embedding-based semantic asset verification.

Keyword-overlap scoring (unity_entity_asset_resolver._score) and the
optional vision-LLM reorder (unity_sketchfab_vision.py) both have a gap:
neither is a fixed, reproducible function of its inputs. Keyword scoring
can't tell semantic correctness from same-word coincidence; the vision LLM
call depends on whatever model happens to be loaded in LM Studio and its
own sampling, and needs a vision-capable model this pipeline never
guarantees. Confirmed live: DropZone's Player entity, searched as
"realistic paratrooper", downloaded a real, correctly-licensed Sketchfab
asset titled "Cartoon Character" — a valid model that is simply the wrong
thing, and nothing in the resolver could tell.

This module is a third, deterministic signal: cosine similarity between
frozen-model embeddings. Given the same bytes and the same pinned model
weights, the score is byte-reproducible — no sampling, no prompt
sensitivity — which is exactly the "deterministic Python provides the
truth" property Steps 4-7 are supposed to have, and which the LLM vision
call structurally cannot.

Two independent comparisons, kept separate rather than fused into one
score (a candidate that fails on image similarity specifically is a more
actionable diagnostic than one blended number):

  image_similarity(text, image_bytes)
      cosine(CLIP-text-embed(text), CLIP-image-embed(thumbnail)) — the
      real CLIP use case, concept text vs. a picture in one shared
      embedding space. Uses fastembed's ONNX CLIP pair
      (Qdrant/clip-ViT-B-32-text / -vision) rather than torch/open_clip,
      matching this repo's established "ONNX over torch on Windows" choice
      (see requirements.txt's fastembed comment).

  text_similarity(text_a, text_b)
      cosine(bge-embed(text_a), bge-embed(text_b)) — text-vs-text, reusing
      rag/embedder.py's existing BAAI/bge-small-en-v1.5 loader rather than
      CLIP's own text tower: CLIP's text encoder is trained for image
      alignment, not general sentence similarity, and bge-small is already
      the right, already-hardened tool for this in this repo.

Every function here degrades to None on any failure (no cached/downloadable
model, a malformed image, an embedder exception) — this is an opt-in QC
signal layered on top of tiers that already work without it, never a new
point of failure for asset resolution.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
CLIP_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"


def _load_fastembed(model_cls, model_name: str, cache: Optional[Path] = None,
                    repair: bool = True):
    """Instantiate a fastembed model class with the same cache-repair
    discipline as rag.embedder.load_embedder — the Windows %TEMP%
    cache-corruption failure mode it guards against
    (fastembed.common.utils.define_cache_dir defaulting under %TEMP%,
    which Storage Sense partially deletes) is a property of the fastembed
    cache mechanism itself, not of any one model, so any fastembed model
    class needs the same purge-and-retry safety net. Reuses rag.embedder's
    cache_dir()/purge() rather than duplicating them."""
    from rag.embedder import cache_dir as fastembed_cache_dir, purge

    cache = cache or fastembed_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    try:
        return model_cls(model_name, cache_dir=str(cache))
    except Exception as first:  # noqa: BLE001
        if not repair:
            raise
        purged = purge(model_name, cache)
        if not purged:
            raise RuntimeError(
                f"Could not load '{model_name}' from {cache}: {first}") from first
        log.info("[semantic] load failed (%s) — purged %d cached copy(ies), "
                 "re-downloading", type(first).__name__, len(purged))
        try:
            return model_cls(model_name, cache_dir=str(cache))
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(
                f"Could not load '{model_name}' from {cache}: {second}") from second


@lru_cache(maxsize=1)
def _clip_text_embedder():
    from fastembed import TextEmbedding
    return _load_fastembed(TextEmbedding, CLIP_TEXT_MODEL)


@lru_cache(maxsize=1)
def _clip_image_embedder():
    from fastembed import ImageEmbedding
    return _load_fastembed(ImageEmbedding, CLIP_IMAGE_MODEL)


@lru_cache(maxsize=1)
def _bge_text_embedder():
    from rag.embedder import load_embedder
    return load_embedder()  # BAAI/bge-small-en-v1.5, already cache-repaired


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def image_similarity(text: str, image_bytes: bytes) -> Optional[float]:
    """cosine(CLIP-text-embed(text), CLIP-image-embed(image_bytes)) in
    [-1, 1], or None on any failure — no text/bytes, an unloadable CLIP
    model (no network on first run, corrupted cache repair also failed), or
    bytes PIL can't decode as an image. Never raises."""
    if not text or not image_bytes:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        txt_emb = next(iter(_clip_text_embedder().embed([text])))
        img_emb = next(iter(_clip_image_embedder().embed([img])))
    except Exception as exc:  # noqa: BLE001
        log.debug("[semantic] image_similarity(%r) failed: %s", text, exc)
        return None
    return _cosine(txt_emb, img_emb)


def image_embedding(image_bytes: bytes) -> Optional[list[float]]:
    """Raw CLIP image embedding for `image_bytes`, as a plain JSON-
    serializable list of floats, or None on any failure. Never raises.

    Meant to be stamped into a frozen Sketchfab catalog entry ONCE, at
    freeze time (see sketchfab_freeze.py), so a later semantic check never
    needs to re-download the thumbnail — see
    image_similarity_from_embedding()'s own docstring for why that matters
    for a deterministic cache-hit re-check.
    """
    if not image_bytes:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        emb = next(iter(_clip_image_embedder().embed([img])))
    except Exception as exc:  # noqa: BLE001
        log.debug("[semantic] image_embedding() failed: %s", exc)
        return None
    return [float(v) for v in emb]


def image_similarity_from_embedding(text: str,
                                    image_embedding: list[float]) -> Optional[float]:
    """cosine(CLIP-text-embed(text), `image_embedding`) — the network-free
    sibling of image_similarity(): reuses an embedding captured once at
    freeze time (image_embedding() above) instead of re-fetching the
    thumbnail, so an already-frozen catalog entry can be re-checked against
    a DIFFERENT entity's query with zero network at read time — the
    property src.unity_entity_asset_resolver.py's Tier S cache-hit path
    needs to stay a deterministic lookup (see semantic_filter_frozen()).
    None on any failure (no text/embedding, unloadable CLIP text model).
    Never raises."""
    if not text or not image_embedding:
        return None
    try:
        txt_emb = next(iter(_clip_text_embedder().embed([text])))
    except Exception as exc:  # noqa: BLE001
        log.debug("[semantic] image_similarity_from_embedding(%r) failed: %s", text, exc)
        return None
    return _cosine(txt_emb, image_embedding)


def text_similarity(text_a: str, text_b: str) -> Optional[float]:
    """cosine(bge-embed(text_a), bge-embed(text_b)) in [-1, 1], or None on
    any failure. Never raises."""
    if not text_a or not text_b:
        return None
    try:
        emb_a = next(iter(_bge_text_embedder().embed([text_a])))
        emb_b = next(iter(_bge_text_embedder().embed([text_b])))
    except Exception as exc:  # noqa: BLE001
        log.debug("[semantic] text_similarity(%r, %r) failed: %s", text_a, text_b, exc)
        return None
    return _cosine(emb_a, emb_b)


def semantic_filter(entity_name: str, entity_kind: str, concept_text: str,
                    candidates: list[dict], *, client, min_image_similarity: float,
                    min_text_similarity: float, max_candidates: int = 6) -> list[dict]:
    """Filter + reorder keyword-ranked Sketchfab candidates (raw model
    dicts, best-first — the same shape unity_sketchfab_vision.select_best_
    candidate consumes) by semantic similarity to `concept_text` (typically
    the entity's own sketchfab_query).

    A candidate clears the gate only if EVERY axis actually measured for it
    is at or above its floor — an axis that couldn't be measured (no
    fetchable thumbnail, no description text) never blocks on its own,
    since that is missing data, not evidence of a mismatch. Candidates that
    clear both floors are returned best-first by image similarity (the
    stronger, genuinely-CLIP signal); candidates neither passed nor
    rejected because NOTHING was measurable for them are appended
    afterward, unranked, so they are never silently discarded.

    Returns the ORIGINAL `candidates` list untouched when nothing in the
    whole pool could be measured at all (no network on first run, every
    thumbnail unfetchable, every description empty) — that is an embedder
    availability failure, not a semantic verdict, and must degrade to a
    no-op exactly like unity_sketchfab_vision.select_best_candidate
    returning None, never to "reject everything."

    Returns an EMPTY list only when at least one candidate was genuinely
    measured and every measured candidate failed its floor — the caller
    (sketchfab_freeze._fetch_and_freeze_inner) treats that the same as a
    keyword-score miss: fall through to the next asset tier rather than
    accept a semantically wrong pick. The closest rejected candidate is
    logged as the concrete remedy for a human reading the run, not just a
    bare miss.
    """
    from .unity_sketchfab_vision import _thumbnail_url

    pool = candidates[:max(1, max_candidates)]
    passed: list[tuple[float, float, dict]] = []
    rejected: list[tuple[float, float, dict]] = []
    unscored: list[dict] = []
    any_measured = False

    for model in pool:
        url = _thumbnail_url(model)
        img_bytes = client.fetch_image_bytes(url) if url else None
        img_sim = image_similarity(concept_text, img_bytes) if img_bytes else None
        description = str(model.get("description") or model.get("name") or "")
        txt_sim = text_similarity(concept_text, description) if description else None

        if img_sim is None and txt_sim is None:
            unscored.append(model)
            continue
        any_measured = True
        img_ok = img_sim is None or img_sim >= min_image_similarity
        txt_ok = txt_sim is None or txt_sim >= min_text_similarity
        entry = (img_sim if img_sim is not None else 1.0,
                 txt_sim if txt_sim is not None else 1.0, model)
        (passed if (img_ok and txt_ok) else rejected).append(entry)

    if not any_measured:
        return candidates

    if not passed:
        if rejected:
            best_img, best_txt, best_model = max(rejected, key=lambda t: (t[0], t[1]))
            log.warning(
                "[semantic] entity '%s' (kind=%s): every scoreable Sketchfab "
                "candidate for %r failed the semantic floor (need "
                "image>=%.2f and text>=%.2f). Closest: %r (image=%.2f, "
                "text=%.2f). Falling through to the next asset tier instead "
                "of accepting a mismatch.", entity_name, entity_kind,
                concept_text, min_image_similarity, min_text_similarity,
                best_model.get("name"), best_img, best_txt)
        return unscored

    passed.sort(key=lambda t: (-t[0], -t[1]))
    return [m for _i, _t, m in passed] + unscored


def semantic_filter_frozen(entity_name: str, entity_kind: str, concept_text: str,
                           candidates: list[dict], *, min_image_similarity: float,
                           min_text_similarity: float,
                           max_candidates: int = 6) -> list[dict]:
    """Deterministic, network-free sibling of semantic_filter() for
    CACHE-HIT reuse.

    semantic_filter() only ever gates a LIVE fetch — a cache MISS in
    src.unity_entity_asset_resolver.py's Tier S. Once one model was frozen
    for one query, every LATER entity whose keywords merely overlapped
    reused that same frozen entry verbatim via Tier S's own keyword-match
    cache-hit path, semantic_select on or off — the gate never re-ran
    against the NEW entity's own query. This closes that gap using only
    what is already sitting in the frozen entry — no `client`, no network —
    which is the whole point: Tier S's cache-hit path has to stay a pure,
    deterministic lookup (see CLAUDE.md's "Steps 4-7 remain deterministic"
    and sketchfab_freeze.py's own "frozen artifact" doctrine), so this
    cannot re-fetch a thumbnail the way semantic_filter() does.

      text axis  — text_similarity(concept_text, entry['description']) —
                   already stored on every frozen entry; always measurable.
      image axis — image_similarity_from_embedding(concept_text,
                   entry['clip_image_embedding']) — only present on entries
                   frozen (or backfilled — see
                   src.sketchfab_catalog_audit.backfill_embeddings) while
                   semantic_select was on. Absent on everything else, in
                   which case this axis is simply unmeasured for that
                   entry — never blocking, same "missing data is not
                   evidence of a mismatch" contract semantic_filter() uses.

    `candidates` here are CATALOG-ENTRY dicts (filename/description/
    sketchfab_uid/clip_image_embedding/...) — NOT raw Sketchfab search
    results. Do not pass one shape to the function built for the other.

    Same pass/reject/unscored/any_measured shape as semantic_filter(): see
    its docstring for the exact rules this mirrors.
    """
    pool = candidates[:max(1, max_candidates)]
    passed: list[tuple[float, float, dict]] = []
    rejected: list[tuple[float, float, dict]] = []
    unscored: list[dict] = []
    any_measured = False

    for entry in pool:
        stored_emb = entry.get("clip_image_embedding")
        img_sim = (image_similarity_from_embedding(concept_text, stored_emb)
                  if stored_emb else None)
        description = str(entry.get("description") or "")
        txt_sim = text_similarity(concept_text, description) if description else None

        if img_sim is None and txt_sim is None:
            unscored.append(entry)
            continue
        any_measured = True
        img_ok = img_sim is None or img_sim >= min_image_similarity
        txt_ok = txt_sim is None or txt_sim >= min_text_similarity
        scored = (img_sim if img_sim is not None else 1.0,
                 txt_sim if txt_sim is not None else 1.0, entry)
        (passed if (img_ok and txt_ok) else rejected).append(scored)

    if not any_measured:
        return candidates

    if not passed:
        if rejected:
            best_img, best_txt, best_entry = max(rejected, key=lambda t: (t[0], t[1]))
            log.warning(
                "[semantic] entity '%s' (kind=%s): every scoreable CACHED "
                "Sketchfab entry for %r failed the semantic floor (need "
                "image>=%.2f and text>=%.2f). Closest: %r (image=%.2f, "
                "text=%.2f). Falling through instead of reusing a stale "
                "cache hit.", entity_name, entity_kind, concept_text,
                min_image_similarity, min_text_similarity,
                best_entry.get("filename"), best_img, best_txt)
        return unscored

    passed.sort(key=lambda t: (-t[0], -t[1]))
    return [e for _i, _t, e in passed] + unscored
