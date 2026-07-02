"""GTA5 weapon ontology entries."""
from __future__ import annotations
from .base import RagEntry

_CAT_TAG: dict[str, str] = {
    "GROUP_MELEE":          "melee",
    "GROUP_PISTOL":         "pistol",
    "GROUP_SMG":            "smg",
    "GROUP_RIFLE":          "rifle",
    "GROUP_MG":             "mg",
    "GROUP_SHOTGUN":        "shotgun",
    "GROUP_SNIPER":         "sniper",
    "GROUP_HEAVY":          "heavy",
    "GROUP_THROWN":         "thrown",
    "GROUP_STUNGUN":        "stun",
    "GROUP_UNARMED":        "unarmed",
    "GROUP_FIREEXTINGUISHER": "tool",
    "GROUP_PETROLCAN":      "tool",
    "GROUP_DIGISCANNER":    "tool",
    "GROUP_NIGHTVISION":    "tool",
    "GROUP_PARACHUTE":      "tool",
    "GROUP_TRANQILIZER":    "special",
    "GROUP_HACKINGDEVICE":  "special",
}

_CAT_ALIASES: dict[str, list[str]] = {
    "melee":   ["melee weapon", "close combat weapon", "blade"],
    "pistol":  ["pistol", "handgun", "sidearm", "gun"],
    "smg":     ["smg", "submachine gun", "uzi", "mac-10"],
    "rifle":   ["rifle", "assault rifle", "ar", "carbine"],
    "mg":      ["machine gun", "mg", "lmg"],
    "shotgun": ["shotgun", "scattergun", "pump action"],
    "sniper":  ["sniper rifle", "sniper", "long rifle", "marksman"],
    "heavy":   ["heavy weapon", "rocket launcher", "minigun", "heavy"],
    "thrown":  ["grenade", "throwable", "explosive", "thrown weapon"],
    "stun":    ["stun gun", "taser", "non-lethal"],
    "tool":    ["tool", "gadget", "equipment"],
    "special": ["special weapon", "gadget", "device"],
}

_DAMAGE_TAG: dict[str, str] = {
    "BULLET":    "damage.bullet",
    "MELEE":     "damage.melee",
    "EXPLOSIVE": "damage.explosive",
    "FIRE":      "damage.fire",
    "ELECTRIC":  "damage.electric",
}


def make_weapon_entry(raw: dict, source: str = "DurtyFree/weapons.json") -> RagEntry | None:
    name = raw.get("Name", "")
    model = raw.get("ModelName")
    if not model:
        return None   # no 3D model — internal/NPC-only weapon

    cat  = raw.get("Category") or ""
    dmg  = raw.get("DamageType") or ""
    dlc  = raw.get("DlcName") or ""

    lbl_raw = raw.get("TranslatedLabel", {})
    english = lbl_raw.get("English", "") if isinstance(lbl_raw, dict) else ""
    label   = english or name.replace("WEAPON_", "").replace("_", " ").title()

    cat_tag = _CAT_TAG.get(cat, "ranged")
    tags    = [f"weapon.{cat_tag}"]
    if dmg in _DAMAGE_TAG:
        tags.append(_DAMAGE_TAG[dmg])
    if raw.get("IsVehicleWeapon"):
        tags.append("mount.vehicle")
    if dlc:
        tags.append(f"dlc.{dlc.lower()}")

    aliases = [label.lower()] + _CAT_ALIASES.get(cat_tag, [])
    # add common shorthand
    if "PISTOL" in name:   aliases += ["pistol", "handgun"]
    if "RIFLE" in name:    aliases += ["rifle", "ar"]
    if "SHOTGUN" in name:  aliases += ["shotgun"]
    if "SNIPER" in name:   aliases += ["sniper", "scope"]
    if "GRENADE" in name:  aliases += ["grenade", "frag"]
    if "RPG" in name:      aliases += ["rpg", "rocket launcher", "bazooka"]
    if "MINIGUN" in name:  aliases += ["minigun", "gatling gun"]
    if "KNIFE" in name:    aliases += ["knife", "blade"]
    aliases = list(dict.fromkeys(aliases))

    desc = (
        f"GTA5 weapon {name}. {label}. "
        f"Category: {cat_tag}. Damage type: {dmg.lower()}. "
        f"3D model: {model}. "
        f"Use for: arming NPCs, weapon pickups, mission collectibles."
    )

    entry = RagEntry(
        id=f"gta5:weapon:{model}",
        domain="gta5", kind="weapon",
        canonical=model, label=label,
        aliases=aliases, description=desc,
        tags=tags,
        meta={"weapon_name": name, "category": cat, "damage_type": dmg, "dlc": dlc},
        source=source,
    )
    entry.chunk_text = (
        f"GTA5 weapon model {model}\n"
        f"Label: {label}\n"
        f"Aliases: {', '.join(aliases)}\n"
        f"Tags: {', '.join(tags)}\n"
        f"Description: {desc}"
    )
    return entry
