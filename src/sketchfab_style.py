"""
Deterministic visual-style extraction + per-run style session for the
Sketchfab model source (see docs/model-pipeline-architecture.md).

Two pieces:

* resolve_style() — a pure keyword-scoring classifier, same 3x-prompt /
  1x-concept weighting and table-order tie-breaking as
  src/unity_archetypes.py. Maps free-text concept/prompt to a `style_id`
  plus the Sketchfab query tokens that style implies. No LLM, no network —
  same input, same output, forever.

* SketchfabStyleSession — one instance per generation run, owned by the
  resolver's Tier S. Locks onto the first successful fetch (the "anchor")
  and biases every later candidate toward it: tag-Jaccard similarity
  against the anchor's keyword set (large weight) plus a same-author bonus
  (smaller weight — some Sketchfab accounts are aggregators/rippers with
  wildly inconsistent catalogs, so same-author is a *bonus*, never a hard
  filter). This is what makes downloaded models similar to EACH OTHER, not
  merely each independently similar to the concept.

Determinism note: the anchor makes a run order-dependent, and Sketchfab's
own ranking drifts over time — that's fine, all of it sits on the
nondeterministic side of the frozen line. provenance() is stamped into
every frozen catalog entry (see sketchfab_freeze.fetch_and_freeze's
`style_meta`) so replays read identical data from
lookups/unity/sketchfab_catalog.json regardless of what Sketchfab would
return today. For the best anchor, resolve the most style-defining entity
first (player character / largest landmark), not schema emission order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Style catalogue ────────────────────────────────────────────────────────────
# (style_id, concept/prompt trigger keywords, sketchfab query tokens).
# Order matters only for ties — first entry with the max score wins, same
# tie-break contract as unity_archetypes.py. Query tokens go into the
# free-text `q` (creators tag lowpoly/stylized/cartoon religiously, and
# free-text matches tags) — deliberately NOT the strict `tags=` API param,
# which requires the exact tag to exist on the model and over-restricts
# into constant Kenney fallbacks. Strict tags stay available via
# SketchfabClient.search(tags=...) for config-driven use.

_STYLE: list[tuple[str, list[str], list[str]]] = [
    ("low_poly",  ["low poly", "low-poly", "lowpoly", "flat shaded", "flat-shaded",
                   "minimalist", "faceted"],
                  ["low poly", "lowpoly"]),
    ("cartoon",   ["cartoon", "toon", "cel shaded", "cel-shaded", "cel shading",
                   "cartoony", "cute", "casual", "kids", "colorful", "cartoon colors",
                   "vibrant colors", "bright colors"],
                  ["cartoon", "stylized", "toon"]),
    ("stylized",  ["stylized", "hand painted", "hand-painted", "handpainted",
                   "fantasy art", "wow style", "fortnite style", "painterly"],
                  ["stylized", "handpainted"]),
    ("realistic", ["realistic", "photorealistic", "photoreal", "pbr", "gritty",
                   "grounded", "photogrammetry", "scan", "scanned", "lifelike"],
                  ["realistic", "pbr", "game ready"]),
    ("voxel",     ["voxel", "minecraft", "blocky", "cube world", "cubic"],
                  ["voxel"]),
    ("pixel",     ["pixel art", "retro 3d", "ps1", "psx", "n64", "low res retro",
                   "playstation 1"],
                  ["psx", "retro", "low poly"]),
]

DEFAULT_STYLE_ID = "stylized"
DEFAULT_STYLE_TOKENS = ("stylized",)

# API-level structural constraints per style — face count is the cheapest
# reliable proxy for visual class in the search metadata. max_face_count on
# the toon-ish styles structurally excludes photogrammetry scans from the
# candidate pool BEFORE ranking happens; min_face_count on `realistic` does
# the inverse (keeps 300-triangle prototype props out of a gritty scene).
# Bounds err loose: a wrongly-excluded good model degrades to Kenney (fine),
# but the bound exists to kill the million-face raw-scan class, not to
# fine-tune poly budgets.
_STYLE_SEARCH_PARAMS: dict[str, dict[str, int]] = {
    "low_poly":  {"max_face_count": 20_000},
    "cartoon":   {"max_face_count": 40_000},
    "voxel":     {"max_face_count": 20_000},
    "pixel":     {"max_face_count": 15_000},
    "stylized":  {"max_face_count": 150_000},
    "realistic": {"min_face_count": 15_000},
}

# Word-boundary matching, not bare substring — bare `"scan" in text` would
# fire on "scandinavian village". Multi-word triggers work too because
# spaces are non-alphanumeric boundaries.
def _hits(text: str, keywords: list[str]) -> int:
    t = (text or "").lower()
    n = 0
    for k in keywords:
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
            n += 1
    return n


def resolve_style(concept: str, prompt: str = "") -> tuple[str, list[str]]:
    """Deterministically classify the visual style of a generation run.

    Scoring: 3x per trigger hit in `prompt`, 1x per hit in `concept`
    (mirrors unity_archetypes.py — direct prompt wording outranks the LLM's
    expanded concept text). Ties break by table order; zero hits everywhere
    falls back to ("stylized", ["stylized"]) — a neutral-but-coherent
    default beats no constraint for game-gen sourcing.
    """
    scored = [(3 * _hits(prompt, trig) + _hits(concept, trig), sid, toks)
              for sid, trig, toks in _STYLE]
    best = max(scored, key=lambda x: x[0])  # max() keeps first on ties
    if best[0] <= 0:
        return DEFAULT_STYLE_ID, list(DEFAULT_STYLE_TOKENS)
    return best[1], list(best[2])


def search_params_for(style_id: str) -> dict:
    """API constraints (face-count bounds) for SketchfabClient.search()."""
    return dict(_STYLE_SEARCH_PARAMS.get(style_id, {}))


@dataclass
class SketchfabStyleSession:
    """Per-generation-run style lock for the resolver's Sketchfab tier.

    Lifecycle (all in src/unity_entity_asset_resolver.py):

        style_id, tokens = resolve_style(concept, prompt)   # once per run
        session = SketchfabStyleSession(style_id, tokens)

        # per entity:
        entry = fetch_and_freeze(
            session.query(entity_query),
            session.wrap_score(lambda a: _score(a, qw, kind)),
            client=client,
            preferred_author=session.anchor_author,   # None until anchored
            search_params=session.search_params(),
            style_meta=session.provenance(),
            min_score=2.0,   # NOT 0.0 — an off-style import is worse than
            ...)             # a Kenney fallback; fail closed on style.
        if entry:
            session.observe(entry)
    """

    style_id: str
    style_tokens: list[str]
    anchor_author: Optional[str] = None   # `username` — feeds preferred_author
    anchor_uid: Optional[str] = None
    anchor_tags: frozenset = field(default_factory=frozenset)
    # Tag similarity outweighs same-author on purpose — the anchor's tag set
    # is the trustworthy style signal; authorship is only a cohesion nudge.
    tag_weight: float = 3.0
    author_weight: float = 2.0

    def query(self, base_query: str) -> str:
        """Append the run's style tokens to an entity query —
        "knight" -> "knight cartoon stylized"."""
        extra = " ".join(t for t in self.style_tokens if t)
        return f"{base_query} {extra}".strip() if extra else base_query

    def search_params(self) -> dict:
        return search_params_for(self.style_id)

    def observe(self, entry: dict) -> None:
        """Record the first successful frozen entry as the run's anchor.
        Later calls are no-ops — the lock is sticky for the whole run."""
        if self.anchor_uid is not None:
            return
        self.anchor_uid = entry.get("sketchfab_uid") or None
        self.anchor_author = entry.get("author_username") or None
        self.anchor_tags = frozenset(entry.get("keywords") or [])

    def wrap_score(self, base_fn: Callable[[dict], float]) -> Callable[[dict], float]:
        """Style-aware scorer over the resolver's own semantic scorer.

        score = base + tag_weight * jaccard(anchor_tags, candidate_keywords)
                     + author_weight * (candidate author == anchor author)

        Pre-anchor (first entity) this is exactly base_fn — the anchor
        itself is chosen on semantic match + the query's style tokens +
        face-count bounds alone. Deterministic given the same anchor.
        Only wrap the score for Sketchfab candidates; the Kenney catalog
        keeps the unwrapped scorer (it's internally coherent already, and
        its entries carry no `_model`).
        """
        def scored(entry: dict) -> float:
            s = float(base_fn(entry))
            if self.anchor_tags:
                cand = set(entry.get("keywords") or [])
                union = len(cand | self.anchor_tags)
                if union:
                    s += self.tag_weight * (len(cand & self.anchor_tags) / union)
            if self.anchor_author:
                model = entry.get("_model") or {}
                user = (model.get("user") or {}).get("username")
                if user == self.anchor_author:
                    s += self.author_weight
            return s
        return scored

    def provenance(self) -> dict:
        """Fields frozen into each catalog entry so a replay can reconstruct
        why this model was chosen without re-touching the network."""
        return {
            "style_id": self.style_id,
            "style_tokens": list(self.style_tokens),
            "style_anchor_uid": self.anchor_uid or "",
            "style_anchor_author": self.anchor_author or "",
        }
