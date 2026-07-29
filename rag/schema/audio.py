"""
GTA5 audio bank / sound ontology entries.
Covers script banks, ambient banks, music events, and mission audio.

The dumps give a bare name and nothing else, so every semantic word an entry
carries has to be decoded from that name. Two decodings matter most:

  * Ped-model-shaped names (a_c_rhesus, A_M_M_Business_01, S_M_Y_Cop_01) are
    speech banks — a ped's voice lines, not a sound effect.
  * The remaining names carry their subject in plain tokens (SIREN, ALARM,
    EXPLOSION, HEIST). Spelling those out is what lets "police siren wail"
    reach SIREN_DISTANT instead of a police *dialogue* bank that merely shares
    the word "police".

Type detection reads the name before the source file: the same name appears in
several dumps, and a file-derived type made the entry depend on scrape order.
"""
from __future__ import annotations
import re
from .base import RagEntry, build_chunk


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

# GTA5 ped model grammar: <category>_<gender|c>_[age_]<body>.  Matching it in
# the audio dumps identifies a ped speech bank.
_PED_NAME_RE = re.compile(r"^(?:a|s|g|u)_(?:c|m|f)_", re.IGNORECASE)
_STORY_PED_RE = re.compile(r"^(?:ig|cs|csb|hc|mp)_[a-z]", re.IGNORECASE)

# name token → (spoken phrase, tag). Order matters: first match wins.
_SUBJECT_KW: list[tuple[str, str, str]] = [
    ("siren",     "siren wail",                "subject.siren"),
    ("alarm",     "alarm bell",                "subject.alarm"),
    ("explo",     "explosion blast",           "subject.explosion"),
    ("bomb",      "bomb detonation",           "subject.explosion"),
    ("gunshot",   "gunshot",                   "subject.gunfire"),
    ("gunfire",   "gunfire",                   "subject.gunfire"),
    ("shoot",     "gunfire",                   "subject.gunfire"),
    ("radio",     "radio station",             "subject.radio"),
    ("horn",      "vehicle horn",              "subject.vehicle"),
    ("engine",    "engine noise",              "subject.vehicle"),
    ("crash",     "vehicle crash",             "subject.vehicle"),
    ("chopper",   "helicopter rotor",          "subject.vehicle"),
    ("heli",      "helicopter rotor",          "subject.vehicle"),
    ("crowd",     "crowd noise",               "subject.crowd"),
    ("cheer",     "crowd cheering",            "subject.crowd"),
    ("rain",      "rain",                      "subject.weather"),
    ("thunder",   "thunder",                   "subject.weather"),
    ("wind",      "wind",                      "subject.weather"),
    ("phone",     "phone ring",                "subject.phone"),
    ("countdown", "countdown timer",           "subject.timer"),
    ("timer",     "timer beep",                "subject.timer"),
    ("clock",     "ticking clock",             "subject.timer"),
    ("door",      "door movement",             "subject.foley"),
    ("footstep",  "footsteps",                 "subject.foley"),
    ("water",     "water",                     "subject.water"),
    ("ocean",     "ocean waves",               "subject.water"),
    ("heist",     "heist score",               "context.heist"),
    ("chase",     "chase score",               "context.chase"),
    ("combat",    "combat score",              "context.combat"),
    ("stealth",   "stealth score",             "context.stealth"),
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


# Several dumps list the same name (AGENCY_HEIST_FIB_TOWER_ALARMS is in both the
# script and ambient files). One id means one entry, so when that happens the
# more specific type wins — a fixed order, never scrape order.
_TYPE_PRECEDENCE: list[str] = [
    "speech", "complete", "sting", "mission", "ambient", "music", "script", "sfx",
]


def type_rank(bank_type: str) -> int:
    """Lower wins. Used by the scraper to collapse duplicate names."""
    return _TYPE_PRECEDENCE.index(bank_type) if bank_type in _TYPE_PRECEDENCE \
        else len(_TYPE_PRECEDENCE)


def _is_ped_bank(name: str) -> bool:
    return bool(_PED_NAME_RE.match(name) or _STORY_PED_RE.match(name))


def _detect_type(name: str, source_file: str) -> str:
    # Name evidence first — it is intrinsic to the entry, the source file is not.
    if _is_ped_bank(name):
        return "speech"
    nl = name.upper()
    for prefix, btype in _NAME_TYPE_RULES:
        if nl.startswith(prefix):
            return btype
    sl = source_file.lower()
    for key, btype in _BANK_TYPE_RULES:
        if key.lower() in sl:
            return btype
    return "sfx"


def _subject(name: str) -> tuple[str, list[str]]:
    """(spoken phrase, tags) decoded from the name's own tokens."""
    lower  = name.lower()
    phrase = ""
    tags: list[str] = []
    for token, spoken, tag in _SUBJECT_KW:
        if token in lower:
            phrase = spoken
            tags.append(tag)
            break
    return phrase, tags


def _tags(name: str, bank_type: str) -> list[str]:
    tags  = [f"type.{bank_type}"]
    lower = name.lower()
    _, subject_tags = _subject(name)
    tags += subject_tags
    for kw, tag in _CONTEXT_KW.items():
        if kw in lower and tag not in tags:
            tags.append(tag)
    return list(dict.fromkeys(tags))


def _describe(name: str, bank_type: str, subject: str) -> tuple[str, list[str]]:
    """(description, aliases) — what this bank actually is."""
    spoken = name.replace("_", " ").lower()

    if bank_type == "speech":
        return (f"Ped voice bank — spoken dialogue lines for the {spoken} ped model, "
                f"not a sound effect.",
                ["voice lines", "ped dialogue", "speech bank", f"{spoken} voice"])

    if bank_type in ("music", "sting", "complete"):
        noun = {"music": "music cue", "sting": "musical sting",
                "complete": "mission-complete cue"}[bank_type]
        extra = f" Carries the {subject}." if subject else ""
        return (f"Score {noun} triggered by mission or world state.{extra}",
                [noun, "music cue", "score"])

    if subject:
        return (f"Sound event — {subject}.",
                [subject, "sound effect", "sfx"])

    return (f"{bank_type.capitalize()} audio bank, triggered from script.",
            ["sound effect", "audio bank"])


def make_audio_entry(name: str,
                     source_file: str = "scriptAudioBankNames.json") -> RagEntry:
    bank_type       = _detect_type(name, source_file)
    tags            = _tags(name, bank_type)
    subject, _      = _subject(name)
    label           = name.replace("_", " ").title()
    desc, extra_al  = _describe(name, bank_type, subject)
    aliases         = [name.lower().replace("_", " ")] + extra_al

    entry = RagEntry(
        id=f"gta5:audio:{name}",
        domain="gta5", kind="audio",
        canonical=name, label=label,
        aliases=list(dict.fromkeys(aliases)),
        description=desc,
        tags=tags,
        meta={"bank_type": bank_type, "subject": subject},
        source=source_file,
    )
    entry.chunk_text = build_chunk(
        entry,
        f"GTA5 audio bank {entry.canonical}",
        {"Kind": bank_type, "Sound": subject},
    )
    return entry
