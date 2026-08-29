"""
Canonical tag taxonomy for the RAG ontology.
Tags use dot-notation:  faction.law,  class.super,  emotion.idle

This is the type system for the compiler:
  - Corpus entries are validated against it at write time
  - The resolver enforces it at resolution time
  - The LLM cannot produce tags outside this set
"""

GTA5_PED = {
    "category": ["ambient", "service", "gang", "unique", "cutscene",
                 "story", "multiplayer", "heist"],
    "faction":  ["law", "military", "gang", "criminal", "service",
                 "civilian", "cult"],
    "gender":   ["male", "female"],
    "age":      ["young", "middle", "old"],
    "role":     ["guard", "shooter", "driver", "dealer", "medic",
                 "technician", "civilian", "informant"],
    "weapon":   ["unarmed", "pistol", "smg", "rifle", "shotgun",
                 "sniper", "melee", "heavy"],
    "threat":   ["low", "medium", "high", "boss"],
}

GTA5_VEHICLE = {
    "class":   ["super", "sports", "sports_classics", "muscle", "sedan",
                "coupe", "compact", "suv", "pickup", "van", "offroad",
                "motorcycle", "bicycle", "helicopter", "plane", "boat",
                "emergency", "military", "industrial", "commercial",
                "utility", "service", "train"],
    "type":    ["car", "motorcycle", "bicycle", "helicopter", "plane",
                "boat", "train", "trailer"],
    "faction": ["police", "military", "gang", "civilian", "luxury"],
    "speed":   ["slow", "medium", "fast", "ultra"],
    "seats":   ["1", "2", "4", "4+"],
    "feature": ["sirens", "armed", "unarmed", "convertible", "armored"],
}

GTA5_ANIMATION = {
    "context": ["ambient", "combat", "movement", "ped_action", "cutscene",
                "mission", "vehicle", "gameplay", "special", "transition"],
    "emotion": ["idle", "walk", "run", "sprint", "crouch", "sit", "prone",
                "combat", "vehicle_enter", "vehicle_exit", "drinking",
                "smoking", "phone", "talk", "surrender", "react", "fall",
                "death", "celebrate", "dance", "cheer", "jump", "climb", "swim"],
    "stance":  ["standing", "crouching", "sitting", "prone", "vehicle"],
    "gender":  ["male", "female", "neutral"],
    "weapon":  ["unarmed", "pistol", "smg", "rifle", "shotgun",
                "sniper", "heavy", "melee"],
}

GTA5_SCENARIO = {
    "type":     ["ambient_human", "ambient_vehicle", "ambient_animal",
                 "scripted_human", "prop_animation"],
    "faction":  ["law", "criminal", "service", "civilian"],
    "behavior": ["patrol", "guard", "deal", "loiter", "drinking", "smoking",
                 "phone", "exercise", "leisure", "wander", "cheer", "working",
                 "construction", "gardening", "film", "observe", "atm",
                 "sitting", "resting", "standing", "busking", "protest",
                 "preaching", "ambient"],
}

GTA5_WEAPON = {
    "weapon": ["melee", "pistol", "smg", "rifle", "mg", "shotgun", "sniper",
               "heavy", "thrown", "stun", "unarmed", "tool", "special"],
    "damage": ["bullet", "melee", "explosive", "fire", "electric"],
    "mount":  ["handheld", "vehicle"],
}

GTA5_PROP = {
    "prop":    ["electronic", "container", "valuables", "contraband",
                "industrial", "military", "explosive", "access",
                "medical", "documents", "collectible", "misc"],
    "mission": ["heist", "casino_heist", "biker", "gunrunning", "dlc",
                "trevor", "story"],
    "context": ["cutscene", "ambient", "scripted"],
}

GTA5_AUDIO = {
    "type":    ["ambient", "script", "music", "mission", "complete",
                "speech", "sting", "sfx"],
    "emotion": ["victory", "defeat"],
    "context": ["alert", "chase", "combat", "ambient"],
    "faction": ["law"],
}

UNITY_PACKAGE = {
    "pipeline": ["urp", "hdrp", "builtin", "any"],
    "domain":   ["rendering", "audio", "physics", "ai", "ui", "input",
                 "animation", "network", "tools", "vfx", "terrain",
                 "cinemachine", "testing", "localization", "2d", "xr",
                 "timeline", "shadergraph"],
    "category": ["camera", "rendering", "tools", "testing", "simulation",
                 "input", "physics", "audio", "network", "ai", "xr"],
}

UNITY_COMPONENT = {
    "category":  ["rendering", "physics_3d", "physics_2d", "audio", "animation",
                  "navigation", "ui", "ui_toolkit", "event", "network", "input",
                  "timeline", "cinemachine", "effects", "terrain", "xr", "lod",
                  "transform", "postprocess", "scripting"],
    "pipeline":  ["urp", "hdrp", "builtin", "any"],
    "requires":  ["Rigidbody", "Collider", "Collider2D", "Camera", "Animator",
                  "Canvas", "MeshFilter", "SkinnedMeshRenderer", "NetworkObject",
                  "PlayableDirector", "com.unity.cinemachine", "com.unity.ai.navigation",
                  "com.unity.netcode.gameobjects", "com.unity.textmeshpro",
                  "com.unity.inputsystem", "com.unity.xr.coreutils",
                  "com.unity.xr.interaction.toolkit", "AnimatorController"],
    "is":        ["physics", "collider", "character", "renderable", "skinned",
                  "2d", "audio", "audio_listener", "animation", "navigation",
                  "ai", "baking", "ui", "container", "image", "text", "interactive",
                  "scroll", "layout", "texture", "ui_toolkit", "timeline",
                  "cutscene", "camera", "cinemachine", "tps", "input", "multiplayer",
                  "network", "transform", "vehicle", "wheel", "joint", "cloth",
                  "vfx", "particle", "reflection", "lod", "terrain", "xr", "vr",
                  "ar", "scripting", "mesh", "postprocess"],
}

UNITY_API_VERB = {
    "subsystem": ["transform", "rigidbody", "animator", "audio", "navmesh",
                  "gameobject", "physics", "camera", "cinemachine", "ui",
                  "timeline", "scene", "coroutine", "save", "screen"],
    "action":    ["move", "rotate", "scale", "hierarchy", "physics", "animate",
                  "audio", "navigate", "spawn", "destroy", "toggle", "query",
                  "modify", "camera", "ui", "cutscene", "scene", "async",
                  "save", "visual", "misc"],
}

UNITY_SCENE_RULE = {
    "rule":     ["required", "forbidden", "warning"],
    "pipeline": ["urp", "hdrp", "builtin", "any"],
    "affects":  ["camera", "rendering", "audio", "physics", "navigation", "ui",
                 "animation", "network", "input", "terrain", "xr", "lighting",
                 "performance", "scripting", "shaders", "materials", "postprocess",
                 "character", "cinemachine"],
}

UNITY_GENRE_ATOM = {
    "genre":       ["universal", "fps", "rpg", "platformer", "horror",
                    "racing", "tower_defence", "puzzle", "stealth", "strategy",
                    "fighting"],
    "action_type": ["spawn", "destroy", "trigger", "timer", "cutscene", "visual",
                    "ui", "save", "audio", "combat", "movement", "pickup", "ai",
                    "dialogue", "quest", "progression", "interaction", "mechanic",
                    "hazard", "camera", "environment", "scare", "build", "toggle"],
}

# ── Registry ─────────────────────────────────────────────────────────────────
ALL_DOMAINS: dict[str, dict[str, dict[str, list[str]]]] = {
    "gta5": {
        "ped":       GTA5_PED,
        "vehicle":   GTA5_VEHICLE,
        "animation": GTA5_ANIMATION,
        "scenario":  GTA5_SCENARIO,
        "audio":     GTA5_AUDIO,
        "weapon":    GTA5_WEAPON,
        "prop":      GTA5_PROP,
    },
    "unity": {
        "package":    UNITY_PACKAGE,
        "component":  UNITY_COMPONENT,
        "api_verb":   UNITY_API_VERB,
        "scene_rule": UNITY_SCENE_RULE,
        "genre_atom": UNITY_GENRE_ATOM,
    },
}


def valid_tags(domain: str, kind: str) -> set[str]:
    """Return all valid dot-notation tags for a domain+kind pair."""
    taxonomy = ALL_DOMAINS.get(domain, {}).get(kind, {})
    return {f"{ns}.{v}" for ns, vals in taxonomy.items() for v in vals}


def validate_tags(tags: list[str], domain: str, kind: str) -> tuple[list[str], list[str]]:
    """Returns (valid, unknown) tag lists. Unknown tags are not an error — just a signal."""
    known = valid_tags(domain, kind)
    if not known:
        return tags, []
    valid   = [t for t in tags if t in known]
    unknown = [t for t in tags if t not in known]
    return valid, unknown
