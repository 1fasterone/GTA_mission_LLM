"""
GTA5 vehicle ontology entries.
Class, type, speed, seats, weapons, and faction are auto-derived from raw DurtyFree data.
"""
from __future__ import annotations
from .base import RagEntry, build_chunk


_CLASS_TAG: dict[str, str] = {
    # DurtyFree uses a mix of plural and singular class names across DLCs
    "SUPER":              "class.super",
    "SPORTS":             "class.sports",
    "SPORTS CLASSICS":    "class.sports_classics",
    "SPORTSCLASSICS":     "class.sports_classics",
    "MUSCLE":             "class.muscle",
    "SEDANS":             "class.sedan",
    "SEDAN":              "class.sedan",
    "COUPES":             "class.coupe",
    "COUPE":              "class.coupe",
    "COMPACTS":           "class.compact",
    "COMPACT":            "class.compact",
    "SUVS":               "class.suv",
    "SUV":                "class.suv",
    "PICKUP TRUCKS":      "class.pickup",
    "PICKUPTRUCKS":       "class.pickup",
    "PICKUP TRUCK":       "class.pickup",
    "VANS":               "class.van",
    "VAN":                "class.van",
    "OFF-ROAD":           "class.offroad",
    "OFF_ROAD":           "class.offroad",
    "OFFROAD":            "class.offroad",
    "MOTORCYCLES":        "class.motorcycle",
    "MOTORCYCLE":         "class.motorcycle",
    "CYCLES":             "class.bicycle",
    "CYCLE":              "class.bicycle",
    "HELICOPTERS":        "class.helicopter",
    "HELICOPTER":         "class.helicopter",
    "PLANES":             "class.plane",
    "PLANE":              "class.plane",
    "BOATS":              "class.boat",
    "BOAT":               "class.boat",
    "EMERGENCY":          "class.emergency",
    "MILITARY":           "class.military",
    "INDUSTRIAL":         "class.industrial",
    "COMMERCIAL":         "class.commercial",
    "UTILITY":            "class.utility",
    "SERVICE":            "class.service",
    "TRAINS":             "class.train",
    "TRAIN":              "class.train",
}

_CLASS_FACTION: dict[str, str] = {
    "EMERGENCY": "faction.police",
    "MILITARY":  "faction.military",
}

# DurtyFree MaxSpeed is in m/s
_SPEED_TIERS = [
    (50.0, "speed.ultra"),   # >180 km/h
    (40.0, "speed.fast"),    # >144 km/h
    (28.0, "speed.medium"),  # >100 km/h
    (0.0,  "speed.slow"),
]

_CLASS_ALIASES: dict[str, list[str]] = {
    "SUPER":         ["supercar", "hypercar", "fast car"],
    "SPORTS":        ["sports car", "performance car"],
    "MUSCLE":        ["muscle car", "american muscle"],
    "SEDANS":        ["sedan", "family car"],
    "COUPES":        ["coupe", "two-door"],
    "COMPACTS":      ["compact", "hatchback", "small car"],
    "SUVS":          ["suv", "4x4", "crossover"],
    "SUV":           ["suv", "4x4", "crossover"],
    "PICKUP TRUCKS": ["pickup", "pickup truck"],
    "VANS":          ["van", "delivery van"],
    "OFF-ROAD":      ["offroad", "off-road", "dune buggy"],
    "MOTORCYCLES":   ["motorcycle", "motorbike", "bike"],
    "CYCLES":        ["bicycle", "pushbike"],
    "HELICOPTERS":   ["helicopter", "heli", "chopper"],
    "PLANES":        ["plane", "aircraft", "airplane"],
    "BOATS":         ["boat", "watercraft", "speedboat"],
    "EMERGENCY":     ["emergency vehicle", "police vehicle"],
    "MILITARY":      ["military vehicle", "army vehicle"],
    "INDUSTRIAL":    ["industrial truck", "heavy vehicle"],
    "COMMERCIAL":    ["bus", "commercial vehicle"],
}

_SPECIAL_ALIASES: dict[str, list[str]] = {
    "bison":     ["pickup truck", "truck", "pickup", "work truck"],
    "bison2":    ["pickup truck", "modified pickup", "lifted truck"],
    "sandking":  ["pickup truck", "offroad truck", "off-road pickup"],
    "mesa":      ["pickup truck", "offroad truck"],
    "police":    ["police car", "cop car", "patrol car", "lspd cruiser"],
    "police2":   ["police cruiser", "police suv", "lspd suv"],
    "police3":   ["police interceptor", "lspd interceptor"],
    "police4":   ["unmarked police", "undercover car", "plainclothes car"],
    "policeb":   ["police motorcycle", "police bike", "cop bike"],
    "policet":   ["police truck", "police pickup"],
    "policehelicopter": ["police helicopter", "police heli", "lspd chopper"],
    "riot":      ["swat van", "noose van", "riot van", "swat truck"],
    "fbi":       ["fbi suv", "government suv"],
    "fbi2":      ["fbi car", "government car"],
    "sheriff":   ["sheriff suv", "county sheriff vehicle"],
    "sheriff2":  ["sheriff car", "county cruiser"],
    "army":      ["military jeep", "army jeep", "military truck"],
    "buzzard":   ["attack helicopter", "attack heli", "combat chopper"],
    "buzzard2":  ["police helicopter", "police heli"],
    "valkyrie":  ["military helicopter", "armed helicopter"],
    "cargobob":  ["cargo helicopter", "cargo heli", "transport chopper"],
    "hydra":     ["fighter jet", "military jet", "vtol jet"],
    "lazer":     ["fighter plane", "military jet"],
    "annihilator": ["police helicopter", "pursuit helicopter"],
    "zentorno":  ["lambo style supercar"],
    "stretch":   ["limousine", "limo", "stretch limo"],
    "ambulance": ["ambulance", "paramedic vehicle", "ems vehicle"],
    "firetruk":  ["fire truck", "fire engine"],
    "stockade":  ["armored truck", "money truck", "security van"],
    "titan":     ["military cargo plane", "cargo aircraft"],
    "cargoplane":["cargo plane", "transport aircraft"],
    "luxor":     ["private jet", "luxury jet"],
    "shamal":    ["private plane", "small jet"],
    "adder":     ["bugatti style supercar"],
    "entityxf":  ["koenigsegg style supercar"],
    "t20":       ["mcclaren style supercar"],
}


def _tags(raw: dict) -> list[str]:
    cls   = raw.get("Class", "").upper().strip()
    vtype = raw.get("Type", "CAR").upper()
    tags  = []

    if cls in _CLASS_TAG:
        tags.append(_CLASS_TAG[cls])
    tags.append(_CLASS_FACTION.get(cls, "faction.civilian"))

    speed = raw.get("MaxSpeed") or 0
    for threshold, tag in _SPEED_TIERS:
        if speed >= threshold:
            tags.append(tag)
            break

    seats = raw.get("Seats") or 4
    tags.append("seats.1" if seats == 1 else "seats.2" if seats == 2 else
                "seats.4" if seats <= 4 else "seats.4+")

    type_map = {"HELI": "type.helicopter", "PLANE": "type.plane",
                "BOAT": "type.boat", "BIKE": "type.motorcycle",
                "BICYCLE": "type.bicycle", "TRAILER": "type.trailer"}
    tags.append(type_map.get(vtype, "type.car"))

    if raw.get("HasSirens"):          tags.append("feature.sirens")
    if raw.get("Weapons"):            tags.append("feature.armed")
    else:                             tags.append("feature.unarmed")
    if raw.get("HasConvertibleRoof"): tags.append("feature.convertible")
    if raw.get("HasArmoredWindows"):  tags.append("feature.armored")

    return tags


def _class_alias_lookup(cls: str) -> list[str]:
    """Resolve aliases tolerantly: try exact key, then stripped trailing-S, then stripped space."""
    if cls in _CLASS_ALIASES:
        return _CLASS_ALIASES[cls]
    # "MOTORCYCLE" → try "MOTORCYCLES"
    plural = cls + "S"
    if plural in _CLASS_ALIASES:
        return _CLASS_ALIASES[plural]
    # "PICKUP TRUCK" → "PICKUP TRUCKS"
    plural2 = cls + "S"
    if plural2 in _CLASS_ALIASES:
        return _CLASS_ALIASES[plural2]
    # normalise: remove spaces and try again
    nospace = cls.replace(" ", "")
    for key in _CLASS_ALIASES:
        if key.replace(" ", "") == nospace:
            return _CLASS_ALIASES[key]
    return []


def _aliases(raw: dict, display: str) -> list[str]:
    name  = (raw.get("Name") or "").lower()
    cls   = raw.get("Class", "").upper().strip()
    mfr   = (raw.get("Manufacturer") or "").lower()
    a     = [display.lower()]
    a    += _class_alias_lookup(cls)
    a    += _SPECIAL_ALIASES.get(name, [])
    if mfr:
        a.append(f"{mfr} {display.lower()}")
    return list(dict.fromkeys(a))


def _desc(raw: dict) -> str:
    """Only the facts the chunk's own Name/Manufacturer/Class lines omit."""
    seats = raw.get("Seats", 4)
    spd   = raw.get("MaxSpeed", 0) or 0
    arms  = raw.get("Weapons") or []
    parts = [f"{seats} seat(s). Max speed {spd:.1f} m/s."]
    if arms:
        parts.append(f"Armed: {', '.join(str(w) for w in arms[:3])}.")
    if raw.get("HasSirens"):          parts.append("Has sirens.")
    if raw.get("HasArmoredWindows"):  parts.append("Armored windows.")
    return " ".join(parts)


def _chunk(entry: "RagEntry") -> str:
    return build_chunk(
        entry,
        f"GTA5 vehicle {entry.canonical}",
        {"Name":         entry.label,
         "Manufacturer": entry.meta.get("manufacturer", ""),
         "Class":        entry.meta.get("class", "")},
    )


def make_vehicle_entry(raw: dict, source: str = "DurtyFree/vehicles.json") -> RagEntry:
    name = raw.get("Name") or raw.get("name", "")
    if not name:
        raise ValueError("vehicle record missing Name")

    dn = raw.get("DisplayName", {})
    display = (dn.get("English") if isinstance(dn, dict) else str(dn)) or name.title()

    tags    = _tags(raw)
    aliases = _aliases(raw, display)
    desc    = _desc(raw)

    meta = {k: v for k, v in {
        "hash":         raw.get("Hash"),
        "hex_hash":     raw.get("HexHash"),
        "class":        raw.get("Class"),
        "type":         raw.get("Type"),
        "manufacturer": raw.get("Manufacturer"),
        "seats":        raw.get("Seats"),
        "max_speed":    raw.get("MaxSpeed"),
        "acceleration": raw.get("Acceleration"),
        "dlc":          raw.get("DlcName"),
        "has_sirens":   raw.get("HasSirens"),
        "weapons":      raw.get("Weapons") or [],
        "has_armored":  raw.get("HasArmoredWindows"),
        "price":        raw.get("Price"),
    }.items() if v is not None}

    entry = RagEntry(
        id=f"gta5:vehicle:{name}",
        domain="gta5", kind="vehicle",
        canonical=name, label=display,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = _chunk(entry)
    return entry
