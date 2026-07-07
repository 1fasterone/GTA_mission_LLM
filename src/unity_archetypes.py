"""
Unity intent classifier — the "sub-game" structure layer, ported from archetypes.py.

archetypes.py shapes GTA5 mission *structure* (race/heist/dive/fly/...). This
module shapes Unity project *shape* — which axis of "what kind of game is this"
currently has no deterministic backstop at all:

  game_type   (2D / 2.5D / 3D / VR / top_down / isometric)   — unity_schema.GameType
  locomotion  (walk / drive / fly / swim)                     — unity_schema.UnitySchema.locomotion
  genre       (fps / rpg / platformer / horror / racing / ...) — rag.tags.taxonomy.UNITY_GENRE_ATOM["genre"]

Today all three are 100% LLM-decided at Step 3. unity_schema's own validators
silently fall back to "3D" / "walk" on anything they can't parse, which means a
misread vibe doesn't error — it silently produces a structurally valid project
for the wrong game, and subsystems_for() (unity_compose.py) then deterministically
and confidently builds the WRONG controller/camera/physics-body for it.

This module closes that gap the same way archetypes.py does: score the raw
prompt against keyword catalogues (prompt hit = strong signal, concept hit =
soft signal — same 3x/1x weighting as detect_archetype), then either bias the
Step 2/3 prompts toward the detected shape, or — if the LLM's own output
disagrees with a HIGH-CONFIDENCE detection — override it outright and log why.

genre reuses the EXACT vocabulary already tagged onto every entry in
rag/scrapers/unity_genre_atoms.py (UNITY_GENRE_ATOM["genre"]) rather than
inventing a second taxonomy, so schema.meta["genre"] is immediately usable to
filter genre_atom RAG queries at Step 4 once that wiring lands — nothing in
src/ currently reads that tag, this is what should feed it.

Known follow-up (not fixed here): subsystems_for()'s controller-selection dict
has no branch for genre=="fps" — game_type="3D" always falls through to
"third_person", even for a first-person shooter prompt. Once genre flows
through schema.meta, that dict needs a genre-aware fps -> "first_person" case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

# Single source of truth for the genre vocabulary — matches every entry in
# rag/scrapers/unity_genre_atoms.py exactly, so a detected genre is always a
# valid filter value against that corpus. No second taxonomy to drift out of sync.
try:
    from rag.tags.taxonomy import UNITY_GENRE_ATOM
    _VALID_GENRES: tuple[str, ...] = tuple(UNITY_GENRE_ATOM["genre"])
except ImportError:  # pragma: no cover - keeps this importable if rag/ isn't on path
    _VALID_GENRES = (
        "universal", "fps", "rpg", "platformer", "horror",
        "racing", "tower_defence", "puzzle", "stealth", "strategy",
    )

_VALID_GAME_TYPES = ("2D", "2.5D", "3D", "VR", "top_down", "isometric")
_VALID_LOCOMOTION = ("walk", "drive", "fly", "swim")


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class UnityIntent:
    game_type:  Optional[str] = None      # None = no confident signal, leave the LLM's choice alone
    locomotion: Optional[str] = None
    genre:      str           = "universal"

    game_type_confidence:  int = 0        # raw keyword score — 3+ means a real prompt hit fired
    locomotion_confidence: int = 0
    genre_confidence:      int = 0

    matched: dict[str, list[str]] = field(default_factory=dict)  # axis -> matched keywords, for logging

    def concept_hint(self) -> str:
        """Injected into Step 2 (UNITY_CONCEPT_USER's theme_block slot) — biases the prose concept."""
        parts = []
        if self.game_type:
            parts.append(f"This is a {_GAME_TYPE_LABEL.get(self.game_type, self.game_type)} game.")
        if self.locomotion and self.locomotion != "walk":
            parts.append(f"The player primarily gets around by {self.locomotion}ing "
                          f"({_LOCOMOTION_LABEL[self.locomotion]}).")
        if self.genre != "universal":
            parts.append(f"Genre: {self.genre.replace('_', ' ')}. "
                          f"Lean into what makes a {self.genre.replace('_', ' ')} game feel "
                          f"like one, not a generic action game with reskinned assets.")
        if not parts:
            return ""
        return "PROJECT SHAPE (detected from the prompt, do not contradict it):\n" + "\n".join(
            f"- {p}" for p in parts
        ) + "\n"

    def structure_hint(self) -> str:
        """Injected into Step 3 (UNITY_SCHEMA_FILL_USER's schema_hints slot) — pins the JSON fields."""
        lines = []
        if self.game_type:
            lines.append(f'  "game_type" MUST be "{self.game_type}" — the prompt names it explicitly.')
        if self.locomotion:
            lines.append(f'  "locomotion" MUST be "{self.locomotion}" — the prompt names it explicitly.')
        if self.genre != "universal":
            lines.append(f'  Bias entities/features/scripts toward a {self.genre.replace("_", " ")} game.')
        if not lines:
            return ""
        return "SCHEMA CONSTRAINTS (deterministically detected, not a suggestion):\n" + "\n".join(lines) + "\n"


_GAME_TYPE_LABEL = {
    "2D": "2D side-scrolling", "2.5D": "2.5D (3D models, 2D gameplay plane)",
    "3D": "fully 3D", "VR": "VR", "top_down": "top-down (real 3D models)",
    "2d_topdown": "2D top-down (flat sprites/physics)", "isometric": "isometric",
}
_LOCOMOTION_LABEL = {
    "drive": "a vehicle", "fly": "an aircraft", "swim": "swimming/diving", "walk": "on foot",
}


# ── Perspective / dimension catalogue → GameType ───────────────────────────────
# Order doesn't matter — every entry is scored, highest wins.

_PERSPECTIVE: list[tuple[str, list[str]]] = [
    # "2d_topdown" is listed BEFORE "top_down" deliberately: _score() only
    # overwrites best_name on a STRICT >, so on an exact tie (a prompt hitting
    # one keyword from each, e.g. "2D zelda-like top-down adventure") whichever
    # entry is scored first keeps the win. 2d_topdown's keywords are the more
    # specific (2D-qualified) signal, so it should win ties against the
    # dimension-neutral top_down phrases below — not the other way around.
    ("2d_topdown", [
        "twin-stick", "twin stick", "zelda-like", "zelda-style", "bullet heaven",
    ]),
    ("top_down", [
        # Dimension-neutral phrases only — could describe a real-3D dungeon
        # crawler just as easily as a flat 2D game, so these default to the
        # existing 3D top_down archetype. Clearly-2D-coded genre words
        # (Zelda-like, bullet heaven, twin-stick) live under "2d_topdown"
        # above instead — those are almost always flat sprite games in
        # practice, and bundling them here would have silently routed a
        # "2D zelda-like" prompt to the 3D archetype.
        "top-down", "top down", "topdown", "overhead view", "bird's eye", "birds eye view",
    ]),
    ("isometric", [
        "isometric", "iso view", "diablo-like", "diablo-style", "isometric rpg", "isometric strategy",
    ]),
    ("2D", [
        "side-scroller", "sidescroller", "side scrolling", "side-scrolling", "2d platformer",
        "platformer", "metroidvania", "run and jump", "jump and run", "mario-like", "mario-style",
        "pixel platformer", "2d game", "flat 2d",
    ]),
    ("2.5D", [
        "2.5d", "2.5-d", "parallax", "3d models with 2d gameplay", "paper mario",
        "side view with 3d models", "2.5 dimensional",
    ]),
    ("VR", [
        "vr", "virtual reality", "oculus", "meta quest", "quest headset", "vive", "room-scale",
        "room scale", "vr headset",
    ]),
    ("3D", [
        "third person", "third-person", "over the shoulder", "open world", "open-world",
        "action adventure", "3d game", "fully 3d",
    ]),
]

# ── Locomotion catalogue (independent axis) ────────────────────────────────────

_LOCOMOTION: list[tuple[str, list[str]]] = [
    ("drive", [
        "driving", "drive", "car", "vehicle", "racing game", "rally", "street racer", "kart",
        "trucking", "vehicular combat",
    ]),
    ("fly", [
        "flying", "flight", "plane", "aircraft", "jet", "dogfight", "spaceship", "starfighter",
        "aerial combat", "flight sim",
    ]),
    ("swim", [
        "swimming", "underwater", "diving", "scuba", "submarine", "ocean exploration", "deep sea",
    ]),
]

# ── Gameplay genre catalogue → matches UNITY_GENRE_ATOM["genre"] exactly ───────

_GENRE: list[tuple[str, list[str]]] = [
    ("fps", [
        "first person shooter", "first-person shooter", "fps", "doom-like", "doom-style",
        "run and gun", "boomer shooter", "arena shooter", "gunplay",
    ]),
    ("rpg", [
        "rpg", "role playing", "role-playing", "jrpg", "action rpg", "loot and level",
        "skill tree", "open world rpg", "party-based", "party based", "dungeon crawler",
    ]),
    ("platformer", [
        "platformer", "platforming", "metroidvania", "jump and run", "precision platformer",
        "side-scroller",
    ]),
    ("horror", [
        "horror", "survival horror", "scary", "creepy", "haunted", "zombie survival",
        "psychological horror", "jump scare", "found footage",
    ]),
    ("racing", [
        "racing", "race", "street racer", "rally", "time trial", "circuit racer",
        "kart racer", "grand prix", "drift racing", "drift car", "power slide",
        # NOTE: bare "drift" AND "drifting" are deliberately excluded — they
        # collide with non-racing physics/mood language ("buoyancy drift",
        # "camera drift", "drifting clouds") and flipped prompts to
        # genre=racing on a single ambiguous word twice (hit live: DeepDive's
        # "drift", then TerminalHorizon's "drifting clouds" — which steered
        # the whole concept toward a racing game via concept_hint()).
    ]),
    ("tower_defence", [
        "tower defense", "tower defence", "td game", "wave defense", "base defense",
    ]),
    ("puzzle", [
        "puzzle", "match-3", "match 3", "sokoban", "physics puzzle", "brain teaser", "logic game",
    ]),
    ("stealth", [
        "stealth", "sneak", "infiltration", "hitman-like", "hitman-style", "undetected",
        "sneaking mission",
    ]),
    ("strategy", [
        "strategy", "rts", "real-time strategy", "turn-based", "turn based", "4x game", "tactics",
        "grand strategy",
    ]),
]


# ── Scoring ──────────────────────────────────────────────────────────────────

# Word-boundary wrapper so a keyword only matches whole words/phrases — a plain
# substring `kw in text` check let short keywords match inside unrelated words
# (hit live: the VR keyword "vive" matched inside "survive", flipping a plain
# 3D underwater-survival prompt to game_type=VR). \b already treats the space
# inside multi-word keywords ("top down") correctly since a space is a
# non-word character, so this needs no special-casing for phrases.
@lru_cache(maxsize=None)
def _kw_pattern(kw: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(kw) + r"\b")


# Structured prompts often list script/class names in PascalCase
# ("SwimmingController", "O2Manager") rather than prose — split those compound
# identifiers into separate words BEFORE lowercasing (lowercasing first would
# destroy the case-transition signal a boundary split depends on), so a
# keyword embedded in one still matches \b-bounded instead of being swallowed
# by having no real word boundary after it ("swimming" glued to "Controller").
#
# The lookahead requires the split-triggering capital to be followed by a
# lowercase letter (i.e. it starts a new word, "Manager" / "Controller") —
# not just any capital. That's what tells "O2Manager" (M followed by 'a',
# starts a word) apart from literal dimension notation like "2D"/"3D"/"2.5D"
# (D at a word boundary, followed by space/punctuation/EOS, not a word) —
# without this, "2.5D" was getting mangled into "2.5 D" and silently losing
# every "2.5d"/"2d"/"3d" keyword match in the perspective table.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z][a-z])")


def _normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    return _CAMEL_BOUNDARY_RE.sub(" ", text).lower()


def _score(prompt_low: str, concept_low: str, table: list[tuple[str, list[str]]]
           ) -> tuple[Optional[str], int, list[str]]:
    """Same weighting as archetypes.detect_archetype: prompt hit = 3, concept hit = 1."""
    best_name: Optional[str] = None
    best_score = 0
    best_hits: list[str] = []
    for name, keywords in table:
        score = 0
        hits: list[str] = []
        for kw in keywords:
            pat = _kw_pattern(kw)
            if pat.search(prompt_low):
                score += 3
                hits.append(kw)
            elif concept_low and pat.search(concept_low):
                score += 1
                hits.append(kw)
        if score > best_score:
            best_name, best_score, best_hits = name, score, hits
    return best_name, best_score, best_hits


def classify(prompt: str, concept: str = "") -> UnityIntent:
    """
    Detect game_type + locomotion + genre from a vibe prompt (and optionally the
    Step-2 concept text, for a second pass with softer weight). Pure function,
    no LLM, no I/O — mirrors archetypes.detect_archetype exactly.

    Any axis with zero signal comes back None (game_type/locomotion) or
    "universal" (genre) — meaning "no opinion", not "force this value". Callers
    decide what confidence bar earns an override; see apply_intent below.
    """
    p = _normalize_for_matching(prompt)
    c = _normalize_for_matching(concept)

    gt_name,   gt_score,   gt_hits   = _score(p, c, _PERSPECTIVE)
    loco_name, loco_score, loco_hits = _score(p, c, _LOCOMOTION)
    genre_name, genre_score, genre_hits = _score(p, c, _GENRE)

    intent = UnityIntent(
        game_type              = gt_name,
        locomotion              = loco_name,
        genre                   = genre_name or "universal",
        game_type_confidence    = gt_score,
        locomotion_confidence    = loco_score,
        genre_confidence         = genre_score,
    )
    if gt_hits:
        intent.matched["game_type"] = gt_hits
    if loco_hits:
        intent.matched["locomotion"] = loco_hits
    if genre_hits:
        intent.matched["genre"] = genre_hits
    return intent


# ── Override ─────────────────────────────────────────────────────────────────

def apply_intent(schema, intent: UnityIntent, *, min_confidence: int = 3) -> list[str]:
    """
    Called after Step 3 fills the schema. Only overrides a field when the
    classifier's confidence is at least `min_confidence` (default 3 = one direct
    prompt keyword hit, not just a soft concept echo) AND the LLM's own value
    disagrees. genre is always written to schema.meta regardless of confidence
    (default "universal") so Step 4 can filter genre_atom RAG queries by it —
    that wiring is a separate follow-up, but the value needs to exist first.

    Returns log messages in the same style as archetypes.apply_scaffold, meant
    to be printed via self._fix(msg) in unity_pipeline.run().
    """
    msgs: list[str] = []

    if (intent.game_type and intent.game_type_confidence >= min_confidence
            and schema.game_type != intent.game_type):
        old = schema.game_type
        schema.game_type = intent.game_type
        hits = ", ".join(intent.matched.get("game_type", []))
        msgs.append(
            f"Intent classifier: game_type '{old}' -> '{intent.game_type}' "
            f"(prompt said: {hits})"
        )

    if (intent.locomotion and intent.locomotion_confidence >= min_confidence
            and schema.locomotion != intent.locomotion):
        old = schema.locomotion
        schema.locomotion = intent.locomotion
        hits = ", ".join(intent.matched.get("locomotion", []))
        msgs.append(
            f"Intent classifier: locomotion '{old}' -> '{intent.locomotion}' "
            f"(prompt said: {hits})"
        )

    schema.meta["genre"] = intent.genre
    if intent.genre_confidence >= min_confidence:
        hits = ", ".join(intent.matched.get("genre", []))
        msgs.append(f"Intent classifier: genre = '{intent.genre}' (prompt said: {hits})")

    return msgs
