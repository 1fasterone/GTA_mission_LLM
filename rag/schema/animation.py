"""
GTA5 animation dictionary ontology entries.
Auto-tags from dict name patterns: context, emotion, stance, weapon, gender.
"""
from __future__ import annotations
import re
from .base import RagEntry, build_chunk


# Order matters — first match wins
_CONTEXT_PATTERNS: list[tuple[str, str]] = [
    (r"^amb@",          "context.ambient"),
    (r"@combat@|^combat_",  "context.combat"),
    (r"@enter_exit|@get_in|@get_out", "context.vehicle"),
    (r"^move_",         "context.movement"),
    (r"^rcm|^cs_|cutscene", "context.cutscene"),
    (r"^switch@",       "context.transition"),
    (r"^ped_",          "context.ped_action"),
    (r"miss|heist|scr_", "context.mission"),
    (r"^special_",      "context.special"),
]

_EMOTION_KW: list[tuple[str, str]] = [
    ("idle",      "emotion.idle"),
    ("wander",    "emotion.idle"),
    ("standing",  "emotion.idle"),
    ("walk",      "emotion.walk"),
    ("jog",       "emotion.run"),
    ("run",       "emotion.run"),
    ("sprint",    "emotion.sprint"),
    ("crouch",    "emotion.crouch"),
    ("sit",       "emotion.sit"),
    ("seat",      "emotion.sit"),
    ("prone",     "emotion.prone"),
    ("lay",       "emotion.prone"),
    ("shoot",     "emotion.combat"),
    ("aim",       "emotion.combat"),
    ("fire_",     "emotion.combat"),
    ("reload",    "emotion.combat"),
    ("enter",     "emotion.vehicle_enter"),
    ("get_in",    "emotion.vehicle_enter"),
    ("exit",      "emotion.vehicle_exit"),
    ("get_out",   "emotion.vehicle_exit"),
    ("drink",     "emotion.drinking"),
    ("smoke",     "emotion.smoking"),
    ("phone",     "emotion.phone"),
    ("talk",      "emotion.talk"),
    ("chat",      "emotion.talk"),
    ("surrender", "emotion.surrender"),
    ("react",     "emotion.react"),
    ("fall",      "emotion.fall"),
    ("dead",      "emotion.death"),
    ("death",     "emotion.death"),
    ("die",       "emotion.death"),
    ("celebrat",  "emotion.celebrate"),
    ("dance",     "emotion.dance"),
    ("cheer",     "emotion.cheer"),
    ("jump",      "emotion.jump"),
    ("climb",     "emotion.climb"),
    ("swim",      "emotion.swim"),
]

_WEAPON_KW: list[tuple[str, str]] = [
    ("unarmed",   "weapon.unarmed"),
    ("no_weapon", "weapon.unarmed"),
    ("pistol1",   "weapon.pistol"),
    ("pistol",    "weapon.pistol"),
    ("revolver",  "weapon.pistol"),
    ("smg",       "weapon.smg"),
    ("micro_smg", "weapon.smg"),
    ("rifle",     "weapon.rifle"),
    ("carbine",   "weapon.rifle"),
    ("2h_",       "weapon.rifle"),
    ("shotgun",   "weapon.shotgun"),
    ("sniper",    "weapon.sniper"),
    ("rpg",       "weapon.heavy"),
    ("minigun",   "weapon.heavy"),
    ("melee",     "weapon.melee"),
    ("knife",     "weapon.melee"),
    ("bat",       "weapon.melee"),
    ("hammer",    "weapon.melee"),
]

_STANCE_KW: list[tuple[str, str]] = [
    ("crouch",   "stance.crouching"),
    ("crouching","stance.crouching"),
    ("sit",      "stance.sitting"),
    ("seated",   "stance.sitting"),
    ("prone",    "stance.prone"),
    ("floor",    "stance.prone"),
    ("lay",      "stance.prone"),
    ("vehicle",  "stance.vehicle"),
    ("car",      "stance.vehicle"),
    ("bike",     "stance.vehicle"),
    ("horse",    "stance.vehicle"),
]

_GENDER_RE = [
    (r"@female@|_f_|_female_|_f@|female_base|@f_", "gender.female"),
    (r"@male@|_m_|_male_|_m@|male_base|@m_",       "gender.male"),
]


def _match_first(text: str, patterns: list[tuple[str, str]]) -> str | None:
    for pat, tag in patterns:
        if re.search(pat, text):
            return tag
    return None


def _kw_first(text: str, keywords: list[tuple[str, str]]) -> str | None:
    for kw, tag in keywords:
        if kw in text:
            return tag
    return None


def _humanize(dict_name: str) -> str:
    clean = dict_name.replace("@", " | ").replace("_", " ")
    return re.sub(r"\s+", " ", clean).strip().title()


def make_anim_tags(dict_name: str) -> list[str]:
    lower = dict_name.lower()
    tags  = []

    ctx = _match_first(lower, _CONTEXT_PATTERNS)
    tags.append(ctx or "context.gameplay")

    for pat, tag in _GENDER_RE:
        if re.search(pat, lower):
            tags.append(tag)
            break

    em = _kw_first(lower, _EMOTION_KW)
    if em:
        tags.append(em)

    wp = _kw_first(lower, _WEAPON_KW)
    if wp:
        tags.append(wp)

    st = _kw_first(lower, _STANCE_KW)
    tags.append(st or "stance.standing")

    return tags


def make_anim_entry(dict_name: str, clips: list[str],
                    source: str = "DurtyFree/animDictsCompact.json") -> RagEntry:
    tags  = make_anim_tags(dict_name)
    label = _humanize(dict_name)
    clip_preview = ", ".join(clips[:6])
    if len(clips) > 6:
        clip_preview += f" (+{len(clips) - 6} more)"

    desc = f"Animation dictionary holding {len(clips)} clip(s) for this action."

    entry = RagEntry(
        id=f"gta5:animation:{dict_name}",
        domain="gta5", kind="animation",
        canonical=dict_name, label=label,
        aliases=[],
        description=desc,
        tags=tags,
        meta={"clips": clips, "clip_count": len(clips)},
        source=source,
    )
    entry.chunk_text = build_chunk(
        entry,
        f"GTA5 animation dictionary {entry.canonical}",
        {"Action": entry.label, f"Clips ({len(clips)})": clip_preview},
    )
    return entry
