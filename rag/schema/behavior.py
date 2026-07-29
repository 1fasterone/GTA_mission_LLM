"""
GTA5 ped scenario / behavior ontology entries.
Scenarios = ambient behaviors: WORLD_HUMAN_COP_IDLES, WORLD_HUMAN_DRINKING, etc.
"""
from __future__ import annotations
from .base import RagEntry, build_chunk


_TYPE_PREFIX: dict[str, str] = {
    "WORLD_HUMAN":   "ambient_human",
    "WORLD_VEHICLE": "ambient_vehicle",
    "WORLD_ANIMAL":  "ambient_animal",
    "CODE_HUMAN":    "scripted_human",
}

_BEHAVIOR_KW: dict[str, tuple[str, str]] = {
    "cop":          ("faction.law",      "behavior.patrol"),
    "guard":        ("faction.service",  "behavior.guard"),
    "patrol":       ("faction.law",      "behavior.patrol"),
    "dealer":       ("faction.criminal", "behavior.deal"),
    "drug":         ("faction.criminal", "behavior.deal"),
    "bum":          ("faction.civilian", "behavior.loiter"),
    "drink":        ("faction.civilian", "behavior.drinking"),
    "smoke":        ("faction.civilian", "behavior.smoking"),
    "phone":        ("faction.civilian", "behavior.phone"),
    "jog":          ("faction.civilian", "behavior.exercise"),
    "yoga":         ("faction.civilian", "behavior.exercise"),
    "golf":         ("faction.civilian", "behavior.leisure"),
    "sunbathe":     ("faction.civilian", "behavior.leisure"),
    "wander":       ("faction.civilian", "behavior.wander"),
    "cheering":     ("faction.civilian", "behavior.cheer"),
    "clipboard":    ("faction.service",  "behavior.working"),
    "construction": ("faction.service",  "behavior.construction"),
    "drill":        ("faction.service",  "behavior.construction"),
    "gardener":     ("faction.service",  "behavior.gardening"),
    "film":         ("faction.civilian", "behavior.film"),
    "binoculars":   ("faction.civilian", "behavior.observe"),
    "atm":          ("faction.civilian", "behavior.atm"),
    "seat":         ("faction.civilian", "behavior.sitting"),
    "lean":         ("faction.civilian", "behavior.resting"),
    "stand":        ("faction.civilian", "behavior.standing"),
    "busker":       ("faction.civilian", "behavior.busking"),
    "beach":        ("faction.civilian", "behavior.leisure"),
    "fishing":      ("faction.civilian", "behavior.leisure"),
    "picnic":       ("faction.civilian", "behavior.leisure"),
    "protest":      ("faction.civilian", "behavior.protest"),
    "preach":       ("faction.civilian", "behavior.preaching"),
    "car_park":     ("faction.service",  "behavior.working"),
    "security":     ("faction.service",  "behavior.guard"),
}


def _tags(name: str) -> list[str]:
    tags   = []
    upper  = name.upper()
    lower  = name.lower()

    for prefix, t in _TYPE_PREFIX.items():
        if upper.startswith(prefix):
            tags.append(f"type.{t}")
            break

    for kw, (faction_tag, behavior_tag) in _BEHAVIOR_KW.items():
        if kw in lower:
            if faction_tag not in tags:
                tags.append(faction_tag)
            tags.append(behavior_tag)
            break

    if not any(t.startswith("behavior.") for t in tags):
        tags.append("behavior.ambient")
    if not any(t.startswith("faction.") for t in tags):
        tags.append("faction.civilian")
    return tags


def _aliases(name: str) -> list[str]:
    clean = (name.replace("WORLD_HUMAN_", "")
               .replace("WORLD_VEHICLE_", "")
               .replace("WORLD_ANIMAL_", "")
               .replace("CODE_HUMAN_", "")
               .replace("_", " ").lower())
    return [clean, f"{clean} behavior", f"ped {clean}", f"{clean} animation"]


def make_scenario_entry(name: str,
                        source: str = "DurtyFree/scenariosCompact.json") -> RagEntry:
    tags    = _tags(name)
    aliases = _aliases(name)
    label   = name.replace("_", " ").title()
    desc    = (
        f"Ambient ped behavior: {name.replace('_', ' ').lower()}. "
        f"Driven with TaskInvokeScenario."
    )

    entry = RagEntry(
        id=f"gta5:scenario:{name}",
        domain="gta5", kind="scenario",
        canonical=name, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta={}, source=source,
    )
    entry.chunk_text = build_chunk(entry, f"GTA5 scenario {entry.canonical}")
    return entry
