"""
NPC dialog line resolver — three responsibilities:

  resolve_voice(voice_id, cfg)    Map a voice label to a Higgs voice path/name.
  resolve_language(code)          Validate + normalise an ISO 639-1 language code.
  extract_dialog_lines(concept, llm, speaker, interaction_type)
      Ask the LLM to read the concept and produce NPC dialog lines grounded in
      the world. Returns a list of DialogLine-compatible dicts (no audio yet).

The LLM is only called during Step 3 (schema fill). After that all resolution
is deterministic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .higgs import SUPPORTED_EMOTIONS, SUPPORTED_LANGUAGES, SUPPORTED_STYLES
from .lang import normalize_lang


# ── Voice catalogue (config.yaml audio.voices can override) ──────────────────

_DEFAULT_VOICES: dict[str, str] = {
    "default":       "default",
    "male_deep":     "male_deep",
    "male_young":    "male_young",
    "female_soft":   "female_soft",
    "female_stern":  "female_stern",
    "elderly_male":  "elderly_male",
    "elderly_female":"elderly_female",
    "child":         "child",
    "robotic":       "robotic",
    "radio_announcer":"radio_announcer",
}

# Interaction type → sensible default voice
_INTERACTION_VOICE: dict[str, str] = {
    "ambient":  "default",
    "quest":    "male_deep",
    "vendor":   "female_soft",
    "combat":   "male_young",
    "follower": "female_soft",
}


def resolve_voice(voice_id: str, config: dict | None = None) -> str:
    """Map a voice label to the Higgs voice id/path that matches it.

    Priority:
      1. Exact key in config audio.voices dict (user catalogue).
      2. Default catalogue above.
      3. Pass through as-is (user may have supplied a direct path).
    """
    cfg    = ((config or {}).get("audio", {}) or {}).get("voices", {})
    merged = {**_DEFAULT_VOICES, **cfg}
    return merged.get(str(voice_id).strip().lower(), str(voice_id))


def resolve_language(code: str) -> str:
    """Return a normalised ISO 639-1 code, falling back to 'en' if unsupported."""
    c = normalize_lang(code)
    return c if c in SUPPORTED_LANGUAGES else "en"


def resolve_emotion(emotion: str) -> str:
    """Return a supported emotion label, falling back to 'neutral'."""
    e = str(emotion).strip().lower()
    return e if e in SUPPORTED_EMOTIONS else "neutral"


def resolve_audio_style(style: str) -> str:
    """Return a supported audio style label, falling back to 'natural'."""
    s = str(style).strip().lower()
    return s if s in SUPPORTED_STYLES else "natural"


def default_voice_for(interaction_type: str) -> str:
    """Suggest a voice ID for an interaction type (used by the fixer)."""
    return _INTERACTION_VOICE.get(str(interaction_type).strip().lower(), "default")


# ── Diegetic dialog extraction via LLM ───────────────────────────────────────

_EXTRACT_SYSTEM = """\
You extract NPC dialog lines for a game scene.

Rules:
- Lines must be grounded in the world described — no generic filler, no off-world
  references. A bartender gives clues about criminals, not weather.
- Each line captures a specific moment: offer a quest, give a warning, set atmosphere,
  or react to the player.
- Emotion labels must be one of: neutral happy sad angry fearful surprised whisper calm excited.
- Return 3 to 6 lines for the speaker.
- Every line MUST have a "line_id" (snake_case, unique), "text", "emotion", and "tags"
  (list of descriptive keywords like ["quest", "hint"] or ["ambient", "greeting"]).

Return JSON only — a list of objects, no prose, no markdown fences:
[{"line_id": "...", "text": "...", "emotion": "...", "tags": [...]}, ...]
"""


def _parse_line_list(raw: str) -> list[dict]:
    """Extract a JSON array from a possibly noisy LLM response."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    return json.loads(raw[start: end + 1])


def _validate_lines(items: list[dict]) -> list[dict]:
    """Keep only structurally valid line dicts."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        lid  = str(item.get("line_id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not lid or not text:
            continue
        if len(text.split()) < 3:       # reject single-word stubs
            continue
        lid = re.sub(r"[^a-z0-9_]", "_", lid.lower()).strip("_") or "line"
        if lid in seen:
            lid = f"{lid}_{len(out)}"
        seen.add(lid)
        out.append({
            "line_id": lid,
            "text":    text,
            "emotion": resolve_emotion(str(item.get("emotion", "neutral"))),
            "tags":    [str(t) for t in item.get("tags", []) if t],
        })
    return out


def extract_dialog_lines(
    concept: str,
    llm: Any,
    speaker: str = "NPC",
    interaction_type: str = "ambient",
    seed: int = 0,
    count: int = 0,
) -> list[dict]:
    """Primary path: extract diegetic NPC lines from the Step-2 concept via LLM.

    Returns a list of DialogLine-compatible dicts (no voice/audio fields yet —
    those are resolved in default_fix()). Returns [] on any failure so callers
    can fall back to default lines.

    `count` (>0) requests roughly that many lines, overriding the system prompt's
    default 3-6 — used by --compose to build a 10-15 line concept-driven bank.
    """
    if not concept or llm is None:
        return []

    count_line = (
        f"Write exactly {count} distinct lines for this speaker covering the "
        f"whole interaction (greeting, instructions, mid-race barks, warnings, "
        f"reactions, farewell) — this overrides any smaller count above.\n\n"
        if count and count > 0 else ""
    )
    user_prompt = (
        f"Speaker: {speaker}  |  interaction type: {interaction_type}\n\n"
        f"{count_line}"
        f"Game concept (write {speaker}'s dialog lines for this world):\n\n"
        f"{concept.strip()}"
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
        items = _parse_line_list(raw)
        return _validate_lines(items)
    except Exception:  # noqa: BLE001
        return []


# ── Default line fallbacks (--no-llm path only) ───────────────────────────────

_FALLBACK_LINES: dict[str, list[dict]] = {
    "quest": [
        {"line_id": "greet",    "text": "I've been waiting for someone capable.",  "emotion": "neutral", "tags": ["quest", "intro"]},
        {"line_id": "task",     "text": "There's something I need you to do.",      "emotion": "neutral", "tags": ["quest", "offer"]},
        {"line_id": "warning",  "text": "Be careful out there. It's dangerous.",    "emotion": "fearful", "tags": ["quest", "warning"]},
        {"line_id": "reward",   "text": "If you succeed, the reward will be yours.", "emotion": "happy",   "tags": ["quest", "reward"]},
    ],
    "vendor": [
        {"line_id": "welcome",  "text": "Welcome, traveller. Take a look around.", "emotion": "happy",   "tags": ["vendor", "greeting"]},
        {"line_id": "offer",    "text": "Got the finest goods in the region.",     "emotion": "neutral", "tags": ["vendor", "pitch"]},
        {"line_id": "farewell", "text": "Come back if you need anything else.",    "emotion": "neutral", "tags": ["vendor", "farewell"]},
    ],
    "combat": [
        {"line_id": "taunt",    "text": "You picked the wrong fight!",   "emotion": "angry",   "tags": ["combat", "taunt"]},
        {"line_id": "hurt",     "text": "Is that all you've got?",       "emotion": "angry",   "tags": ["combat", "hurt"]},
        {"line_id": "retreat",  "text": "Fall back! Regroup!",           "emotion": "fearful", "tags": ["combat", "retreat"]},
    ],
    "follower": [
        {"line_id": "ready",    "text": "I'm right behind you.",          "emotion": "calm",    "tags": ["follower", "idle"]},
        {"line_id": "confirm",  "text": "Understood. Let's move.",        "emotion": "neutral", "tags": ["follower", "confirm"]},
        {"line_id": "danger",   "text": "Heads up — something's wrong.", "emotion": "fearful", "tags": ["follower", "alert"]},
    ],
    "ambient": [
        {"line_id": "mutter_1", "text": "Another day in this place...",          "emotion": "calm",    "tags": ["ambient", "idle"]},
        {"line_id": "mutter_2", "text": "Hope things pick up around here.",      "emotion": "neutral", "tags": ["ambient", "idle"]},
        {"line_id": "mutter_3", "text": "Keep your head down, that's my advice.", "emotion": "whisper", "tags": ["ambient", "tip"]},
    ],
}


def default_lines(interaction_type: str) -> list[dict]:
    """Return minimal fallback lines for an interaction type (--no-llm path)."""
    return list(_FALLBACK_LINES.get(interaction_type, _FALLBACK_LINES["ambient"]))
