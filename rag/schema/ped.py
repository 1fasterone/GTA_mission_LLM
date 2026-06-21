"""
GTA5 ped model ontology entries.

Auto-decodes the GTA5 naming convention:
  s_m_y_cop_01  → service, male, young, cop
  a_f_m_beach_01 → ambient, female, middle, beach
  g_m_y_ballas_01 → gang, male, young, ballas
"""
from __future__ import annotations
from typing import Any
from .base import RagEntry


_PREFIX_MAP: dict[str, str] = {
    "a":  "ambient",
    "s":  "service",
    "g":  "gang",
    "u":  "unique",
    "hc": "heist",
}

_GENDER_MAP: dict[str, str] = {"m": "male", "f": "female"}
_AGE_MAP:    dict[str, str] = {"y": "young", "m": "middle", "o": "old"}

# Name body → faction tag
_FACTION_KW: dict[str, str] = {
    "cop":        "law",
    "swat":       "law",
    "police":     "law",
    "sheriff":    "law",
    "fibsec":     "law",
    "fiboffice":  "law",
    "fib":        "law",
    "noose":      "law",
    "prision":    "law",
    "prisguard":  "law",
    "marine":     "military",
    "nvce":       "military",
    "ballas":     "gang",
    "lost":       "gang",
    "vagos":      "gang",
    "famca":      "gang",
    "famdnf":     "gang",
    "famfor":     "gang",
    "mexgang":    "gang",
    "mexthug":    "gang",
    "cartel":     "gang",
    "armgoon":    "criminal",
    "dealer":     "criminal",
    "drug":       "criminal",
    "prisoner":   "criminal",
    "security":  "service",
    "paramedic": "service",
    "fireman":   "service",
    "doctor":    "service",
    "scientist": "service",
    "xmech":     "service",
    "dockwork":  "service",
    "construct": "service",
    "pilot":     "service",
    "acult":     "cult",
    "beach":     "civilian",
    "business":  "civilian",
    "hipster":   "civilian",
    "tourist":   "civilian",
    "shopper":   "civilian",
    "skater":    "civilian",
    "runner":    "civilian",
    "genfat":    "civilian",
    "genstreet": "civilian",
    "eastsa":    "civilian",
    "vinewood":  "civilian",
}

_ROLE_KW: dict[str, str] = {
    "cop":        "guard",
    "swat":       "shooter",
    "noose":      "shooter",
    "fib":        "shooter",
    "fiboffice":  "shooter",
    "sheriff":    "guard",
    "marine":     "shooter",
    "nvce":       "shooter",
    "security":   "guard",
    "securoguard":"guard",
    "prision":    "guard",
    "prisguard":  "guard",
    "cartelguard":"guard",
    "dealer":     "dealer",
    "paramedic":  "medic",
    "doctor":     "medic",
    "scientist":  "technician",
    "xmech":      "technician",
    "pilot":      "driver",
    "hitch":      "driver",
}

_WEAPON_KW: dict[str, str] = {
    "swat":     "rifle",
    "noose":    "rifle",
    "marine":   "rifle",
    "nvce":     "rifle",
    "cop":      "pistol",
    "fib":      "pistol",
    "fiboffice":"pistol",
    "sheriff":  "pistol",
    "security": "pistol",
    "ballas":   "pistol",
    "vagos":    "pistol",
    "lost":     "pistol",
    "famca":    "pistol",
    "armgoon":  "pistol",
    "dealer":   "pistol",
}

_THREAT_BY_FACTION: dict[str, str] = {
    "law":      "high",
    "military": "high",
    "gang":     "medium",
    "criminal": "medium",
    "cult":     "medium",
    "service":  "low",
    "civilian": "low",
    "unique":   "low",
}


def _decode(name: str) -> dict[str, Any]:
    parts  = name.lower().split("_")
    result = dict(prefix=None, gender=None, age=None,
                  faction=None, role=None, weapon=None)

    # --- prefix ---
    if name.lower().startswith("cs_"):
        result["prefix"] = "cutscene"
        parts = parts[1:]
    elif name.lower().startswith("mp_"):
        result["prefix"] = "multiplayer"
        parts = parts[1:]
    elif name.lower().startswith("ig_"):
        result["prefix"] = "story"
        parts = parts[1:]
    elif parts[0] in _PREFIX_MAP:
        result["prefix"] = _PREFIX_MAP[parts[0]]
        parts = parts[1:]

    # --- gender ---
    if parts and parts[0] in _GENDER_MAP:
        result["gender"] = _GENDER_MAP[parts[0]]
        parts = parts[1:]

    # --- age ---
    if parts and parts[0] in _AGE_MAP:
        result["age"] = _AGE_MAP[parts[0]]
        parts = parts[1:]

    body = "_".join(parts)
    for kw, val in _FACTION_KW.items():
        if kw in body:
            result["faction"] = val
            break
    for kw, val in _ROLE_KW.items():
        if kw in body:
            result["role"] = val
            break
    for kw, val in _WEAPON_KW.items():
        if kw in body:
            result["weapon"] = val
            break

    # --- fallbacks ---
    if result["faction"] is None:
        result["faction"] = "civilian" if result["prefix"] == "ambient" else "service"
    if result["role"]    is None:
        result["role"]    = "civilian"
    if result["weapon"]  is None:
        result["weapon"]  = "unarmed"

    return result


def _label(name: str, d: dict) -> str:
    parts = name.split("_")
    skip  = 0
    if parts and parts[0].lower() in ("a", "s", "g", "u"):
        skip = 1
    if len(parts) > skip and parts[skip].lower() in ("m", "f", "c"):
        skip += 1
    if len(parts) > skip and parts[skip].lower() in ("y", "m", "o"):
        skip += 1
    body_parts = parts[skip:]
    # Only strip trailing element if it's a pure number variant (01, 02 …)
    if body_parts and body_parts[-1].isdigit():
        body_parts = body_parts[:-1]
    body = " ".join(body_parts).title()
    gender_str  = f" ({d['gender'].title()})" if d["gender"] else ""
    age_str     = f" {d['age'].title()}"      if d["age"]    else ""
    faction_str = d["faction"].title() if d["faction"] and d["faction"] not in ("civilian", "service") else ""
    pieces = [p for p in (faction_str, body, age_str + gender_str) if p]
    return " ".join(pieces) or name


def _tags(d: dict) -> list[str]:
    t = []
    if d["prefix"]:   t.append(f"category.{d['prefix']}")
    if d["faction"]:  t.append(f"faction.{d['faction']}")
    if d["gender"]:   t.append(f"gender.{d['gender']}")
    if d["age"]:      t.append(f"age.{d['age']}")
    if d["role"]:     t.append(f"role.{d['role']}")
    if d["weapon"]:   t.append(f"weapon.{d['weapon']}")
    threat = _THREAT_BY_FACTION.get(d["faction"] or "", "low")
    t.append(f"threat.{threat}")
    return t


def _aliases(name: str, d: dict) -> list[str]:
    faction = d["faction"] or ""
    gender  = d["gender"]  or ""
    age     = d["age"]     or ""
    n       = name.lower()
    a: list[str] = []

    if faction == "law":
        a += ["cop", "police officer", "law enforcement", "officer"]
        if "sheriff" in n:     a += ["sheriff", "deputy", "county sheriff"]
        if "swat" in n or "noose" in n: a += ["swat", "noose", "tactical officer"]
        if "fib" in n:         a += ["fbi", "fbi agent", "federal agent", "government agent"]
        if "prision" in n or "prisguard" in n:
            a += ["prison guard", "correctional officer", "jail guard", "prison officer"]
    elif faction == "military":
        a += ["soldier", "military", "marine", "army"]
    elif faction == "gang":
        if "ballas" in n:     a += ["ballas", "ballas gang member", "purple gang"]
        elif "lost" in n:     a += ["lost mc", "biker", "motorcycle gang member"]
        elif "vagos" in n:    a += ["vagos", "mexican gang", "yellow gang"]
        elif "fam" in n:      a += ["families", "grove street", "families gang member"]
        elif "cartel" in n:   a += ["cartel guard", "cartel member", "cartel soldier", "drug cartel"]
        elif "mexgang" in n:  a += ["mexican gang", "mexican gang member"]
        else:                  a += ["gang member", "gangster", "thug"]
    elif faction == "criminal":
        a += ["criminal", "thug", "armed thug"]
        if "dealer" in n:     a += ["drug dealer", "dealer", "pusher"]
    elif faction == "service":
        if "paramedic" in n:  a += ["paramedic", "medic", "ems"]
        elif "fireman" in n:  a += ["firefighter", "fire department"]
        elif "pilot" in n:    a += ["pilot", "aviator"]
        elif "security" in n: a += ["security guard", "security", "bouncer", "guard"]
        elif "scientist" in n:a += ["scientist", "lab worker", "researcher", "chemist"]
        elif "xmech" in n:    a += ["mechanic", "technician"]
        elif "construct" in n:a += ["construction worker", "builder"]
        elif "dockwork" in n: a += ["dock worker", "dockworker", "warehouse worker", "foreman"]
        else:                  a += ["service worker"]
    elif faction == "civilian":
        if "business" in n:   a += ["businessman", "office worker", "executive", "suited man"]
        elif "beach" in n:    a += ["beach goer", "swimmer", "surfer", "beach guy"]
        elif "hipster" in n:  a += ["hipster", "young man", "casual"]
        elif "tourist" in n:  a += ["tourist", "visitor"]
        else:                  a += ["civilian", "pedestrian", "bystander"]
    elif faction == "cult":
        a += ["cult member", "cultist", "cult follower"]

    if gender == "female" and age == "old":
        a += ["old woman", "elderly woman"]
    elif gender == "male" and age == "old":
        a += ["old man", "elderly man"]

    return list(dict.fromkeys(a))


def _desc(name: str, d: dict, label: str, category: str) -> str:
    gender  = d["gender"]  or "unknown"
    age     = (d["age"] + " ") if d["age"] else ""
    faction = d["faction"] or "civilian"
    role    = d["role"]    or "civilian"
    weapon  = d["weapon"]  or "unarmed"
    cat     = f" Category: {category}." if category else ""
    return (
        f"GTA5 ped {name}. {label}. "
        f"{age}{gender} {faction} character. "
        f"Role: {role}. Weapon: {weapon}.{cat} "
        f"Use for: NPC cast, faction encounters, ambient crowd."
    )


def _chunk(entry: "RagEntry") -> str:
    return (
        f"GTA5 ped model {entry.canonical}\n"
        f"Label: {entry.label}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Description: {entry.description}"
    )


def make_ped_entry(raw: dict, source: str = "DurtyFree/peds.json") -> RagEntry:
    name = raw.get("Name") or raw.get("name", "")
    if not name:
        raise ValueError("ped record missing Name")

    d        = _decode(name)
    category = raw.get("Category") or raw.get("category", "")
    label    = _label(name, d)
    tags     = _tags(d)
    aliases  = _aliases(name, d)
    desc     = _desc(name, d, label, category)

    meta = {k: v for k, v in {
        "hash":     raw.get("Hash"),
        "hex_hash": raw.get("HexHash"),
        "dlc":      raw.get("DlcName") or raw.get("Dlc"),
        "category": category,
        "clip_set": raw.get("ClipSet") or raw.get("ClipSets"),
        "type":     raw.get("Type"),
    }.items() if v is not None}

    entry = RagEntry(
        id=f"gta5:ped:{name}",
        domain="gta5", kind="ped",
        canonical=name, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = _chunk(entry)
    return entry
