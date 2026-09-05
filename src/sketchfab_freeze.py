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

Style coherence lives one level up in src/sketchfab_style.py
(SketchfabStyleSession): the resolver composes the query (entity + style
tokens), wraps score_fn with anchor-similarity scoring, and passes
`search_params` (face-count bounds) + `style_meta` (provenance) down through
here. This module stays ranking-free and style-free on purpose — it only
threads those knobs through to the client and stamps provenance into the
frozen entry.

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
    rank it against real catalog entries — same keyword-overlap contract.
    `_model` is stashed so a style-aware score wrapper
    (SketchfabStyleSession.wrap_score) can also read author/user fields."""
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


def _rank(results: list[dict], score_fn: Callable[[dict], float],
         kind_hints: list[str], min_score: float) -> list[tuple[float, dict]]:
    """Score + threshold-filter raw Sketchfab search results against the
    caller's score_fn. Shared by both the author-scoped and global search
    passes in _fetch_and_freeze_inner so they rank identically."""
    scored = [(score_fn(_synthetic_entry(m, kind_hints)), m) for m in results]
    return [(s, m) for s, m in scored if s >= min_score]


def _report_scale_correction(fbx_out: Path, model_name: str) -> dict:
    """Log when convert_gltf_to_fbx's bounding-box sanity check corrected an
    implausible import scale — visibility for a class of upstream authoring
    error (wrong unit in the source upload) this pipeline can't prevent, only
    compensate for. Best-effort: a missing/unreadable convert.json is not an
    error, just nothing to report.

    Returns the convert-time size diagnostics to persist into the catalog
    entry ({"bbox_meters": [x, y, z], "scale_corrected": factor-or-None}, or
    {} when the sidecar is missing/unreadable) — NOT consumed by any
    placement math, purely so mis-scaled models are greppable/testable
    without re-opening Blender."""
    import json as _json
    result_path = fbx_out.with_suffix(".convert.json")
    try:
        data = _json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    factor = data.get("scale_corrected")
    if factor:
        before = data.get("largest_dim_before")
        log.warning(
            "[sketchfab] %r imported at an implausible scale (largest "
            "dimension %.1fm) — auto-corrected by %.4fx before export. The "
            "source upload itself uses the wrong unit; this is compensating,"
            " not fixing, the upstream error.", model_name, before or 0.0, factor)
    diag: dict = {"scale_corrected": factor}
    if isinstance(data.get("size_after"), list):
        diag["bbox_meters"] = data["size_after"]
    return diag


def fetch_and_freeze(query: str, score_fn: Callable[[dict], float], *,
                     client: SketchfabClient, blender_cfg: Optional[dict] = None,
                     cache_dir: Path | str = "Models/Sketchfab",
                     lookups_dir: str = "lookups",
                     category: str = "prop",
                     kind_hints: Iterable[str] = (),
                     min_score: float = 0.0,
                     preferred_author: Optional[str] = None,
                     max_candidates: int = 4,
                     search_params: Optional[dict] = None,
                     style_meta: Optional[dict] = None,
                     vision_select_fn: Optional[Callable[[list[dict]], Optional[str]]] = None,
                     semantic_filter_fn: Optional[Callable[[list[dict]], list[dict]]] = None
                     ) -> Optional[dict]:
    """Search Sketchfab for `query`, rank license-compliant hits with the
    caller-supplied `score_fn`, download + convert the best one, freeze it
    into lookups/unity/sketchfab_catalog.json, and return the new catalog
    entry — or None on any failure. `score_fn` should be the resolver's own
    per-entity scorer (e.g. `lambda a: _score(a, qw, kind)`), optionally
    wrapped by SketchfabStyleSession.wrap_score for anchor-similarity — so
    Sketchfab candidates are ranked with the exact same weighted-keyword
    logic as Kenney catalog entries; this function does no ranking of its
    own.

    `preferred_author` (a Sketchfab `username`, not `displayName`) tries that
    one author's own catalog FIRST, falling back to the open marketplace only
    if nothing of theirs clears `min_score` — style-coherent sourcing: once
    one good model is found for a project, later entities prefer the SAME
    artist's other uploads instead of each independently landing on a
    different, stylistically unrelated author ("constructed chaos").

    `search_params` (extra kwargs for SketchfabClient.search — typically
    sketchfab_style.search_params_for(): face-count bounds, optionally
    strict tags) applies to BOTH the author-scoped and global passes, so the
    structural style filter never lapses on fallback.

    `style_meta` (typically SketchfabStyleSession.provenance()) is stamped
    into the frozen catalog entry — setdefault-merged so it can never
    clobber a core field — recording style_id / style_tokens / anchor so a
    replay can reconstruct why this model was chosen without touching the
    network. When the resolver runs with min_score=0.0 and no session, both
    default to no-ops and behavior is byte-identical to before.

    `vision_select_fn` (typically unity_sketchfab_vision.select_best_candidate,
    bound to one entity) is the visual-quality-control step text scoring
    cannot do: called ONCE with the keyword-ranked candidate list (best-first,
    the raw Sketchfab model dicts) after ranking and before download, it may
    return a candidate `uid` to move to the front — text and vision AGREEING
    is the common case (this only ever reorders within candidates that
    already cleared min_score, never adds an off-target one the keyword pass
    rejected). None (no vision model configured, no reachable thumbnails, an
    explicit "none match" verdict, any failure) leaves the keyword order
    exactly as it always was.

    `semantic_filter_fn` (typically unity_semantic_embed.semantic_filter,
    bound to one entity) is `vision_select_fn`'s deterministic sibling —
    CLIP/embedding cosine similarity instead of an LLM chat call. Called
    ONCE with the same keyword-ranked candidate list, BEFORE
    `vision_select_fn`, it returns the subset that clears a semantic
    similarity floor, best-first. Unlike `vision_select_fn` it MAY shrink
    the candidate list to empty when nothing clears the floor — that is
    treated as a clean miss (falls through to the next asset tier), never
    as an error. None (not configured) leaves the keyword order untouched.
    """
    try:
        return _fetch_and_freeze_inner(
            query, score_fn, client=client, blender_cfg=blender_cfg,
            cache_dir=Path(cache_dir), lookups_dir=lookups_dir,
            category=category, kind_hints=list(kind_hints), min_score=min_score,
            preferred_author=preferred_author, max_candidates=max_candidates,
            search_params=dict(search_params or {}),
            style_meta=dict(style_meta or {}),
            vision_select_fn=vision_select_fn,
            semantic_filter_fn=semantic_filter_fn)
    except Exception as exc:  # noqa: BLE001 — must never propagate to the resolver
        log.debug("sketchfab fetch_and_freeze(%r) failed: %s", query, exc)
        return None


def _fetch_and_freeze_inner(query: str, score_fn: Callable[[dict], float], *,
                            client: SketchfabClient, blender_cfg: Optional[dict],
                            cache_dir: Path, lookups_dir: str, category: str,
                            kind_hints: list[str], min_score: float,
                            preferred_author: Optional[str] = None,
                            max_candidates: int = 4,
                            search_params: Optional[dict] = None,
                            style_meta: Optional[dict] = None,
                            vision_select_fn: Optional[Callable[[list[dict]], Optional[str]]] = None,
                            semantic_filter_fn: Optional[Callable[[list[dict]], list[dict]]] = None
                            ) -> Optional[dict]:
    if not client.available():
        return None
    search_params = search_params or {}

    candidates: list[tuple[float, dict]] = []
    if preferred_author:
        author_results = client.filter_licensed(
            client.search(query, user=preferred_author, **search_params))
        candidates = _rank(author_results, score_fn, kind_hints, min_score)

    if not candidates:
        results = client.filter_licensed(client.search(query, **search_params))
        if not results:
            return None
        candidates = _rank(results, score_fn, kind_hints, min_score)
        if not candidates:
            return None

    candidates.sort(key=lambda sm: (-sm[0], sm[1].get("uid", "")))

    # Deterministic semantic gate, ahead of the (optional) LLM vision pass
    # below: CLIP/embedding cosine similarity between the query and each
    # candidate's own thumbnail + description. Unlike vision_select_fn this
    # can actually SHRINK the pool — a candidate list that fails the floor
    # entirely comes back empty, which this function then treats exactly
    # like a keyword-score miss (return None, fall through to the next
    # asset tier) rather than accepting a semantically wrong pick. See
    # unity_semantic_embed.semantic_filter's own docstring for why an
    # unmeasurable pool (embedder unavailable) instead returns the original
    # list untouched rather than emptying it.
    if semantic_filter_fn is not None and candidates:
        try:
            filtered_models = semantic_filter_fn([m for _s, m in candidates])
        except Exception as exc:  # noqa: BLE001 — semantic gate is advisory-strict,
            # never fatal to the whole resolution attempt
            log.debug("sketchfab semantic_filter_fn(%r) failed: %s", query, exc)
            filtered_models = None
        if filtered_models is not None:
            score_by_uid = {m.get("uid"): s for s, m in candidates}
            candidates = [(score_by_uid.get(m.get("uid"), 0.0), m) for m in filtered_models]
        if not candidates:
            return None

    # Visual quality control on top of the keyword ranking above: a "helmet"
    # search returns everything tagged "helmet" regardless of what KIND, and
    # text scoring can only ever pick the best of that pool, never notice the
    # pool itself is off-target (see unity_sketchfab_vision.py's own
    # docstring for the live regression this closes). Reorders WITHIN the
    # already-min_score-cleared candidates only — never adds one the keyword
    # pass rejected — and any failure (no vision model, no thumbnails, "none
    # match") leaves this exact keyword order untouched.
    if vision_select_fn is not None and len(candidates) > 1:
        try:
            picked_uid = vision_select_fn([m for _s, m in candidates])
        except Exception as exc:  # noqa: BLE001 — vision is advisory, never fatal
            log.debug("sketchfab vision_select_fn(%r) failed: %s", query, exc)
            picked_uid = None
        if picked_uid:
            picked = [sm for sm in candidates if sm[1].get("uid") == picked_uid]
            rest = [sm for sm in candidates if sm[1].get("uid") != picked_uid]
            if picked:
                candidates = picked + rest

    # Try the top-ranked candidates in order — the single best-scoring hit
    # sometimes has a broken archive (no gltf inside) or fails Blender convert;
    # falling through to the next viable candidate turns those single-point
    # misses into a successful, on-concept pick instead of a whole-query
    # failure. Also improves the resolver's per-entity Tier S (same code path).
    from .blender.sketchfab_convert import convert_gltf_to_fbx
    for _cand_score, model in candidates[: max(1, max_candidates)]:
        uid = model.get("uid") or ""
        if not uid:
            continue
        license_slug = client.license_slug(model) or "cc0"
        slug = _slug(model.get("name") or uid)
        dest_dir = cache_dir / category / f"{slug}-{uid}"
        fbx_out = dest_dir / f"{slug}-{uid}.fbx"
        textures_dir = dest_dir / "Textures"

        with tempfile.TemporaryDirectory(prefix="sketchfab_") as tmp:
            tmp_zip = Path(tmp) / f"{uid}.zip"
            if client.download_archive(uid, tmp_zip) is None:
                continue
            raw_dir = Path(tmp) / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(tmp_zip) as zf:
                    zf.extractall(raw_dir)
            except zipfile.BadZipFile:
                continue

            gltf_path = _find_gltf(raw_dir)
            if gltf_path is None:
                continue

            converted = convert_gltf_to_fbx(
                gltf_path, fbx_out, textures_dir, blender_cfg=blender_cfg,
                category=category)
            if converted is None:
                continue
            convert_diag = _report_scale_correction(
                fbx_out, model.get("name") or uid)

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
            # The actual searchable handle (SketchfabClient.search(user=...)) —
            # distinct from the human-readable "author" (displayName) above.
            "author_username": user.get("username") or "",
            "author_url": user.get("profileUrl") or "",
            "attribution": _attribution_text(model, license_slug),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Convert-time size diagnostics (bbox_meters / scale_corrected) —
        # additive keys every catalog reader ignores; see
        # _report_scale_correction.
        entry.update(convert_diag)
        # Style provenance (style_id / style_tokens / style_anchor_*) — merged
        # setdefault-style so caller-supplied metadata can never clobber a core
        # catalog field.
        for k, v in (style_meta or {}).items():
            entry.setdefault(k, v)

        write_sketchfab_entry(entry, Path(lookups_dir) / SKETCHFAB_RELPATH)
        return entry

    return None
