"""GTA5 mission prop ontology entries.

Decodes prop names from ObjectList.ini (filtered to mission-relevant items).
"""
from __future__ import annotations
import re
from .base import RagEntry


# keyword → (category_tag, extra_aliases)
_KEYWORD_RULES: list[tuple[list[str], str, list[str]]] = [
    (["laptop"],                    "electronic",   ["laptop computer", "laptop", "computer"]),
    (["computer", "pc", "server",
      "cpu", "terminal"],           "electronic",   ["computer", "server", "terminal"]),
    (["phone", "cell", "mobile"],   "electronic",   ["phone", "cell phone", "mobile phone"]),
    (["tablet", "ipad"],            "electronic",   ["tablet", "tablet computer"]),
    (["radio", "walkie"],           "electronic",   ["radio", "walkie-talkie", "radio device"]),
    (["camera", "cam", "cctv"],     "electronic",   ["camera", "security camera", "surveillance"]),
    (["keypad", "panel"],           "electronic",   ["keypad", "control panel", "security panel"]),
    (["brief", "suitcase",
      "luggage"],                   "container",    ["briefcase", "suitcase", "case", "bag"]),
    (["bag"],                       "container",    ["bag", "duffel bag", "backpack", "satchel"]),
    (["crate"],                     "container",    ["crate", "wooden crate", "military crate",
                                                     "shipping crate", "container"]),
    (["box"],                       "container",    ["box", "cardboard box", "storage box"]),
    (["locker"],                    "container",    ["locker", "storage locker", "cabinet"]),
    (["barrel", "drum"],            "industrial",   ["barrel", "oil barrel", "chemical drum"]),
    (["tank", "gascan", "gas"],     "industrial",   ["gas can", "fuel can", "tank", "petrol"]),
    (["fuel"],                      "industrial",   ["fuel can", "gas can", "petrol can"]),
    (["money", "cash", "dollar",
      "bank"],                      "valuables",    ["money", "cash", "bills", "banknotes"]),
    (["gold", "bullion", "bling"],  "valuables",    ["gold bar", "gold", "bullion", "valuables"]),
    (["jewel", "diamond",
      "gem"],                       "valuables",    ["jewelry", "diamond", "gem", "valuables"]),
    (["drug", "meth", "weed",
      "coke", "narco", "acetone",
      "powder"],                    "contraband",   ["drugs", "narcotics", "contraband",
                                                     "drug package"]),
    (["ammo", "ammunition"],        "military",     ["ammo box", "ammunition", "ammo crate"]),
    (["bomb", "explo", "mine",
      "ied"],                       "explosive",    ["bomb", "explosive", "IED", "device"]),
    (["missile", "rocket",
      "warhead"],                   "military",     ["missile", "rocket", "warhead"]),
    (["grenade", "molotov"],        "explosive",    ["grenade", "throwable", "explosive"]),
    (["key", "keycard", "badge",
      "card", "fob"],               "access",       ["keycard", "key", "access card",
                                                     "security badge", "ID card"]),
    (["safe", "vault"],             "valuables",    ["safe", "vault", "safe box", "lockbox"]),
    (["med", "health", "medic",
      "first"],                     "medical",      ["medical bag", "medkit", "first aid kit",
                                                     "medical supply"]),
    (["file", "doc", "note",
      "evidence", "folder",
      "paper"],                     "documents",    ["files", "documents", "evidence",
                                                     "papers", "folder"]),
    (["trophy", "award",
      "prize"],                     "misc",         ["trophy", "prize", "award"]),
    (["pick", "drop",
      "collect"],                   "collectible",  ["collectible", "pickup item"]),
]

# Known prefix context hints for tags
_PREFIX_TAG: dict[str, str] = {
    "hei":  "mission.heist",
    "h4":   "mission.casino_heist",
    "bkr":  "mission.biker",
    "gr":   "mission.gunrunning",
    "ba":   "mission.dlc",
    "tr":   "mission.trevor",
    "ch":   "mission.heist",
    "p":    "mission.story",
    "prop": "prop.standard",
    "w":    "prop.weapon",
    "v":    "prop.interior",
    "xm":   "mission.dlc",
    "ex":   "mission.dlc",
    "m23":  "mission.story",
    "m24":  "mission.story",
    "m25":  "mission.story",
}

_STRIP_SUFFIXES = re.compile(r"[_\s]+\d+[a-z]?$")
_STRIP_PREFIXES = {"prop", "hei", "h4", "bkr", "gr", "ba", "tr", "ch", "xm", "ex", "sf", "vw"}


def _label(name: str) -> str:
    parts = name.split("_")
    # drop known prefixes and "prop" token
    out = []
    for i, p in enumerate(parts):
        if i == 0 and p in _STRIP_PREFIXES:
            continue
        if p == "prop":
            continue
        if p.isdigit() and i == len(parts) - 1:
            continue
        if len(p) <= 2 and p in ("01","02","a","b","s","l","m"):
            continue
        out.append(p)
    label = " ".join(out).title()
    return label or name.replace("_", " ").title()


def _decode(name: str) -> tuple[str, list[str], list[str]]:
    """Return (category_tag, aliases, extra_tags) for a prop name."""
    low = name.lower()
    cat_tag  = "prop.misc"
    aliases  = []
    extra    = []

    for kws, cat, al in _KEYWORD_RULES:
        if any(kw in low for kw in kws):
            cat_tag = f"prop.{cat}"
            aliases = al.copy()
            break

    # prefix-based mission context tag
    prefix = low.split("_")[0]
    if prefix in _PREFIX_TAG:
        extra.append(_PREFIX_TAG[prefix])

    # cutscene / story signal
    if "_cs_" in low or low.startswith("p_") or low.startswith("cs"):
        extra.append("context.cutscene")

    return cat_tag, aliases, extra


def make_prop_entry(name: str, source: str = "DurtyFree/ObjectList.ini") -> RagEntry:
    label    = _label(name)
    cat_tag, aliases, extra_tags = _decode(name)
    tags     = [cat_tag] + extra_tags
    aliases  = [label.lower()] + aliases
    aliases  = list(dict.fromkeys(aliases))

    desc = (
        f"GTA5 prop/object {name}. {label}. "
        f"Use for: mission spawnable objects, pickups, scene dressing, "
        f"collectible targets."
    )

    entry = RagEntry(
        id=f"gta5:prop:{name}",
        domain="gta5", kind="prop",
        canonical=name, label=label,
        aliases=aliases, description=desc,
        tags=tags, source=source,
    )
    entry.chunk_text = (
        f"GTA5 prop model {name}\n"
        f"Label: {label}\n"
        f"Aliases: {', '.join(aliases)}\n"
        f"Tags: {', '.join(tags)}\n"
        f"Description: {desc}"
    )
    return entry
