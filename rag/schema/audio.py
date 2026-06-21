"""
GTA5 audio bank / sound ontology entries.
Covers script banks, ambient banks, music events, and mission audio.
"""
from __future__ import annotations
from .base import RagEntry


_BANK_TYPE_RULES: list[tuple[str, str]] = [
    # source file name → bank type
    ("ambientAudio",   "ambient"),
    ("scriptAudio",    "script"),
    ("musicEvent",     "music"),
    ("missionComplete","complete"),
    ("missionAudio",   "mission"),
    ("speech",         "speech"),
]

_NAME_TYPE_RULES: list[tuple[str, str]] = [
    ("STING_",      "sting"),
    ("MISSION_PASSED", "complete"),
    ("MISSION_FAIL",   "mission"),
    ("DLC_ISLANDS_AMBIENCE", "ambient"),
    ("AMBIENCE",    "ambient"),
    ("MUSIC_",      "music"),
    ("CHASE_",      "script"),
    ("COMBAT_",     "script"),
    ("SPEECH_",     "speech"),
]

_CONTEXT_KW: dict[str, str] = {
    "police": "faction.law",
    "cop":    "faction.law",
    "swat":   "faction.law",
    "chase":  "context.chase",
    "alarm":  "context.alert",
    "combat": "context.combat",
    "passed": "emotion.victory",
    "fail":   "emotion.defeat",
    "ambience": "context.ambient",
    "exterior": "context.ambient",
    "interior": "context.ambient",
    "music":  "type.music",
    "sting":  "type.sting",
}


def _detect_type(name: str, source_file: str) -> str:
    sl = source_file.lower()
    for key, btype in _BANK_TYPE_RULES:
        if key.lower() in sl:
            return btype
    nl = name.upper()
    for prefix, btype in _NAME_TYPE_RULES:
        if nl.startswith(prefix):
            return btype
    return "sfx"


def _tags(name: str, bank_type: str) -> list[str]:
    tags  = [f"type.{bank_type}"]
    lower = name.lower()
    for kw, tag in _CONTEXT_KW.items():
        if kw in lower and tag not in tags:
            tags.append(tag)
    return tags


def make_audio_entry(name: str,
                     source_file: str = "scriptAudioBankNames.json") -> RagEntry:
    bank_type = _detect_type(name, source_file)
    tags      = _tags(name, bank_type)
    label     = name.replace("_", " ").title()
    desc      = (
        f"GTA5 audio: {name}. Type: {bank_type}. "
        f"Use with PLAY_SOUND / PLAY_AMBIENT_SPEECH / REQUEST_SCRIPT_AUDIO_BANK."
    )

    entry = RagEntry(
        id=f"gta5:audio:{name}",
        domain="gta5", kind="audio",
        canonical=name, label=label,
        aliases=[name.lower().replace("_", " ")],
        description=desc,
        tags=tags,
        meta={"bank_type": bank_type},
        source=source_file,
    )
    entry.chunk_text = (
        f"GTA5 audio {entry.canonical}\n"
        f"Type: {bank_type}\n"
        f"Tags: {', '.join(tags)}\n"
        f"Description: {entry.description}"
    )
    return entry
