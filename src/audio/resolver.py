"""
Audio cue resolver — the deterministic router (mirrors src/unity_model_resolver +
base_models keyword routing). Two jobs:

  * `classify(category, kind, descriptor)` — decide HOW a cue is produced:
        synth   — DSP archetype exists for it (free, deterministic)
        tts     — spoken voice line
        gen     — organic SFX / music / ambient a model must generate
        library — reuse a pre-existing clip (cache tier)
    `kind` is the LLM's explicit choice; `auto` is resolved here from category +
    descriptor. No LLM, no I/O — pure function.

  * `required_cues(game_type)` / `game_type_from_prompt(prompt)` — the minimum cue
    set a game of that type needs, injected by the fixer so a shooter always has
    `gun_fire/empty_clip/reload` etc. even if the LLM forgot them.
"""

from __future__ import annotations

from .synth import keyword_archetype

ROUTES = ("synth", "gen", "tts", "library")
CATEGORIES = ("music", "sfx", "ambient", "voice")


def classify(category: str, kind: str = "auto", descriptor: str = "") -> str:
    """Route one cue to a production method. Explicit `kind` wins (clamped); `auto`
    is derived from category + whether a DSP archetype matches the descriptor."""
    k = (kind or "auto").strip().lower()
    if k in ROUTES:
        # honour an explicit synth only when an archetype actually exists
        if k == "synth" and keyword_archetype(descriptor) is None:
            return "gen"
        return k
    cat = (category or "sfx").strip().lower()
    if cat == "voice":
        return "tts"
    if cat in ("music", "ambient"):
        return "gen"
    # sfx: synth if an archetype matches, else a model must generate it
    return "synth" if keyword_archetype(descriptor) is not None else "gen"


# ── per game-type required cue sets ───────────────────────────────────────────
# Each entry: (id, category, descriptor). kind is left "auto" -> classify() routes it.
_REQUIRED: dict[str, list[tuple]] = {
    "shooter": [
        ("gun_fire",   "sfx", "sharp gunshot"),
        ("empty_clip", "sfx", "gun clip empty dry fire click"),
        ("reload",     "sfx", "metallic weapon reload clank"),
        ("hit",        "sfx", "bullet impact hit"),
        ("explosion",  "sfx", "explosion blast"),
        ("ui_click",   "sfx", "menu button click"),
    ],
    "platformer": [
        ("jump",     "sfx", "character jump hop"),
        ("coin",     "sfx", "collect a gold coin"),
        ("hit",      "sfx", "take damage hit"),
        ("powerup",  "sfx", "power up upgrade"),
        ("footstep", "sfx", "footstep"),
        ("ui_click", "sfx", "menu button click"),
    ],
    "racing": [
        ("engine",   "sfx", "engine loop rumble"),
        ("skid",     "sfx", "tyre skid screech"),
        ("crash",    "sfx", "metal crash clank"),
        ("ui_click", "sfx", "menu button click"),
    ],
    "puzzle": [
        ("select",  "sfx", "ui select blip"),
        ("place",   "sfx", "piece place click"),
        ("match",   "sfx", "match pickup chime"),
        ("win",     "sfx", "win powerup jingle"),
    ],
    "rpg": [
        ("ui_click",   "sfx", "menu button click"),
        ("pickup",     "sfx", "collect item"),
        ("hit",        "sfx", "sword hit impact"),
        ("level_up",   "sfx", "level up powerup"),
        ("dialogue",   "voice", "npc greeting line"),
        ("ambient",    "ambient", "calm village ambient bed"),
    ],
    "generic": [
        ("ui_click",  "sfx", "menu button click"),
        ("ui_back",   "sfx", "menu back blip"),
        ("pickup",    "sfx", "collect item"),
        ("hit",       "sfx", "impact hit"),
    ],
}

_GAME_TYPE_WORDS = [
    (("shooter", "shoot", "fps", "shoot-em-up", "shoot em up", "gun", "war", "military"), "shooter"),
    (("platformer", "platform", "jump", "metroidvania", "side-scroller", "sidescroller"), "platformer"),
    (("racing", "racer", "kart", "driving", "car game", "rally"), "racing"),
    (("puzzle", "match-3", "match 3", "tetris", "sokoban", "block"), "puzzle"),
    (("rpg", "role-playing", "role playing", "adventure", "dungeon", "quest"), "rpg"),
]


def game_type_from_prompt(prompt: str) -> str:
    """Deterministic prompt -> game type for required-cue injection ('generic' default)."""
    p = (prompt or "").lower()
    for words, gt in _GAME_TYPE_WORDS:
        if any(w in p for w in words):
            return gt
    return "generic"


def required_cues(game_type: str) -> list[dict]:
    """The minimum cue set for a game type, as plain dicts the fixer can add."""
    gt = game_type if game_type in _REQUIRED else "generic"
    return [{"id": cid, "category": cat, "descriptor": desc, "kind": "auto"}
            for (cid, cat, desc) in _REQUIRED[gt]]
