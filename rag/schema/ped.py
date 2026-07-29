"""
GTA5 ped model ontology entries.

Auto-decodes the GTA5 naming convention:
  s_m_y_cop_01   → service, male, young, cop
  a_f_m_beach_01 → ambient, female, middle, beach
  g_m_y_ballas_01 → gang, male, young, ballas

Three families do NOT follow that gender/age grammar and used to fall through
to "unknown civilian character" with civilian/pedestrian/bystander aliases:

  a_c_*                 animals — a boar is not a bystander
  ig_* / cs_* / csb_*   the named story cast — Amanda Townley is not "service"
  player_zero/one/two   Michael, Franklin, Trevor
"""
from __future__ import annotations
import re
from typing import Any
from .base import RagEntry, build_chunk


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

# a_c_<key> → (display species, aliases). Every a_c_ model in the dumps is here;
# an unlisted one still resolves as an animal, just without the species words.
_ANIMALS: dict[str, tuple[str, list[str]]] = {
    "boar":         ("Boar",              ["boar", "wild boar", "hog"]),
    "cat":          ("Cat",               ["cat", "house cat", "kitten"]),
    "chickenhawk":  ("Chicken Hawk",      ["chicken hawk", "hawk", "bird of prey"]),
    "chimp":        ("Chimpanzee",        ["chimp", "chimpanzee", "monkey", "ape"]),
    "chop":         ("Chop",              ["chop", "dog", "rottweiler", "franklin's dog"]),
    "cormorant":    ("Cormorant",         ["cormorant", "seabird", "bird"]),
    "cow":          ("Cow",               ["cow", "cattle", "livestock"]),
    "coyote":       ("Coyote",            ["coyote", "wild dog"]),
    "crow":         ("Crow",              ["crow", "bird"]),
    "deer":         ("Deer",              ["deer", "stag", "wildlife"]),
    "dolphin":      ("Dolphin",           ["dolphin", "sea animal"]),
    "fish":         ("Fish",              ["fish", "sea animal"]),
    "hen":          ("Hen",               ["hen", "chicken", "bird"]),
    "humpback":     ("Humpback Whale",    ["humpback whale", "whale", "sea animal"]),
    "husky":        ("Husky",             ["husky", "dog", "sled dog"]),
    "killerwhale":  ("Killer Whale",      ["killer whale", "orca", "whale"]),
    "mtlion":       ("Mountain Lion",     ["mountain lion", "cougar", "puma", "big cat"]),
    "panther":      ("Panther",           ["panther", "big cat"]),
    "pig":          ("Pig",               ["pig", "farm animal", "livestock"]),
    "pigeon":       ("Pigeon",            ["pigeon", "bird"]),
    "poodle":       ("Poodle",            ["poodle", "dog", "lap dog"]),
    "pug":          ("Pug",               ["pug", "dog", "lap dog"]),
    "rabbit":       ("Rabbit",            ["rabbit", "bunny", "wildlife"]),
    "rat":          ("Rat",               ["rat", "rodent", "vermin"]),
    "retriever":    ("Retriever",         ["retriever", "dog", "golden retriever"]),
    "rhesus":       ("Rhesus Monkey",     ["rhesus monkey", "monkey", "macaque"]),
    "rottweiler":   ("Rottweiler",        ["rottweiler", "dog", "guard dog", "attack dog"]),
    "seagull":      ("Seagull",           ["seagull", "gull", "bird"]),
    "sharkhammer":  ("Hammerhead Shark",  ["hammerhead shark", "shark", "sea animal"]),
    "sharktiger":   ("Tiger Shark",       ["tiger shark", "shark", "sea animal"]),
    "shepherd":     ("German Shepherd",   ["german shepherd", "dog", "police dog", "guard dog"]),
    "stingray":     ("Stingray",          ["stingray", "ray", "sea animal"]),
    "westy":        ("West Highland Terrier", ["westie", "terrier", "dog", "lap dog"]),
}

# The three protagonists ship under opaque engine names.
_PLAYERS: dict[str, tuple[str, list[str]]] = {
    "player_zero": ("Michael De Santa",
                    ["michael", "michael de santa", "protagonist", "player character"]),
    "player_one":  ("Franklin Clinton",
                    ["franklin", "franklin clinton", "protagonist", "player character"]),
    "player_two":  ("Trevor Philips",
                    ["trevor", "trevor philips", "protagonist", "player character"]),
}

_STORY_PREFIX: dict[str, str] = {
    "ig":  "story",       # in-game story character
    "cs":  "cutscene",    # cutscene variant of a story character
    "csb": "cutscene",    # cutscene background character
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
    "animal":   "low",
    "story":    "low",
}

# Faction implied by the name's own prefix letter, used only when no keyword in
# the body says otherwise. Previously everything non-ambient fell back to
# "service", which put 769 of 1,109 peds — the whole story cast included — in a
# faction they have nothing to do with.
_PREFIX_FACTION: dict[str, str] = {
    "ambient":     "civilian",
    "service":     "service",
    "gang":        "gang",
    "unique":      "unique",
    "heist":       "criminal",
    "multiplayer": "civilian",
}


def _split_camel(text: str) -> str:
    """AmandaTownley -> Amanda Townley;  cs_ashley -> Ashley;  ChengSr -> Cheng Sr."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).replace("_", " ")
    return " ".join(spaced.split()).title()


def _decode(name: str) -> dict[str, Any]:
    low    = name.lower()
    result: dict[str, Any] = dict(prefix=None, gender=None, age=None,
                                  faction=None, role=None, weapon=None,
                                  family="generic", species=None, character=None)

    # --- animals: a_c_<species>[_02] ---
    if low.startswith("a_c_"):
        result.update(prefix="animal", family="animal", faction="animal",
                      role="animal", weapon="unarmed",
                      species=re.sub(r"_\d+$", "", low[4:]))
        return result

    # --- the three protagonists ---
    if low in _PLAYERS:
        result.update(prefix="player", family="story", faction="story",
                      role="protagonist", weapon="unarmed",
                      character=_PLAYERS[low][0])
        return result

    parts = low.split("_")
    head  = parts[0]

    # --- prefix ---
    if head in _STORY_PREFIX:
        result["prefix"] = _STORY_PREFIX[head]
        result["family"] = "story"
        body_raw = name.split("_", 1)[1] if "_" in name else name
        result["character"] = _split_camel(re.sub(r"_?\d+$", "", body_raw))
        parts = parts[1:]
    elif head == "mp":
        result["prefix"] = "multiplayer"
        parts = parts[1:]
    elif head in _PREFIX_MAP:
        result["prefix"] = _PREFIX_MAP[head]
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

    # --- fallbacks: name evidence only, otherwise leave it unset ---
    # An unknown ped tagged faction.service is worse than an untagged one — the
    # tag is wrong, and wrong tags survive into every downstream filter.
    if result["faction"] is None:
        result["faction"] = ("story" if result["family"] == "story"
                             else _PREFIX_FACTION.get(result["prefix"] or ""))
    if result["role"] is None and result["faction"] == "civilian":
        result["role"] = "civilian"
    if result["weapon"] is None:
        result["weapon"] = "unarmed"

    return result


def _label(name: str, d: dict) -> str:
    if d["family"] == "animal":
        species = d["species"] or ""
        known   = _ANIMALS.get(species)
        return known[0] if known else _split_camel(species) or name
    if d["character"]:
        return d["character"]

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
    gender_str  = f"({d['gender'].title()})" if d["gender"] else ""
    age_str     = d["age"].title() if d["age"] else ""
    faction_str = d["faction"].title() if d["faction"] and d["faction"] not in ("civilian", "service") else ""
    pieces = [p for p in (faction_str, body, age_str, gender_str) if p]
    return " ".join(pieces) or name


def _tags(d: dict) -> list[str]:
    t = []
    if d["prefix"]:   t.append(f"category.{d['prefix']}")
    if d["faction"]:  t.append(f"faction.{d['faction']}")
    if d["gender"]:   t.append(f"gender.{d['gender']}")
    if d["age"]:      t.append(f"age.{d['age']}")
    if d["role"]:     t.append(f"role.{d['role']}")
    if d["weapon"]:   t.append(f"weapon.{d['weapon']}")
    if d["species"]:  t.append(f"species.{d['species']}")
    if d["faction"] in _THREAT_BY_FACTION:
        t.append(f"threat.{_THREAT_BY_FACTION[d['faction']]}")
    return t


def _aliases(name: str, d: dict) -> list[str]:
    faction = d["faction"] or ""
    gender  = d["gender"]  or ""
    age     = d["age"]     or ""
    n       = name.lower()
    a: list[str] = []

    # Animals and the named cast never take the human/civilian alias sets:
    # "civilian, pedestrian, bystander" on a boar is how A_C_Westy — a dog —
    # came back second for "civilian bystander walking down the street".
    if d["family"] == "animal":
        known = _ANIMALS.get(d["species"] or "")
        a += list(known[1]) if known else [_split_camel(d["species"] or "").lower()]
        a += ["animal"]
        return list(dict.fromkeys(x for x in a if x))

    if d["character"]:
        if n in _PLAYERS:
            a += _PLAYERS[n][1]
        else:
            full = d["character"].lower()
            a += [full, full.split()[0]] if full else []
            a += ["story character", "named character"]
        return list(dict.fromkeys(x for x in a if x))

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
    """Only what the headline, aliases and tags do not already say."""
    cat = f" Category: {category}." if category else ""

    if d["family"] == "animal":
        return f"Animal ped model — {label}, not a human character.{cat}"

    if d["character"]:
        kind = "playable protagonist" if d["prefix"] == "player" else \
               "named story character" if d["prefix"] == "story" else \
               "named cutscene character"
        return f"{label} — {kind} from the GTA5 cast.{cat}"

    bits = [b for b in (d["age"], d["gender"], d["faction"]) if b]
    who  = " ".join(bits) + " character" if bits else "character of unrecorded type"
    role = f" Role: {d['role']}." if d["role"] else ""
    return f"{who.capitalize()}. Weapon: {d['weapon']}.{role}{cat}"


def _chunk(entry: "RagEntry", d: dict) -> str:
    return build_chunk(
        entry,
        f"GTA5 ped model {entry.canonical}",
        {"Name": entry.label,
         "Species": _ANIMALS.get(d["species"] or "", ("", []))[0] if d["species"] else ""},
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
        "family":    d["family"],
        "species":   d["species"],
        "character": d["character"],
    }.items() if v is not None}

    entry = RagEntry(
        id=f"gta5:ped:{name}",
        domain="gta5", kind="ped",
        canonical=name, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = _chunk(entry, d)
    return entry
