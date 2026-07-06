"""
Audio cue resolver — two responsibilities:

  classify(category, kind, descriptor)
      Route a single cue to a production method (synth / gen / tts / library).
      Pure function, no I/O.

  extract_cues(concept, llm, ...)
      Primary path: ask the LLM to read the concept and extract diegetic sound
      sources from the world's own nouns, verbs, and setting. The setting is the
      source of truth — no genre lookup, no preset list. An underwater harpoon
      launcher produces a muffled pneumatic hiss and a bubble trail, not "gun_fire".

  required_cues(game_type)  [fallback only]
      Used when no LLM is available (--no-llm path). Returns a minimal genre
      preset so the bank is never empty. game_type_from_prompt() supports it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .synth import keyword_archetype

ROUTES     = ("synth", "gen", "tts", "library")
CATEGORIES = ("music", "sfx", "ambient", "voice")


# ── Cue router ────────────────────────────────────────────────────────────────

def classify(category: str, kind: str = "auto", descriptor: str = "") -> str:
    """Route one cue to a production method. Explicit `kind` wins (clamped);
    `auto` is derived from category + whether a DSP archetype matches."""
    k = (kind or "auto").strip().lower()
    if k in ROUTES:
        if k == "synth" and keyword_archetype(descriptor) is None:
            return "gen"
        return k
    cat = (category or "sfx").strip().lower()
    if cat == "voice":
        return "tts"
    if cat in ("music", "ambient"):
        return "gen"
    return "synth" if keyword_archetype(descriptor) is not None else "gen"


# ── Concept-driven cue extraction (primary path) ──────────────────────────────

_EXTRACT_SYSTEM = """\
You extract concrete diegetic sound events from a game concept.

Rules:
- Only sounds that physically occur in the world described. No genre clichés,
  no abstract labels (do not output bare "gunfire", "hit_sfx", "ui_click",
  "explosion" unless the concept world explicitly produces that exact event).
- Every cue is grounded in the concept's specific nouns, verbs, setting, and
  medium. If the world is underwater, sounds are muffled, bubbled, pressured —
  never dry. If the world is a forest, there is no engine rumble unless a
  vehicle exists in that forest.
- Each description is a vivid Stable Audio 3 prompt: name the material, the
  physical medium, the mechanism. 8–20 words.
- Return 8 to 12 cues, no duplicates.

Return JSON only — a list of objects, no prose, no markdown fences:
[{"source": "<diegetic object or agent>", "description": "<SA3 prompt>"}, ...]
"""

# Words that imply sustained / looping playback.
_LOOP_WORDS = frozenset({
    "ambient", "drone", "hum", "bed", "loop", "looping", "continuous",
    "background", "sustained", "constant", "stream", "flow", "rumble",
    "atmosphere", "wash", "pad",
})
# Words that imply music rather than sfx/ambient.
_MUSIC_WORDS = frozenset({
    "music", "score", "theme", "melody", "beat", "sting", "fanfare",
    "jingle", "orchestral", "percussion", "soundtrack", "track",
})
# Words that imply ambient rather than sfx (when not music).
_AMBIENT_WORDS = frozenset({
    "ambient", "drone", "hum", "bed", "atmosphere", "environmental",
    "background", "room", "cavern", "wind", "rain", "ocean", "water",
    "bubbles", "creak", "groan", "rumble", "pressure",
})


def _source_to_id(source: str) -> str:
    """Convert a diegetic source label to a snake_case cue id."""
    return re.sub(r"[^a-z0-9]+", "_", source.strip().lower()).strip("_")[:40] or "cue"


def _infer_category_loop(source: str, desc: str) -> tuple[str, bool]:
    """Infer category (sfx/ambient/music) and loop flag from source + description."""
    text  = (source + " " + desc).lower()
    words = set(re.findall(r"\w+", text))
    if words & _MUSIC_WORDS:
        return "music", bool({"loop", "looping", "continuous", "bed"} & words)
    if words & _AMBIENT_WORDS:
        return "ambient", True
    return "sfx", False


# ── Gameplay-seam role matching (deterministic, no LLM) ───────────────────────
#
# Concept-driven cues carry no semantic-role tag (just id/category/descriptor) —
# a wiring seam that needs a "player took damage" sound can't assume one exists
# under a fixed generic name. match_cue_for_role() does a deterministic keyword
# match against the cues this project actually resolved; callers must treat a
# None result as "no matching cue" and omit the seam line, never fabricate an
# id that isn't in the resolved bank.

_ROLE_KEYWORDS: dict[str, frozenset[str]] = {
    "hurt":  frozenset({"hurt", "damage", "injur", "pain", "hit", "impact", "wound"}),
    "death": frozenset({"death", "die", "dying", "destroy", "shatter", "break",
                         "kill", "collapse"}),
    "heal":  frozenset({"heal", "recover", "restore", "repair", "regenerat",
                         "save", "checkpoint"}),
}


def match_cue_for_role(role: str, cues: list[dict]) -> str | None:
    """First cue (in existing order) whose id/descriptor contains a keyword for
    `role` ("hurt" | "death" | "heal"). None if no cue matches — never invents
    an id that isn't actually in the resolved cue list."""
    keywords = _ROLE_KEYWORDS.get(role)
    if not keywords:
        return None
    for cue in cues:
        text = f"{cue.get('id', '')} {cue.get('descriptor', '')}".lower()
        if any(kw in text for kw in keywords):
            return cue.get("id")
    return None


def _parse_cue_list(raw: str) -> list[dict]:
    """Extract a JSON array from a possibly noisy LLM response."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    return json.loads(raw[start : end + 1])


def _validate(items: list[dict]) -> list[dict]:
    """Keep only well-formed cues with a real description (≥6 words)."""
    out = []
    seen: set[str] = set()
    for item in items:
        src  = str(item.get("source", "")).strip()
        desc = str(item.get("description", "")).strip()
        if not src or not desc:
            continue
        if len(desc.split()) < 6:
            continue
        cid = _source_to_id(src)
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"source": src, "description": desc})
    return out


def _cue_dict(source: str, desc: str) -> dict:
    """Convert a raw extracted pair to an AudioCueSpec-compatible dict."""
    category, loop = _infer_category_loop(source, desc)
    dur = 4.0 if category in ("ambient", "music") else 2.0
    return {
        "id":         _source_to_id(source),
        "category":   category,
        "kind":       "gen",   # always SA3 — these are world-grounded, not DSP archetypes
        "descriptor": source,  # human-readable label; fallback if gen_prompt unused
        "gen_prompt": desc,    # the actual SA3 prompt
        "loop":       loop,
        "duration_s": dur,
        "variations": 1,
        "archetype":  "",
        "text":       "",
        "voice":      "default",
        "spatial":    category == "sfx",
    }


def _cache_key(concept: str, n: int) -> str:
    h = hashlib.sha256((concept + str(n)).encode()).hexdigest()[:16]
    return h


def extract_cues(concept: str, llm, *, n: int = 10,
                 seed: int = 0, cache_dir: Path | None = None) -> list[dict]:
    """Primary path: extract diegetic cues from the game concept via LLM.

    Returns a list of AudioCueSpec-compatible dicts (source → SA3 prompt).
    Falls back to an empty list on any failure so callers can degrade to the
    genre preset.

    Args:
        concept:   Step-2 concept text (or user prompt as fallback).
        llm:       Project LLM client exposing .chat(system, user, temperature,
                   thinking, seed) -> str.
        n:         Target cue count communicated to the model (8–12 sensible).
        seed:      Generation seed forwarded to the LLM.
        cache_dir: Optional directory for caching results across runs.
    """
    if not concept or llm is None:
        return []

    key   = _cache_key(concept, n)
    cache = (cache_dir / f"diegetic_{key}.json") if cache_dir else None
    if cache and cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    user_prompt = (
        f"Game concept (extract {n} diegetic sound cues):\n\n{concept.strip()}"
    )
    try:
        raw = llm.chat(
            system=_EXTRACT_SYSTEM,
            user=user_prompt,
            temperature=0.0,
            thinking=False,
            seed=seed or None,
        )
    except Exception:  # noqa: BLE001
        return []

    try:
        items = _parse_cue_list(raw)
        items = _validate(items)
    except Exception:  # noqa: BLE001
        return []

    if not items:
        return []

    result = [_cue_dict(item["source"], item["description"]) for item in items]

    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    return result


# ── Genre preset fallback (--no-llm path only) ────────────────────────────────

_REQUIRED: dict[str, list[tuple]] = {
    "underwater": [
        ("rebreather_bubbles",  "ambient", "continuous stream of air bubbles from a scuba rebreather underwater"),
        ("ambient_deep_drone",  "ambient", "deep ocean pressure hum vast dark underwater bass drone"),
        ("ambient_shallows",    "ambient", "bright sunlit shallow water shimmer gentle ocean current"),
        ("kelp_creak",          "ambient", "kelp forest stalks swaying underwater structure groan"),
        ("shark_approach",      "sfx",     "deep low ominous pulse danger proximity warning underwater"),
        ("harpoon_fire",        "sfx",     "hiss and bubble trail underwater harpoon launches"),
        ("harpoon_reload",      "sfx",     "mechanical click air hiss harpoon reload"),
        ("fish_scatter",        "sfx",     "rapid splashing small fish school scattering"),
        ("o2_alarm",            "sfx",     "rising beep cadence low oxygen warning alarm"),
        ("data_log_pickup",     "sfx",     "sonar ping digital confirm chime pickup"),
        ("boss_theme",          "music",   "intense orchestral tension boss arena underwater"),
        ("surface_break_sting", "music",   "triumphant swell player breaks ocean surface rescue"),
    ],
    "shooter": [
        ("gun_fire",    "sfx",   "sharp gunshot"),
        ("empty_clip",  "sfx",   "gun clip empty dry fire click"),
        ("reload",      "sfx",   "metallic weapon reload clank"),
        ("hit",         "sfx",   "bullet impact hit"),
        ("explosion",   "sfx",   "explosion blast"),
        ("music_loop",  "music", "intense combat music loop"),
        ("ui_click",    "sfx",   "menu button click"),
    ],
    "platformer": [
        ("jump",        "sfx",   "character jump hop"),
        ("coin",        "sfx",   "collect a gold coin"),
        ("hit",         "sfx",   "take damage hit"),
        ("powerup",     "sfx",   "power up upgrade"),
        ("footstep",    "sfx",   "footstep"),
        ("music_loop",  "music", "upbeat platformer background music loop"),
        ("ui_click",    "sfx",   "menu button click"),
    ],
    "racing": [
        ("engine",      "sfx",   "engine loop rumble"),
        ("skid",        "sfx",   "tyre skid screech"),
        ("crash",       "sfx",   "metal crash clank"),
        ("music_loop",  "music", "fast driving electronic music loop"),
        ("ui_click",    "sfx",   "menu button click"),
    ],
    "puzzle": [
        ("select",      "sfx",   "ui select blip"),
        ("place",       "sfx",   "piece place click"),
        ("match",       "sfx",   "match pickup chime"),
        ("win",         "sfx",   "win powerup jingle"),
        ("music_loop",  "music", "calm relaxing puzzle background music"),
    ],
    "rpg": [
        ("ui_click",    "sfx",   "menu button click"),
        ("pickup",      "sfx",   "collect item"),
        ("hit",         "sfx",   "sword hit impact"),
        ("level_up",    "sfx",   "level up powerup"),
        ("dialogue",    "voice", "npc greeting line"),
        ("ambient",     "ambient","calm village ambient bed"),
        ("music_loop",  "music", "epic orchestral rpg background music"),
    ],
    "horror": [
        ("ambient_drone",  "ambient", "deep dark eerie horror atmospheric drone hum"),
        ("music_tension",  "music",   "slow tense horror minimal music loop"),
        ("footstep",       "sfx",     "slow careful footstep on wooden floor"),
        ("door_creak",     "sfx",     "old wooden door creaking slowly"),
        ("hit",            "sfx",     "impact hit thud"),
        ("ui_click",       "sfx",     "menu button click"),
    ],
    "generic": [
        ("music_loop",  "music",   "background game music loop"),
        ("ambient",     "ambient", "environmental background ambience"),
        ("ui_click",    "sfx",     "menu button click"),
        ("pickup",      "sfx",     "collect item"),
        ("hit",         "sfx",     "impact hit"),
    ],
}

_GAME_TYPE_WORDS = [
    (("underwater", "scuba", "diving", "ocean", "submarine", "aquatic",
      "deep sea", "reef", "seabed", "kelp", "harpoon", "rebreather"), "underwater"),
    (("shooter", "shoot", "fps", "shoot-em-up", "gun", "war", "military"),  "shooter"),
    (("platformer", "platform", "jump", "metroidvania", "side-scroller"),   "platformer"),
    (("racing", "racer", "kart", "driving", "car game", "rally"),           "racing"),
    (("puzzle", "match-3", "match 3", "tetris", "sokoban", "block"),        "puzzle"),
    (("rpg", "role-playing", "dungeon", "quest"),                           "rpg"),
    (("horror", "scary", "survival", "haunted", "zombie", "escape",
      "terror", "creepy"),                                                   "horror"),
]


def game_type_from_prompt(prompt: str) -> str:
    """Fallback: deterministic prompt → game type ('generic' default)."""
    p = (prompt or "").lower()
    # Tokenise so "skydiving" does not match bare "diving", etc.
    tokens = set(re.findall(r"\b\w+\b", p))
    for words, gt in _GAME_TYPE_WORDS:
        for w in words:
            matched = (w in p) if " " in w else (w in tokens)
            if matched:
                return gt
    return "generic"


def required_cues(game_type: str) -> list[dict]:
    """Fallback genre preset — used only on the --no-llm path."""
    gt = game_type if game_type in _REQUIRED else "generic"
    return [{"id": cid, "category": cat, "descriptor": desc, "kind": "auto"}
            for (cid, cat, desc) in _REQUIRED[gt]]
