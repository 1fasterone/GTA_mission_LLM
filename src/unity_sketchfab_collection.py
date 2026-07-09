"""
Concept-driven Sketchfab model collection.

Mirrors the audio cue pipeline (src/audio/resolver.extract_cues -> Stable Audio):
the LLM reads the Step-2 concept and proposes 10-15 concrete 3D models the game
world needs; each is then searched + downloaded + Blender-converted + frozen to
lookups/unity/sketchfab_catalog.json via src/sketchfab_freeze.fetch_and_freeze.

This is a BATCH collection, distinct from the per-entity Tier S in
src/unity_entity_asset_resolver.py: it does not depend on how the LLM happened
to name/kind each schema entity (which is why a "PlayerCar" could resolve to a
Kenney skateboarder). It pre-populates the shared catalog BEFORE generator.write,
so the per-entity resolver's Tier S then has real, on-concept cartoon models to
match against — and the full set is also copied into the project so the files are
visibly written.

Every model logs its outcome (written + where, or failed + why + fallback) so a
run is never a silent "no sketchfab files written". Never raises — a failed
model is logged and skipped so the pipeline always continues.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .unity_asset_catalog import tokenize

_VALID_CATEGORIES = ("vehicle", "character", "prop", "environment",
                     "nature", "building", "weapon", "furniture")

_SYSTEM = """\
You are an art director sourcing REAL 3D models from a marketplace of independent
artists, searched by LITERAL KEYWORD match (not semantic/AI search). Every extra
word narrows results; an abstract/mood word nobody tags ("weathered", "epic",
"sunny") returns ZERO results.

Given a game concept, list the {count} most important physical 3D models this
specific game world needs to look right — its vehicles, characters, key props,
environment pieces, nature. Cover the actual world, not a generic template.

Do TWO things:
  1. Pick ONE shared "style" tag: 1-2 words a real uploader uses — "cartoon",
     "low poly", "stylized", "realistic", "sci-fi", "voxel", "medieval". No mood,
     no color, no commas.
  2. For each model give: "name" (short label), "query" (1-3 concrete words
     naming ONLY the object — "race car", "traffic cone", "grandstand"; no
     adjectives, do NOT repeat the style tag), and "category" (one of:
     vehicle character prop environment nature building weapon furniture).

Rules:
  - style tag + query together must be 4 words or fewer.
  - {count} models, all distinct, no duplicates.
  - Never invent a franchise/IP style; never name an artist.
Return JSON ONLY, no prose, no markdown fences:
{{"style": "...", "models": [{{"name": "...", "query": "...", "category": "..."}}, ...]}}
"""

_USER_TMPL = """\
GAME CONCEPT:
{concept}

List {count} distinct 3D models this world needs. Output JSON only.
"""


def _clean_category(cat: str) -> str:
    c = re.sub(r"[^a-z]", "", str(cat or "").lower())
    return c if c in _VALID_CATEGORIES else "prop"


def extract_model_briefs(concept: str, llm: Any, *, count: int = 12,
                         seed: int = 0) -> tuple[str, list[dict]]:
    """LLM: concept -> (style tag, list of {name, query, category}). Returns
    ("", []) on any failure so callers fall back cleanly."""
    if not concept or llm is None:
        return "", []
    try:
        raw = llm.chat(
            system=_SYSTEM.format(count=count),
            user=_USER_TMPL.format(concept=concept.strip(), count=count),
            temperature=0.4,
            thinking=False,
            seed=seed or None,
        )
    except Exception:  # noqa: BLE001
        return "", []
    try:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return "", []
        payload = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return "", []
    if not isinstance(payload, dict):
        return "", []
    style = str(payload.get("style") or "").strip()
    models = payload.get("models")
    if not isinstance(models, list):
        return "", []

    out: list[dict] = []
    seen: set[str] = set()
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("query") or "").strip()
        query = str(m.get("query") or m.get("name") or "").strip()
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name or query,
            "query": query,
            "category": _clean_category(m.get("category", "prop")),
        })
    return style, out[:count]


def collect_models(concept: str, llm: Any, *, client: Any, sk_cfg: dict,
                   lookups_dir: str = "lookups", count: int = 12, seed: int = 0,
                   log_ok: Optional[Callable[[str], None]] = None,
                   log_warn: Optional[Callable[[str], None]] = None) -> dict:
    """Extract {count} model briefs from `concept`, then search + download +
    convert + freeze each via fetch_and_freeze. Logs every outcome.

    Returns {"style": str, "written": [entry, ...], "failed": [{name, query,
    reason}, ...], "briefs": [...]}. Never raises.
    """
    _ok = log_ok or (lambda _m: None)
    _warn = log_warn or (lambda _m: None)
    result: dict = {"style": "", "written": [], "failed": [], "briefs": []}

    if client is None or not client.available():
        _warn("Sketchfab collection skipped — no API token "
              "(set SKETCHFAB_API_TOKEN or sketchfab.api_token)")
        return result

    style, briefs = extract_model_briefs(concept, llm, count=count, seed=seed)
    result["style"] = style
    result["briefs"] = briefs
    if not briefs:
        _warn("Sketchfab collection: LLM produced no model briefs — "
              "per-entity Tier S / Kenney catalog used instead")
        return result

    from .sketchfab_freeze import fetch_and_freeze

    # Local library index for the concept-aware fallback: when a brief can't be
    # sourced from Sketchfab, the SAME concept query picks the best match from
    # the local catalog (1000+ FBX) instead of leaving the failure to the
    # generic-prop path. Loaded once per run.
    try:
        from .unity_asset_catalog import load_catalog
        _local_assets = [a for a in (load_catalog(lookups_dir).get("assets") or [])
                         if a.get("format") == "fbx" and a.get("source") != "sketchfab"]
    except Exception:  # noqa: BLE001
        _local_assets = []

    def _local_pick(query: str, category: str) -> Optional[dict]:
        qw = set(tokenize(query))
        best, best_s = None, 0.0
        for a in _local_assets:
            kw = set(k.lower() for k in (a.get("keywords") or []))
            kw.update(tokenize(Path(a.get("filename", "")).stem))
            s = float(len(kw & qw))
            if s > 0 and category in (a.get("kind_hints") or []):
                s += 0.5
            if s > best_s:
                best, best_s = a, s
        return best

    _ok(f"Sketchfab collection: {len(briefs)} model briefs from concept "
        f"(style={style!r}) — searching + downloading")

    cache_dir = sk_cfg.get("cache_dir", "Models/Sketchfab")
    min_score = float(sk_cfg.get("min_match_score", 0.0))
    preferred_author: Optional[str] = None

    # Reuse already-frozen models for identical queries — makes re-runs (e.g.
    # after a path/root change) near-instant instead of re-downloading the same
    # models, and keeps the collection idempotent.
    from .unity_asset_catalog import SKETCHFAB_RELPATH
    _cat_path = Path(lookups_dir) / SKETCHFAB_RELPATH
    existing_by_query: dict[str, dict] = {}
    if _cat_path.exists():
        try:
            for ent in (json.loads(_cat_path.read_text(encoding="utf-8")).get("entries") or []):
                q = str(ent.get("query") or "").strip().lower()
                if q and (Path(cache_dir) / str(ent.get("relpath") or "")).is_file():
                    existing_by_query.setdefault(q, ent)
        except Exception:  # noqa: BLE001
            pass

    for i, brief in enumerate(briefs, 1):
        name = brief["name"]
        category = brief["category"]
        query = f"{style} {brief['query']}".strip() if style else brief["query"]
        qw = set(tokenize(query))

        cached = existing_by_query.get(query.strip().lower())
        if cached is not None:
            result["written"].append(cached)
            if preferred_author is None:
                preferred_author = cached.get("author_username") or None
            _ok(f"  SKETCHFAB [{i}/{len(briefs)}] '{name}' (q='{query}') -> "
                f"{cached['relpath']} (cached, reused)")
            continue

        def _score_fn(entry: dict, _qw=qw) -> float:
            return float(len(set(entry.get("keywords") or []) & _qw))

        entry = fetch_and_freeze(
            query, score_fn=_score_fn, client=client, blender_cfg=sk_cfg,
            cache_dir=cache_dir, lookups_dir=lookups_dir, category=category,
            kind_hints=[category], min_score=min_score,
            preferred_author=preferred_author)

        if entry is not None:
            result["written"].append(entry)
            if preferred_author is None:
                preferred_author = entry.get("author_username") or None
            _ok(f"  SKETCHFAB [{i}/{len(briefs)}] '{name}' "
                f"(q='{query}') -> {entry['relpath']} "
                f"(license={entry['license']}, author={entry['author']!r})")
        else:
            # Classify WHY for a transparent log: no results vs off-target
            # results vs download/convert failure.
            try:
                hits = client.filter_licensed(client.search(query, count=12))
            except Exception:  # noqa: BLE001
                hits = []
            def _relevance(m: dict) -> int:
                kw = set(tokenize(m.get("name") or ""))
                kw |= {str(t.get("name", "")).lower()
                       for t in (m.get("tags") or []) if isinstance(t, dict)}
                return len(kw & qw)
            best_rel = max((_relevance(m) for m in hits), default=0)
            if not hits:
                reason = "no CC-licensed marketplace results for this query"
            elif best_rel < 1:
                reason = (f"{len(hits)} results but all off-target "
                          "(marketplace has no matching model for this query)")
            else:
                reason = ("top candidates failed download or Blender-convert "
                          "(check Blender install / network)")
            # Concept-aware LOCAL fallback: the same concept query picks the
            # best match from the local library — never the generic prop.
            local = _local_pick(query, category)
            if local is not None:
                result.setdefault("local", []).append(
                    {"name": name, "query": query, "reason": reason,
                     "filename": local.get("filename"),
                     "relpath": local.get("relpath"), "root": local.get("root")})
                _warn(f"  SKETCHFAB [{i}/{len(briefs)}] '{name}' (q='{query}') "
                      f"FAILED: {reason}")
                _ok(f"  LOCAL    [{i}/{len(briefs)}] '{name}' -> "
                    f"{local.get('root','?')}/{local.get('relpath', local.get('filename','?'))} "
                    f"(concept-matched from local library)")
            else:
                result["failed"].append({"name": name, "query": query, "reason": reason})
                _warn(f"  SKETCHFAB [{i}/{len(briefs)}] '{name}' (q='{query}') "
                      f"FAILED: {reason} — no local concept match either "
                      f"(EMPTY: nothing placed for this brief)")

    n_local = len(result.get("local", []))
    _ok(f"Sketchfab collection done: {len(result['written'])} downloaded, "
        f"{n_local} local-library fallback(s), {len(result['failed'])} unfilled "
        f"(of {len(briefs)})")
    return result


def _short_name(fbx_stem: str, uid: str) -> str:
    """A compact folder/file name for a copied model — the cache stem is
    `<slug>-<32hexuid>` which, nested + doubled, blows past Windows MAX_PATH.
    Truncate the slug to 18 chars + a 6-char uid tag: `<slug18>-<uid6>`."""
    slug = re.sub(r"-[0-9a-f]{16,}$", "", fbx_stem)[:18].strip("-") or "model"
    tag = (uid or "")[:6]
    return f"{slug}-{tag}" if tag else slug


def copy_collection_into_project(entries: list[dict], cache_dir: str | Path,
                                 project_assets: Path,
                                 log_ok: Optional[Callable[[str], None]] = None) -> int:
    """Copy each collected model (FBX + Textures/) into the project's
    Assets/Models/Sketchfab/<category>/<short>/ so the files are visibly written
    even if no entity matched them. Uses a SHORTENED name (cache stems are
    slug+32hex-uid, which doubled + nested overflow Windows MAX_PATH). Textures
    are referenced relatively by the FBX, so renaming the FBX is safe. Returns
    the count copied; Unity generates .meta on import."""
    import shutil
    _ok = log_ok or (lambda _m: None)
    cache_dir = Path(cache_dir)
    dst_root = Path(project_assets) / "Models" / "Sketchfab"
    copied = 0
    for e in entries:
        rel = e.get("relpath")
        if not rel:
            continue
        src_fbx = cache_dir / rel
        src_dir = src_fbx.parent
        if not src_fbx.is_file():
            continue
        short = _short_name(src_fbx.stem, str(e.get("sketchfab_uid") or ""))
        dst_dir = dst_root / e.get("category", "prop") / short
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_fbx, dst_dir / f"{short}.fbx")
            tex = src_dir / "Textures"
            if tex.is_dir():
                shutil.copytree(tex, dst_dir / "Textures", dirs_exist_ok=True)
            copied += 1
        except Exception:  # noqa: BLE001
            continue
    if copied:
        _ok(f"Sketchfab collection: {copied} model folder(s) copied -> "
            f"Assets/Models/Sketchfab/")
    return copied
