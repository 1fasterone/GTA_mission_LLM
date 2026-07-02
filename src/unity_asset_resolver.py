"""
Kenney asset resolver — keyword-overlap matching (no embeddings needed).
Reads lookups/unity/kenney_catalog.json and scores assets against the schema.
Same architecture as unity_resolver.py: local JSON lookup, word-overlap scoring.
"""

import json
from pathlib import Path
from typing import Optional

from rich.console import Console

from .unity_schema import UnitySchema

console = Console()

# ── Game type → relevant contexts ─────────────────────────────────────────────

_GAME_TYPE_CONTEXTS: dict[str, set[str]] = {
    "2D":        set(),                                     # skip 3D assets
    "2.5D":      {"platformer", "action", "any"},
    "3D":        {"action", "platformer", "racing", "any"},
    "VR":        {"action", "platformer", "any"},
    "top_down":  {"action", "platformer", "racing", "any"},
    "isometric": {"action", "platformer", "puzzle", "any"},
}

# Visual style → boost for kenney assets (all kenney packs are low-poly/colorful)
_STYLE_BOOST: dict[str, float] = {
    "cartoon":   1.3,
    "low_poly":  1.5,   # kenney assets ARE low-poly — perfect match
    "stylized":  1.2,
    "pixel_art": 0.8,   # kenney 3D less relevant for pixel art
    "realistic": 0.6,   # kenney low-poly conflicts with realistic style
    "retro":     1.1,
}


def _load_catalog(lookups_dir: str) -> dict:
    # Prefer the generated full-library catalog (unity-catalog CLI); fall back
    # to the hand-curated kenney catalog so old checkouts keep working.
    for name in ("asset_catalog.json", "kenney_catalog.json"):
        path = Path(lookups_dir) / "unity" / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return {}


def _score_asset(asset: dict, query_words: set[str],
                 valid_contexts: set[str]) -> float:
    """Return a relevance score 0.0–1.0 for a single asset against the query."""
    if not valid_contexts:
        return 0.0

    # Context filter: skip assets with no matching game context
    asset_contexts = set(asset.get("game_contexts", []))
    if not (asset_contexts & valid_contexts):
        return 0.0

    # Keyword overlap
    asset_words = set(kw.lower() for kw in asset.get("keywords", []))
    overlap     = len(query_words & asset_words)
    kw_score    = overlap / max(len(asset_words), 1)

    # Generic score (0–10) as a tiebreaker (normalised to 0.0–0.2)
    generic_bonus = asset.get("generic_score", 0) / 50.0

    return kw_score + generic_bonus


def resolve_assets(schema: UnitySchema,
                   lookups_dir: str = "lookups",
                   source_dir: Optional[str] = None,
                   max_assets: int = 15) -> list[dict]:
    """
    Score all catalog assets against the schema and return the top N.

    Each returned dict has:
      filename, pack, category, description, score, source_path
    """
    catalog = _load_catalog(lookups_dir)
    if not catalog:
        console.print("  [yellow]WARN[/] kenney_catalog.json not found — skipping asset resolution")
        return []

    # Skip 2D projects — all kenney packs are 3D
    if schema.game_type == "2D":
        return []

    cfg            = catalog.get("_resolver_config", {})
    max_assets     = min(max_assets, cfg.get("max_assets_per_project", 15))
    prefer_fmt     = cfg.get("prefer_format", "FBX format")
    src_hint       = source_dir or cfg.get("source_path_hint", "Models")

    valid_contexts = _GAME_TYPE_CONTEXTS.get(schema.game_type, {"any"})
    style_boost    = _STYLE_BOOST.get(schema.visual_style, 1.0)

    # Build query words from schema description + script descriptions + features
    raw_text = " ".join([
        schema.description,
        schema.game_type,
        schema.visual_style,
        *[s.description for s in schema.scripts],
        *schema.features,
    ])
    query_words = set(raw_text.lower().split())

    # Score every asset
    scored: list[tuple[float, dict]] = []
    for asset in catalog.get("assets", []):
        score = _score_asset(asset, query_words, valid_contexts) * style_boost
        # Always include high generic-score assets (universal floor/box/platform)
        if score == 0.0 and asset.get("generic_score", 0) >= cfg.get("generic_score_threshold", 7):
            score = asset["generic_score"] / 100.0  # tiny baseline score
        if score > 0.0:
            asset_with_meta = {**asset,
                               "score":       round(score, 4),
                               "source_path": str(Path(src_hint) / prefer_fmt / asset["filename"])}
            scored.append((score, asset_with_meta))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item for _, item in scored[:max_assets]]

    if results:
        console.print(f"  [green]Assets[/] resolved {len(results)} Kenney assets "
                      f"(top: {results[0]['filename']})")
    return results
