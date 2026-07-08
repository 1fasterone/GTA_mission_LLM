"""
Sketchfab fetch-and-freeze — the nondeterministic-instrument -> frozen-artifact
stage for the Sketchfab model source (see docs/model-pipeline-architecture.md).

search + license-filter + download + Blender-convert are ALL nondeterministic
(network, API ranking, third-party availability) — this module is the one place
that's allowed to run them. Its output is a plain dict written to
lookups/unity/sketchfab_catalog.json via unity_asset_catalog.write_sketchfab_entry,
which src/unity_entity_asset_resolver.py then reads as pure, deterministic lookup
data — exactly like the existing Kenney catalog. Nothing in this module is ever
called from generate()/write() directly; the resolver's Tier S is the only
caller, and only when `sketchfab.auto_fetch: true`.

fetch_and_freeze() never raises — every failure mode (no token, no search hits,
no license-compliant hit, download failure, missing Blender, convert failure)
returns None so the caller falls through to the Kenney catalog cleanly.
"""

from __future__ import annotations

import logging
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .sketchfab_client import SketchfabClient
from .unity_asset_catalog import SKETCHFAB_RELPATH, tokenize, write_sketchfab_entry

log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    tokens = [t for t in re.split(r"[^a-zA-Z0-9]+", text or "") if t]
    return "-".join(t.lower() for t in tokens) or "model"


def _synthetic_entry(model: dict, kind_hints: Iterable[str]) -> dict:
    """A catalog-entry-shaped dict for one Sketchfab search result, just
    enough for the caller's score_fn (unity_entity_asset_resolver._score) to
    rank it against real catalog entries — same keyword-overlap contract."""
    name = str(model.get("name") or "")
    tags = [t.get("name", "") for t in (model.get("tags") or []) if isinstance(t, dict)]
    keywords = sorted(set(tokenize(name)) | {t.lower() for t in tags if t})
    return {
        "filename": f"{_slug(name)}-{model.get('uid', '')}.fbx",
        "keywords": list(keywords),
        "kind_hints": list(kind_hints),
        "generic_score": 0,
        "_model": model,   # stashed for the caller; not a real catalog field
    }


def _find_gltf(root: Path) -> Optional[Path]:
    for pat in ("*.gltf", "*.glb"):
        hit = next(root.rglob(pat), None)
        if hit is not None:
            return hit
    return None


def _attribution_text(model: dict, license_slug: str) -> str:
    name = model.get("name") or "Untitled"
    url = model.get("viewerUrl") or (
        f"https://sketchfab.com/3d-models/{model.get('uid', '')}")
    user = model.get("user") or {}
    author = user.get("displayName") or user.get("username") or "Unknown"
    author_url = user.get("profileUrl") or ""
    label = {"cc0": "CC0", "by": "CC-BY 4.0", "by-sa": "CC-BY-SA 4.0",
             "by-nd": "CC-BY-ND 4.0"}.get(license_slug, license_slug.upper())
    by = f"[{author}]({author_url})" if author_url else author
    return f'"[{name}]({url})" by {by} is licensed under {label}.'


def fetch_and_freeze(query: str, score_fn: Callable[[dict], float], *,
                     client: SketchfabClient, blender_cfg: Optional[dict] = None,
                     cache_dir: Path | str = "Models/Sketchfab",
                     lookups_dir: str = "lookups",
                     category: str = "prop",
                     kind_hints: Iterable[str] = (),
                     min_score: float = 0.0) -> Optional[dict]:
    """Search Sketchfab for `query`, rank license-compliant hits with the
    caller-supplied `score_fn`, download + convert the best one, freeze it
    into lookups/unity/sketchfab_catalog.json, and return the new catalog
    entry — or None on any failure. `score_fn` should be the resolver's own
    per-entity scorer (e.g. `lambda a: _score(a, qw, kind)`), so Sketchfab
    candidates are ranked with the exact same weighted-keyword logic as
    Kenney catalog entries — this function does no ranking of its own.
    """
    try:
        return _fetch_and_freeze_inner(
            query, score_fn, client=client, blender_cfg=blender_cfg,
            cache_dir=Path(cache_dir), lookups_dir=lookups_dir,
            category=category, kind_hints=list(kind_hints), min_score=min_score)
    except Exception as exc:  # noqa: BLE001 — must never propagate to the resolver
        log.debug("sketchfab fetch_and_freeze(%r) failed: %s", query, exc)
        return None


def _fetch_and_freeze_inner(query: str, score_fn: Callable[[dict], float], *,
                            client: SketchfabClient, blender_cfg: Optional[dict],
                            cache_dir: Path, lookups_dir: str, category: str,
                            kind_hints: list[str], min_score: float) -> Optional[dict]:
    if not client.available():
        return None

    results = client.filter_licensed(client.search(query))
    if not results:
        return None

    candidates = [(score_fn(_synthetic_entry(m, kind_hints)), m) for m in results]
    candidates = [(s, m) for s, m in candidates if s >= min_score]
    if not candidates:
        return None
    candidates.sort(key=lambda sm: (-sm[0], sm[1].get("uid", "")))
    _, model = candidates[0]

    uid = model.get("uid") or ""
    if not uid:
        return None
    license_slug = client.license_slug(model) or "cc0"
    slug = _slug(model.get("name") or uid)
    dest_dir = cache_dir / category / f"{slug}-{uid}"
    fbx_out = dest_dir / f"{slug}-{uid}.fbx"
    textures_dir = dest_dir / "Textures"

    with tempfile.TemporaryDirectory(prefix="sketchfab_") as tmp:
        tmp_zip = Path(tmp) / f"{uid}.zip"
        if client.download_archive(uid, tmp_zip) is None:
            return None
        raw_dir = Path(tmp) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(raw_dir)
        except zipfile.BadZipFile:
            return None

        gltf_path = _find_gltf(raw_dir)
        if gltf_path is None:
            return None

        from .blender.sketchfab_convert import convert_gltf_to_fbx
        converted = convert_gltf_to_fbx(
            gltf_path, fbx_out, textures_dir, blender_cfg=blender_cfg)
        if converted is None:
            return None

    keywords = sorted(set(tokenize(model.get("name") or ""))
                      | {t.get("name", "").lower()
                         for t in (model.get("tags") or []) if isinstance(t, dict)})
    user = model.get("user") or {}
    entry = {
        "filename": fbx_out.name,
        "relpath": str(fbx_out.relative_to(cache_dir).as_posix()),
        "root": str(cache_dir.as_posix()),
        "format": "fbx",
        "pack": "sketchfab",
        "source": "sketchfab",
        "category": category,
        "description": str(model.get("description") or model.get("name") or ""),
        "keywords": keywords,
        "kind_hints": list(kind_hints) or ["prop"],
        "game_contexts": ["any"],
        "generic_score": 0,
        "scale": 1.0,
        "curated": False,
        "query": query,
        "sketchfab_uid": uid,
        "sketchfab_url": model.get("viewerUrl")
                         or f"https://sketchfab.com/3d-models/{uid}",
        "license": license_slug,
        "author": user.get("displayName") or user.get("username") or "",
        "author_url": user.get("profileUrl") or "",
        "attribution": _attribution_text(model, license_slug),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    write_sketchfab_entry(entry, Path(lookups_dir) / SKETCHFAB_RELPATH)
    return entry
