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
# "none" is the POSITIVE declaration that the game has no avatar at all — the
# player acts through a pointer or UI (unity_schema.is_embodied reads it). It is
# scored from its own declaration tier rather than a movement keyword table; see
# _NON_EMBODIED_DECISIVE below.
_VALID_LOCOMOTION = ("walk", "drive", "fly", "swim", "none")


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class UnityIntent:
    game_type:  Optional[str] = None      # None = no confident signal, leave the LLM's choice alone
    locomotion: Optional[str] = None
    genre:      str           = "universal"
    # Control Surface Stack (docs/CONTROL_SURFACE_STACK.md) — 2+ entries only
    # when BOTH a runtime-switch signal AND 2+ distinct kinds were confidently
    # detected; empty means "no opinion", same contract as game_type/locomotion
    # being None. Ordered on_foot-first (if present), then by descending
    # per-kind score — see `_score_multi_mode`.
    locomotion_modes: list[str] = field(default_factory=list)

    game_type_confidence:  int = 0        # raw keyword score — 3+ means a real prompt hit fired
    locomotion_confidence: int = 0
    genre_confidence:      int = 0
    locomotion_modes_confidence: int = 0  # the SWITCH-SIGNAL score, not a per-kind score

    matched: dict[str, list[str]] = field(default_factory=dict)  # axis -> matched keywords, for logging

    def concept_hint(self) -> str:
        """Injected into Step 2 (UNITY_CONCEPT_USER's theme_block slot) — biases the prose concept."""
        parts = []
        if self.game_type:
            parts.append(f"This is a {_GAME_TYPE_LABEL.get(self.game_type, self.game_type)} game.")
        if self.locomotion == "none":
            # Step 3 only ever sees the CONCEPT, never the raw brief, so if the
            # declaration does not survive into the prose here it is gone for
            # good — the brief's own "there is no character" cannot be consulted
            # later. Stated positively and at the same volume as the movement
            # line it replaces.
            parts.append("The player has NO avatar — no character, no vehicle, "
                         "no craft, nothing to move through the world. They act "
                         "entirely through a pointer or UI. Do not give this "
                         "game a body to control.")
        elif self.locomotion and self.locomotion != "walk":
            parts.append(f"The player primarily gets around by {self.locomotion}ing "
                          f"({_LOCOMOTION_LABEL[self.locomotion]}).")
        if self.locomotion_modes and self.locomotion_modes_confidence >= 3:
            # Same "gone for good if it doesn't survive into the prose" logic
            # as locomotion='none' above — Step 3's schema_hints MUST line
            # (structure_hint) is the deterministic backstop regardless, but
            # planting it here too gives Step 3's own reasoning a fighting
            # chance to justify the field instead of just copying an
            # instruction it may not act on.
            kinds = " / ".join(self.locomotion_modes)
            parts.append(f"The player runs a Control Surface Stack: ONE body that "
                          f"switches at runtime between {kinds} — set "
                          f"locomotion_modes to exactly this list.")
        if self.genre != "universal":
            parts.append(f"Genre: {self.genre.replace('_', ' ')}. "
                          f"Lean into what makes a {self.genre.replace('_', ' ')} game feel "
                          f"like one, not a generic action game with reskinned assets.")
        if not parts:
            return ""
        return "PROJECT SHAPE (detected from the prompt, do not contradict it):\n" + "\n".join(
            f"- {p}" for p in parts
        ) + "\n"

    def structure_hint(self, *, min_confidence: int = 3) -> str:
        """Injected into Step 3 (UNITY_SCHEMA_FILL_USER's schema_hints slot) — pins the JSON fields.

        Gated at the same bar `apply_intent` uses. It previously emitted a MUST
        for ANY non-None value, including one reached from a soft concept echo
        worth 1 — so the weakest possible signal issued the strongest possible
        instruction, under a heading that calls itself "not a suggestion".

        Every line now names the words it matched instead of asserting "the
        prompt names it explicitly", which was routinely false: TowerControl was
        told `locomotion MUST be "fly" — the prompt names it explicitly` about a
        brief whose text is "you are never in an aircraft".
        """
        lines = []
        if self.game_type and self.game_type_confidence >= min_confidence:
            hits = ", ".join(self.matched.get("game_type", []))
            lines.append(f'  "game_type" MUST be "{self.game_type}" — matched: {hits}.')
        if self.locomotion == "none" and self.locomotion_confidence >= min_confidence:
            hits = ", ".join(self.matched.get("locomotion", []))
            lines.append(
                '  "locomotion" MUST be "none" — the brief declares the player '
                f'has no avatar (matched: {hits}). Do NOT give this game a '
                'character, vehicle or craft, and do NOT add a player entity: '
                'the player acts through a pointer or UI.')
        elif self.locomotion and self.locomotion_confidence >= min_confidence:
            hits = ", ".join(self.matched.get("locomotion", []))
            lines.append(f'  "locomotion" MUST be "{self.locomotion}" — matched: {hits}.')
        if self.locomotion_modes and self.locomotion_modes_confidence >= min_confidence:
            hits = ", ".join(self.matched.get("locomotion_modes_switch_signal", []))
            kinds = ", ".join(f'"{k}"' for k in self.locomotion_modes)
            lines.append(
                f'  "locomotion_modes" MUST be [{kinds}] — the brief asks the '
                f'player to switch between these locomotion modes at runtime '
                f'(matched: {hits}).')
        if self.genre != "universal":
            lines.append(f'  Bias entities/features/scripts toward a {self.genre.replace("_", " ")} game.')
            req = _GENRE_SCHEMA_REQUIREMENTS.get(self.genre)
            if req:
                lines.append(req)
        if not lines:
            return ""
        return "SCHEMA CONSTRAINTS (deterministically detected, not a suggestion):\n" + "\n".join(lines) + "\n"


# Genre-specific entity requirements, injected into the Step-3 schema prompt
# when that genre is detected. The asset resolver's genre coherence check
# (unity_entity_asset_resolver) hard-fails a strict racing project with no
# vehicle/track content — this guidance makes the LLM author it up front.
_GENRE_SCHEMA_REQUIREMENTS = {
    "racing": (
        '  A racing game MUST include entities for: a drivable vehicle '
        '(kind="vehicle"), the course itself (kind="track", model_hint like '
        '"race track straight"), barriers lining it (kind="wall", model_hint '
        '"barrier"), and checkpoint/finish markers (kind="decoration", '
        'model_hint "banner checkpoint").'
    ),
}

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

# ── Control Surface Stack (multi-mode) catalogue ───────────────────────────────
#
# `locomotion` above is single-winner: exactly one axis value for the whole
# project. A brief can ALSO ask for several of those bodies to coexist on one
# player, runtime-switchable (docs/CONTROL_SURFACE_STACK.md) — a brief this
# module previously had no way to detect at all, so it was 100% LLM-decided at
# Step 3 (src/micro/controller.py's ControllerSchema.modes) with no
# deterministic backstop, unlike every other axis here. Confirmed empirically
# (VectorCore full-surface-control-stack stress prompt, 2 different local
# models, 4 consecutive runs): a brief listing eight switchable locomotion
# modes across several paragraphs did not reliably make the LLM set
# `locomotion_modes`, even with explicit schema-prompt guidance — this is a
# structured-extraction task, the same class of gap every other axis in this
# module exists to backstop.
#
# Reuses _LOCOMOTION's own keyword lists for the two kinds it already scores
# (drive -> vehicle_car, fly -> flying) rather than a second copy that could
# drift out of sync — only on_foot needs its own vocabulary, since
# `_LOCOMOTION` has no "walk" entry (walk is the fallback default, never
# scored there). Labelled with `ControllerModeKind`'s OWN vocabulary
# (src/micro/controller.py) rather than `_LOCOMOTION`'s "drive"/"fly", so a
# detected kind is directly usable as a `locomotion_modes` entry with no
# translation step to keep in sync.
_ON_FOOT_KEYWORDS: list[str] = [
    "on-foot", "on foot", "walking", "parkour", "wall-run", "wall run",
    "vault", "mantle", "melee combat", "hand-to-hand", "third-person combat",
]

_MULTI_MODE_KIND_TABLE: list[tuple[str, list[str]]] = [
    ("on_foot", _ON_FOOT_KEYWORDS),
    ("vehicle_car", next(kws for name, kws in _LOCOMOTION if name == "drive")),
    ("flying", next(kws for name, kws in _LOCOMOTION if name == "fly")),
]

# Phrases signalling the player switches between several of the above AT
# RUNTIME, on one avatar — the thing that actually makes `locomotion_modes`
# apply, as opposed to a single-locomotion brief that just happens to mention
# more than one body in passing (a racing game whose flavour text says "hop
# out and check the engine" is not a Control Surface Stack). Required as an
# independent, conjunctive signal alongside 2+ kind hits — see
# `_score_multi_mode` — specifically so a passing mention of a second body
# can never be enough on its own; strictly opt-in, matching
# docs/CONTROL_SURFACE_STACK.md's "strictly additive" contract.
_MULTI_MODE_SWITCH_SIGNAL: list[str] = [
    "switch between", "switching between", "switches between",
    "get in and out of", "get in and out", "hop in and out",
    "enter and exit a vehicle", "mode manager", "control surface stack",
    "runtime-switchable", "runtime switchable",
    "multiple locomotion modes", "several locomotion modes",
    "multiple control modes", "switch locomotion", "switch modes",
    "transitions between locomotion", "locomotion modes at runtime",
]

# ── Non-embodiment — a DECLARATION, read before the movement vocabulary ───────
#
# `locomotion` had four values and no way to say "this game has no body at all",
# so a brief that spends four paragraphs explaining that the player never moves
# still had to score as one of the four. The axis could not represent the answer,
# which is not a tuning problem — it is a missing value.
#
# Live on prompts/tower_control.txt, an approach-control console whose prose says
# "you are never in an aircraft" and "no character, no vehicle, no camera to
# fly". It classified locomotion='fly' at confidence 6, because "flight" matched
# `flight strips` — paper on a desk — and "aircraft" matched the very sentence
# denying it. Both hints are injected unconditionally into BOTH LLM steps
# (unity_pipeline.run), so Step 2 was told the player gets around by flying and
# Step 3 was told locomotion MUST be "fly", about a game whose entire subject is
# that nothing moves but the pointer.
#
# The `!= "none"` guard in apply_intent cannot help. It protects a schema that
# ALREADY says none from being overridden afterwards; here the steering happens
# first, so `none` is never written for it to protect. Same lesson the UI-first
# layout archetype learned — READ THE DECLARATION BEFORE THE VOCABULARY — applied
# one step upstream, at the point the vocabulary is scored.
#
# DECISIVE phrases can only describe a game with no avatar, so one prompt hit
# carries the bar alone. SUPPORTING phrases are real evidence that also turn up
# in embodied games ("no character customization" in an RPG), so they corroborate
# and never decide by themselves.
_NON_EMBODIED_DECISIVE: list[str] = [
    "no player character", "no playable character", "no player avatar",
    "no avatar", "without an avatar", "there is no player",
    "point and click", "point-and-click",
    "board game", "card game", "word game", "tile-laying",
    "the pointer is the only thing that moves",
    "the only thing that moves is the pointer",
    "you never leave the desk", "never leave your desk",
]

_NON_EMBODIED_SUPPORTING: list[str] = [
    "no character", "no vehicle", "no camera to fly", "no 3d scenery",
    "no scenery", "you are never in", "you never leave",
    "everything you do, you do with the mouse", "entirely with the mouse",
    "entirely through the ui", "the game is the console",
    "teaching sim", "management sim", "puzzle board",
]

# One DECISIVE phrase, or two independent SUPPORTING ones.
_NON_EMBODIED_BAR = 6
_DECISIVE_WEIGHT = 6
_SUPPORTING_WEIGHT = 3


# ── Denial-aware matching ────────────────────────────────────────────────────
#
# A keyword inside an explicit denial is evidence for the OPPOSITE of what it
# scores. "you are never in an aircraft" is the brief saying this is not a flying
# game, and it scored 3 for `fly`. Every table reads through this filter, so a
# denial suppresses what it denies on any axis — "No 3D scenery of any kind"
# stops arguing for game_type='3D'.
#
# Scope is the clause: from the cue to the next boundary, capped so that one "no"
# early in a long sentence cannot silence the rest of a paragraph.
_DENIAL_CUE_RE = re.compile(r"\b(?:no|not|never|without|neither|nor)\b")
_CLAUSE_END_RE = re.compile(r"[.;,:!?—–\n]|\bbut\b")
_DENIAL_REACH = 64


def _denied_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Character ranges of `text` covered by an explicit denial."""
    spans: list[tuple[int, int]] = []
    for cue in _DENIAL_CUE_RE.finditer(text):
        end_m = _CLAUSE_END_RE.search(text, cue.end())
        end = end_m.start() if end_m else len(text)
        spans.append((cue.start(), min(end, cue.start() + _DENIAL_REACH)))
    return tuple(spans)


def _live_match(pat: re.Pattern, text: str,
                spans: tuple[tuple[int, int], ...]) -> bool:
    """Does `pat` match anywhere OUTSIDE a denial?

    A word denied in one sentence and asserted in another still counts — the
    assertion is the live one. Only a keyword whose every occurrence sits inside
    a denial is suppressed.
    """
    for m in pat.finditer(text):
        if not any(lo <= m.start() < hi for lo, hi in spans):
            return True
    return False


def _score_non_embodied(prompt_low: str, concept_low: str) -> tuple[int, list[str]]:
    """Score the explicit declaration that this game has no avatar.

    Deliberately NOT denial-filtered: these phrases ARE denials, and putting them
    through the denial filter would delete the very signal they carry.
    """
    score = 0
    hits: list[str] = []
    for phrases, weight in ((_NON_EMBODIED_DECISIVE, _DECISIVE_WEIGHT),
                            (_NON_EMBODIED_SUPPORTING, _SUPPORTING_WEIGHT)):
        for kw in phrases:
            pat = _kw_pattern(kw)
            if pat.search(prompt_low):
                score += weight
                hits.append(kw)
            elif concept_low and pat.search(concept_low):
                score += 1
                hits.append(kw)
    return score, hits


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
        "drift arcade", "drift race", "drift track", "race track", "racetrack",
        "speedway",
        # "drift arcade" catches PascalCase project titles: _normalize_for_
        # matching splits "DriftArcade" into "drift arcade" before this table
        # runs (hit live: the DriftArcade run classified genre=universal, so
        # zero racing bias reached asset selection → a ring of airplanes).
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
    # Computed once per call, not per keyword — every table reads the same text.
    p_denied = _denied_spans(prompt_low)
    c_denied = _denied_spans(concept_low) if concept_low else ()
    for name, keywords in table:
        score = 0
        hits: list[str] = []
        for kw in keywords:
            pat = _kw_pattern(kw)
            if _live_match(pat, prompt_low, p_denied):
                score += 3
                hits.append(kw)
            elif concept_low and _live_match(pat, concept_low, c_denied):
                score += 1
                hits.append(kw)
        if score > best_score:
            best_name, best_score, best_hits = name, score, hits
    return best_name, best_score, best_hits


def _score_all(prompt_low: str, concept_low: str, table: list[tuple[str, list[str]]]
              ) -> dict[str, tuple[int, list[str]]]:
    """Like `_score`, but every table entry that scores at all is kept, not
    just the winner — the Control Surface Stack table needs to know EVERY
    kind the brief names (on_foot AND vehicle_car AND flying can all be
    live at once), where every other table here answers a single-winner
    "which ONE axis value" question."""
    p_denied = _denied_spans(prompt_low)
    c_denied = _denied_spans(concept_low) if concept_low else ()
    out: dict[str, tuple[int, list[str]]] = {}
    for name, keywords in table:
        score = 0
        hits: list[str] = []
        for kw in keywords:
            pat = _kw_pattern(kw)
            if _live_match(pat, prompt_low, p_denied):
                score += 3
                hits.append(kw)
            elif concept_low and _live_match(pat, concept_low, c_denied):
                score += 1
                hits.append(kw)
        if score:
            out[name] = (score, hits)
    return out


def _score_multi_mode(prompt_low: str, concept_low: str, min_confidence: int
                      ) -> tuple[list[str], int, list[str], dict[str, list[str]]]:
    """Control Surface Stack detection — CONJUNCTIVE, not just a keyword
    count: a runtime-switch signal AND 2+ distinct kinds must each
    independently clear `min_confidence`, so a single-locomotion brief that
    just mentions a second body in passing (flavour text, not a real
    switchable mode) can never trigger this on its own. Either half missing
    means "no opinion" (empty list), the same contract as every other axis.

    Returns (kinds, switch_signal_score, switch_signal_hits, per_kind_hits).
    `kinds` is ordered on_foot-first (if present), then by descending
    per-kind score — a reasonable `default_mode` guess when the brief
    doesn't say which mode the player starts in.
    """
    switch_name, switch_score, switch_hits = _score(
        prompt_low, concept_low, [("multi_mode", _MULTI_MODE_SWITCH_SIGNAL)])
    if switch_score < min_confidence:
        return [], switch_score, switch_hits, {}

    per_kind = _score_all(prompt_low, concept_low, _MULTI_MODE_KIND_TABLE)
    confident = {name: hits for name, (score, hits) in per_kind.items()
                 if score >= min_confidence}
    if len(confident) < 2:
        return [], switch_score, switch_hits, {}

    ordered = sorted(
        (name for name in confident if name != "on_foot"),
        key=lambda name: per_kind[name][0], reverse=True)
    if "on_foot" in confident:
        ordered.insert(0, "on_foot")
    return ordered, switch_score, switch_hits, confident


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

    # THE DECLARATION TIER, read before the movement vocabulary is trusted. A
    # brief that states it has no avatar has answered the locomotion question
    # outright, and no amount of incidental movement vocabulary outranks it.
    #
    # The `>= loco_score` tiebreak keeps this honest in the other direction: a
    # genuine flight sim that mentions a board game once scores 6 here and 12+
    # for `fly`, and stays a flight sim. Only a brief whose declaration is at
    # least as strong as its movement vocabulary is read as non-embodied.
    ne_score, ne_hits = _score_non_embodied(p, c)
    suppressed: list[str] = []
    if ne_score >= _NON_EMBODIED_BAR and ne_score >= loco_score:
        suppressed = loco_hits
        loco_name, loco_score, loco_hits = "none", ne_score, ne_hits

    # Bakes its own min_confidence gate in (3, matching apply_intent's/
    # structure_hint's shared default) rather than returning a raw score for
    # the caller to threshold, unlike every axis above — this is a
    # conjunctive TWO-part gate (switch signal AND 2+ kinds), not a single
    # best-value comparison, so "no opinion" is the empty list itself.
    mm_kinds, mm_score, mm_hits, mm_per_kind = _score_multi_mode(p, c, 3)

    intent = UnityIntent(
        game_type              = gt_name,
        locomotion              = loco_name,
        genre                   = genre_name or "universal",
        locomotion_modes        = mm_kinds,
        game_type_confidence    = gt_score,
        locomotion_confidence    = loco_score,
        genre_confidence         = genre_score,
        locomotion_modes_confidence = mm_score,
    )
    if gt_hits:
        intent.matched["game_type"] = gt_hits
    if loco_hits:
        intent.matched["locomotion"] = loco_hits
    if genre_hits:
        intent.matched["genre"] = genre_hits
    if mm_kinds:
        intent.matched["locomotion_modes"] = mm_kinds
        intent.matched["locomotion_modes_switch_signal"] = mm_hits
        for kind, hits in mm_per_kind.items():
            intent.matched[f"locomotion_modes:{kind}"] = hits
    # Never drop a signal silently — the movement words the declaration outranked
    # are NAMED, so a wrong call is legible in the log instead of invisible.
    if suppressed:
        intent.matched["locomotion_suppressed"] = suppressed
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

    # locomotion="none" is a POSITIVE declaration that the game has no avatar,
    # and it is sticky: the schema-fill step read the whole brief, this
    # classifier reads keywords. A repo-teaching game whose description happens
    # to say "walk through the commit history" must not be flipped back to an
    # embodied game by that one word — it would restore the false NOT PLAYABLE
    # the declaration exists to prevent. Every other value stays overridable.
    if (intent.locomotion and intent.locomotion_confidence >= min_confidence
            and schema.locomotion != intent.locomotion
            and schema.locomotion != "none"):
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

    # Control Surface Stack (docs/CONTROL_SURFACE_STACK.md) — FILL only, never
    # override. Every other field above overwrites a disagreeing LLM answer,
    # but `intent.locomotion_modes` is only ever a fallback for the "Step 3
    # never mentions it at all" case (the gap this exists to close — the LLM
    # left this a same-shape empty list across 4 consecutive live runs, 2
    # different local models, despite explicit schema-prompt guidance). A
    # schema that already carries an explicit registry — even a single entry,
    # even one `_check_multi_mode_registry` will WARN on — keeps it: an
    # author who wrote something here made a real call this classifier's
    # keyword scoring cannot see (per-mode abilities, a specific
    # default_mode, deliberately excluding a kind the brief also mentions).
    if (intent.locomotion_modes and intent.locomotion_modes_confidence >= min_confidence
            and not schema.locomotion_modes):
        schema.locomotion_modes = list(intent.locomotion_modes)
        hits = ", ".join(intent.matched.get("locomotion_modes_switch_signal", []))
        kinds = ", ".join(intent.locomotion_modes)
        msgs.append(
            f"Intent classifier: locomotion_modes [] -> [{kinds}] "
            f"(prompt said: {hits})"
        )

    return msgs
