"""
Semantic re-audit of the already-frozen Sketchfab catalog.

sketchfab.semantic_select / sketchfab.vision_select (wired into both live
fetch_and_freeze call sites — src/unity_entity_asset_resolver.py's Tier S and
src/unity_sketchfab_collection.py's batch collection) only ever check a
candidate BEFORE it is frozen. Every entry written before either gate
existed, or written while a run had the gate off, sits in
lookups/unity/sketchfab_catalog.json unchecked forever — and the keyword-match
cache-hit paths in both callers (Tier S's own catalog scoring;
unity_sketchfab_collection.py's `existing_by_query` reuse) keep serving those
entries to every future project regardless of whether the gate is on today.
This module is the one-time (or periodic) cleanup pass for that backlog —
distinct from, and does not touch, the cache-hit-reuse mechanism itself.

Re-runs the SAME deterministic CLIP/embedding check
(unity_semantic_embed.image_similarity / text_similarity) against each
entry's OWN stored `query` — the exact search text that was used to freeze
it — by re-fetching the live model detail (thumbnail + description) via
SketchfabClient.get_model(sketchfab_uid), since neither is stored on the
frozen entry itself. Never re-ranks against OTHER candidates the way
semantic_filter does (there's only ever one candidate here: the entry
itself) — this is a floor check, not a selection.

Read/write are deliberately separate functions: `audit_catalog` only ever
reads the catalog and returns a report; `apply_removals` and
`backfill_embeddings` are the only functions that write, both re-reading the
catalog fresh at write time (not the audit's now-possibly-stale snapshot) so
a long audit run racing against a live pipeline run that freezes NEW entries
concurrently can never lose them — they only ever touch uids the caller
explicitly names, from whatever is on disk at that moment.

A PASSING entry gets more than a clean bill of health: `audit_entry` already
downloaded its thumbnail to score it, so `backfill_embeddings` can stamp
that same CLIP embedding into the entry as `clip_image_embedding` for free
— the exact field unity_semantic_embed.semantic_filter_frozen needs to
re-check a cache-hit reuse of this entry deterministically, without network,
against some completely different LATER entity's query. Entries frozen
before that gate existed never got this field at freeze time; this is how
they catch up without ever going back to Sketchfab again after this one
audit.

An entry that can no longer be measured at all (model deleted/made private on
Sketchfab, no token, no browser, network down) is left alone in BOTH
functions — missing data is not evidence of a bad pick, exactly like
semantic_filter's own contract.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .sketchfab_client import SketchfabClient
from .unity_asset_catalog import CATALOG_VERSION

log = logging.getLogger(__name__)


def audit_entry(entry: dict, *, client: SketchfabClient,
                min_image_similarity: float, min_text_similarity: float) -> dict:
    """Re-measure one frozen catalog entry against its own `query`.

    Returns {"uid", "name", "query", "image_similarity", "text_similarity",
    "verdict", "embedding"} where verdict is "pass" (every measured axis
    cleared its floor), "fail" (at least one measured axis missed), or
    "unmeasured" (nothing could be measured — no uid/query on the entry, the
    model is no longer fetchable, or neither a thumbnail nor a description
    was available). An axis that couldn't be measured never blocks on its
    own, same as unity_semantic_embed.semantic_filter.

    `embedding` is the CLIP image embedding for the entry's OWN thumbnail
    (or None if unmeasurable) — a free byproduct of the image-similarity
    check above, since it needs the exact same thumbnail bytes. Not used by
    this function, but a caller (backfill_embeddings below) can stamp it
    into a PASSING entry so future cache-hit re-checks
    (unity_semantic_embed.semantic_filter_frozen, from
    unity_entity_asset_resolver.py's Tier S) never need the network again
    for it, even though this entry predates that gate.
    """
    # Imported here (not at module top) so tests can monkeypatch either by
    # dotted path (e.g. "src.unity_semantic_embed.image_similarity") — the
    # same lazy-import convention unity_semantic_embed.semantic_filter itself
    # uses for _thumbnail_url, for the same reason: a top-level `from X
    # import Y` would bind Y once at import time, past the reach of a test
    # that patches X.Y afterward.
    from .unity_semantic_embed import image_embedding, image_similarity, text_similarity
    from .unity_sketchfab_vision import _thumbnail_url

    uid = str(entry.get("sketchfab_uid") or "")
    query = str(entry.get("query") or "")
    name = str(entry.get("filename") or uid or "?")
    result: dict[str, Any] = {"uid": uid, "name": name, "query": query,
                              "image_similarity": None, "text_similarity": None,
                              "verdict": "unmeasured", "embedding": None}
    if not uid or not query:
        return result

    model = client.get_model(uid)
    if not model:
        return result

    thumb_url = _thumbnail_url(model)
    img_bytes = client.fetch_image_bytes(thumb_url) if thumb_url else None
    img_sim = image_similarity(query, img_bytes) if img_bytes else None
    result["embedding"] = image_embedding(img_bytes) if img_bytes else None

    description = str(model.get("description") or entry.get("description") or "")
    txt_sim = text_similarity(query, description) if description else None

    result["image_similarity"] = img_sim
    result["text_similarity"] = txt_sim
    if img_sim is None and txt_sim is None:
        return result

    img_ok = img_sim is None or img_sim >= min_image_similarity
    txt_ok = txt_sim is None or txt_sim >= min_text_similarity
    result["verdict"] = "pass" if (img_ok and txt_ok) else "fail"
    return result


def audit_catalog(catalog_path: Path, *, client: SketchfabClient,
                  min_image_similarity: float = 0.22,
                  min_text_similarity: float = 0.45,
                  sleep_between: float = 0.5,
                  offset: int = 0,
                  limit: Optional[int] = None,
                  progress_fn: Optional[Callable[[int, int, dict], None]] = None
                  ) -> list[dict]:
    """Re-audit entries[offset : offset+limit] (or entries[offset:] when
    `limit` is None) in `catalog_path`.

    Read-only — never writes, regardless of what it finds. `sleep_between`
    throttles consecutive live network calls between entries so a 400+ entry
    catalog doesn't hammer Sketchfab's API in a tight loop. `offset`/`limit`
    let a large catalog be worked through in bounded chunks across several
    runs rather than one long-lived call — entries keep a stable order
    (on-disk order, untouched by this function), so the same
    offset/limit pair always names the same slice as long as nothing else
    has rewritten the catalog in between. `progress_fn`, if given, is called
    after each entry as (index, total, result) — a CLI can use it to print a
    live line without this function knowing about console/rich at all.
    Returns [] if `catalog_path` doesn't exist, isn't valid JSON, or
    `offset` is past the end — an audit run against nothing to audit is not
    an error.
    """
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = list(data.get("entries") or [])
    entries = entries[max(0, offset):]
    if limit is not None:
        entries = entries[:max(0, limit)]

    results: list[dict] = []
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        r = audit_entry(entry, client=client,
                        min_image_similarity=min_image_similarity,
                        min_text_similarity=min_text_similarity)
        results.append(r)
        if progress_fn is not None:
            progress_fn(i, total, r)
        if sleep_between and i < total:
            time.sleep(sleep_between)
    return results


def apply_removals(catalog_path: Path, uids_to_remove: set[str], *,
                   backup: bool = True) -> dict:
    """Rewrite `catalog_path` with every entry whose `sketchfab_uid` is in
    `uids_to_remove` dropped. Re-reads the file fresh (not any snapshot the
    caller's audit_catalog run captured, which may be stale by the time a
    long audit finishes) — a concurrent pipeline run that froze a brand-new
    entry in the meantime is never at risk of being silently dropped, since
    only the NAMED uids are ever removed, whatever else is on disk stays.

    Writes a timestamped `.bak-<UTC timestamp>` copy of the catalog before
    modifying it, unless `backup=False` — a bulk removal across a
    potentially months-old, hand-accumulated local catalog is not something
    to make irreversible by default. Returns {"removed": int, "kept": int,
    "backup_path": str|None}. A no-op (uids_to_remove empty, or the catalog
    doesn't exist) touches nothing and returns zeros.
    """
    if not uids_to_remove or not catalog_path.is_file():
        return {"removed": 0, "kept": 0, "backup_path": None}

    raw = catalog_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    entries = list(data.get("entries") or [])
    kept = [e for e in entries if str(e.get("sketchfab_uid") or "") not in uids_to_remove]
    removed_n = len(entries) - len(kept)

    backup_path: Optional[Path] = None
    if backup and removed_n:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = catalog_path.with_name(f"{catalog_path.stem}.bak-{stamp}{catalog_path.suffix}")
        backup_path.write_text(raw, encoding="utf-8")

    data["entries"] = sorted(kept, key=lambda e: e.get("sketchfab_uid", ""))
    data["version"] = CATALOG_VERSION
    catalog_path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return {"removed": removed_n, "kept": len(kept),
           "backup_path": str(backup_path) if backup_path else None}


def backfill_embeddings(catalog_path: Path,
                        embeddings_by_uid: dict[str, list[float]]) -> dict:
    """Stamp `clip_image_embedding` onto surviving entries that don't
    already have one, using embeddings an audit_catalog run already
    computed as a byproduct (audit_entry's own `embedding` field) — turns a
    PASSING pre-existing entry into one Tier S's cache-hit path can re-check
    deterministically (unity_semantic_embed.semantic_filter_frozen) against
    a future, different entity's query, without ever touching the network
    for it again.

    Never overwrites an embedding an entry already has (e.g. one stamped at
    freeze time by sketchfab_freeze.py itself) — only fills a gap, never
    replaces. Re-reads the catalog fresh at write time, same race-safety
    property as apply_removals: a uid this run never saw (frozen by a
    concurrent pipeline run in the meantime) simply isn't in
    `embeddings_by_uid` and is left untouched.

    Returns {"updated": int} — the count of entries that gained an
    embedding. A no-op (`embeddings_by_uid` empty, or the catalog doesn't
    exist) touches nothing and returns zero.
    """
    if not embeddings_by_uid or not catalog_path.is_file():
        return {"updated": 0}

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = list(data.get("entries") or [])
    updated = 0
    for e in entries:
        if e.get("clip_image_embedding"):
            continue
        emb = embeddings_by_uid.get(str(e.get("sketchfab_uid") or ""))
        if emb:
            e["clip_image_embedding"] = emb
            updated += 1

    if not updated:
        return {"updated": 0}

    data["entries"] = sorted(entries, key=lambda e: e.get("sketchfab_uid", ""))
    data["version"] = CATALOG_VERSION
    catalog_path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return {"updated": updated}
