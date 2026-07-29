"""GTA5 weapon ontology entries."""
from __future__ import annotations
from .base import RagEntry, build_chunk

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
    # "blade" used to live here, which handed it to every bat, hammer, crowbar
    # and golf club in the game. It is now earned per weapon, below.
    "melee":   ["melee weapon", "close combat weapon"],
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

# Melee reads as one undifferentiated blob from the category alone, so the
# individual weapons carry their own words. Keyed on the WEAPON_ name token.
_WEAPON_ALIASES: dict[str, list[str]] = {
    "BAT":          ["baseball bat", "bat", "club"],
    "NIGHTSTICK":   ["nightstick", "baton", "police baton", "billy club"],
    "CROWBAR":      ["crowbar", "pry bar", "wrecking bar"],
    "HAMMER":       ["hammer", "claw hammer"],
    "GOLFCLUB":     ["golf club", "club", "iron"],
    "POOLCUE":      ["pool cue", "cue", "billiard cue"],
    "WRENCH":       ["wrench", "pipe wrench", "spanner"],
    "BOTTLE":       ["broken bottle", "bottle"],
    "KNUCKLE":      ["brass knuckles", "knuckle duster", "knuckles"],
    "FLASHLIGHT":   ["flashlight", "torch"],
    "KNIFE":        ["knife", "blade", "combat knife"],
    "SWITCHBLADE":  ["switchblade", "blade", "flick knife"],
    "DAGGER":       ["dagger", "blade", "antique dagger"],
    "MACHETE":      ["machete", "blade", "cleaver"],
    "HATCHET":      ["hatchet", "axe", "blade"],
    "STONE_HATCHET": ["stone hatchet", "axe", "blade"],
    "BATTLEAXE":    ["battle axe", "axe", "blade"],
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
    # per-weapon words first, longest token wins (STONE_HATCHET before HATCHET)
    for token in sorted(_WEAPON_ALIASES, key=len, reverse=True):
        if token in name.upper():
            aliases += _WEAPON_ALIASES[token]
            break
    # add common shorthand
    if "PISTOL" in name:   aliases += ["pistol", "handgun"]
    if "RIFLE" in name:    aliases += ["rifle", "ar"]
    if "SHOTGUN" in name:  aliases += ["shotgun"]
    if "SNIPER" in name:   aliases += ["sniper", "scope"]
    if "GRENADE" in name:  aliases += ["grenade", "frag"]
    if "RPG" in name:      aliases += ["rpg", "rocket launcher", "bazooka"]
    if "MINIGUN" in name:  aliases += ["minigun", "gatling gun"]
    aliases = list(dict.fromkeys(aliases))

    desc = (
        f"{label} — GTA5 {cat_tag} weapon, {dmg.lower() or 'unspecified'} damage. "
        f"Spawns as model {model}."
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
    entry.chunk_text = build_chunk(
        entry,
        f"GTA5 weapon model {model}",
        {"Name": label, "Weapon": name},
    )
    return entry
