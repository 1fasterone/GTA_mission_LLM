"""
Step 6 — .NET 4.8 C# code generator.
LLM never touches this. Jinja2 template + pre-processed schema data only.
"""

import json
import math
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .mission_schema import MissionSchema


# ── Weapon mapping: schema enum → GTA5 native weapon string ──────────────────
# Strings passed to GET_HASH_KEY at runtime — covers DLC weapons with no enum.
_WEAPON_NAME = {
    "pistol":   "weapon_pistol",
    "smg":      "weapon_microsmg",
    "rifle":    "weapon_assaultrifle",
    "shotgun":  "weapon_pumpshotgun",
    "sniper":   "weapon_sniperrifle",
    "none":     None,
}

# ── Weather: schema name → GTA5 native string ────────────────────────────────
_WEATHER_NATIVE = {
    "CLEAR":      "CLEAR",
    "OVERCAST":   "OVERCAST",
    "RAIN":       "RAIN",
    "THUNDER":    "THUNDER",
    "FOGGY":      "FOGGY",
    "SMOG":       "SMOG",
    "EXTRASUNNY": "EXTRASUNNY",
    "SNOW":       "XMAS",
}

# ── AI rival difficulty → cruise speed curve (mirrors the hand-tuned HeliRace
# mod's GetAICruiseSpeed table). "medium" reproduces the historical hardcoded
# 40f/45f values so existing generated output is unaffected by default. ─────
_AI_DIFFICULTY_ORDER = ["easy", "medium", "hard"]
_AI_SPAWN_SPEED = {"easy": 28.0, "medium": 40.0, "hard": 55.0}
_AI_CHASE_SPEED = {"easy": 32.0, "medium": 45.0, "hard": 65.0}

# ── Ordered weather options for the optional in-mission config menu ─────────
_WEATHER_MENU_OPTIONS = [
    "CLEAR", "OVERCAST", "RAIN", "THUNDER", "FOGGY", "SMOG", "EXTRASUNNY",
]

# ── Mid-mission event constants (deterministic, template-owned) ──────────────
# The police_pursuit interceptor mirrors the hand-tuned HeliRace mod's Buzzard
# chase; fixed models keep Step 6 a pure function of the schema.
_EVENT_HELI_MODEL  = "buzzard"
_EVENT_PILOT_MODEL = "s_m_y_pilot_01"

# ── Keybind-triggered spawns land around the PLAYER'S position at press-time
# (mirrors the gta-spawner micro's "press F6, spawn around me" convention) —
# not the fixed mission SpawnOrigin, which may be far away by the time a
# keybind fires mid-mission. Fixed radius keeps Step 6 a pure function.
_KEYBIND_SPAWN_RADIUS = 8.0

_PATROL_ROUTE_MODE_CS = {
    "once": "Once",
    "loop": "Loop",
    "ping_pong": "PingPong",
}

_PATROL_ROUTE_CONDITION_CS = {
    "combat": "Combat",
    "alert": "Alert",
    "player_in_trigger": "PlayerInTrigger",
    "objective_state": "ObjectiveState",
}

_PATROL_ROUTE_EDGE_CS = {
    "": "Level",
    "enter": "Enter",
    "inside": "Inside",
    "exit": "Exit",
}

_PATROL_ROUTE_OBJECTIVE_STATE_CS = {
    "pending": "Pending",
    "active": "Active",
    "complete": "Complete",
    "failed": "Failed",
}

_PATROL_ROUTE_REARM_CS = {
    "clear": "Clear",
    "once": "Once",
}

_PATROL_ROUTE_ACTION_CS = {
    "none": "None",
    "pause": "Pause",
    "resume": "Resume",
    "restart": "Restart",
    "switch_path": "SwitchPath",
    "guard": "Guard",
}

_PATROL_BEHAVIOR_CS = {
    "walk": "Walk",
    "jog": "Jog",
    "sprint": "Sprint",
    "scan": "Scan",
    "guard": "Guard",
}

_PATROL_MOVEMENT_BEHAVIOR = {
    "walk": "walk",
    "jog": "jog",
    "run": "sprint",
}

_PATROL_MOVEMENT_SPEED = {
    "walk": 1.0,
    "jog": 2.2,
    "run": 3.5,
}

_PATROL_POSTURE_CS = {
    "normal": "Normal",
    "cautious": "Cautious",
    "aggressive": "Aggressive",
}

_PATROL_WEAPON_STATE_CS = {
    "unchanged": "Unchanged",
    "drawn": "Drawn",
    "holstered": "Holstered",
}

_PATROL_LOOK_TARGET_KIND_CS = {
    "none": "None",
    "coord": "Coord",
    "ped": "Ped",
    "vehicle": "Vehicle",
    "object": "Object",
}

_PATROL_LOOK_TARGET_TYPES = {
    "none", "coord", "ped", "vehicle", "object", "trigger", "logic",
}

# ── Named tracks → ordered checkpoint arrays ─────────────────────────────────
def _load_tracks(lookups_dir: str = "lookups") -> dict:
    path = Path(lookups_dir) / "tracks.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

_TRACKS: dict = _load_tracks()

# ── Named location → Vector3 spawn point ─────────────────────────────────────
_LOCATIONS: dict[str, dict] = {
    "docks":         {"x": -1009.0, "y": -2704.0, "z": 13.8},
    "airport":       {"x": -1336.0, "y": -3044.0, "z": 13.9},
    "downtown":      {"x":   250.0, "y":  -850.0, "z": 29.7},
    "vinewood":      {"x":   604.0, "y":   108.0, "z": 100.0},
    "sandy_shores":  {"x":  1800.0, "y":  3800.0, "z": 32.0},
    "paleto_bay":    {"x":  -105.0, "y":  6300.0, "z": 31.0},
    "alamo_sea":     {"x":  1200.0, "y":  2700.0, "z": 38.0},
    "military_base": {"x": -2258.0, "y":  3012.0, "z": 32.0},
    "prison":        {"x":  1695.0, "y":  2440.0, "z": 45.0},
    "port":          {"x":   830.0, "y": -2900.0, "z":  5.8},
    "highway":       {"x":  1003.0, "y": -1700.0, "z": 52.0},
}

# ── FIX 1: Spawn spread offsets ───────────────────────────────────────────────
# Each ped/vehicle GROUP gets a fixed base offset so groups never overlap.
# Within a group, the loop index 'i' spreads individuals further in the same dir.

_PED_OFFSETS: list[tuple[float, float]] = [
    (  6.0,   0.0),   # group 0 — east
    ( -6.0,   6.0),   # group 1 — northwest
    (  0.0,  -6.0),   # group 2 — south
    (  8.0,  -5.0),   # group 3 — southeast
    ( -4.0,  -8.0),   # group 4 — southwest
    (  9.0,   5.0),   # group 5 — east-northeast
    ( -9.0,  -3.0),   # group 6 — west
]

_VEHICLE_OFFSETS: list[tuple[float, float]] = [
    (  0.0, -12.0),   # slot 0 — directly behind
    ( 10.0, -12.0),   # slot 1 — behind-right
    (-10.0, -12.0),   # slot 2 — behind-left
    (  0.0, -22.0),   # slot 3 — further back
]

_PROP_OFFSETS: list[tuple[float, float]] = [
    (  5.0,   5.0),   # pickup 0
    ( -5.0,   5.0),   # pickup 1
    (  0.0,   8.0),   # pickup 2
]


def _to_class_name(mission_name: str) -> str:
    """'The Harbor Job' → 'TheHarborJob'.  Prepends 'Mission' if result starts with a digit."""
    name = re.sub(r"[^a-zA-Z0-9]+", " ", mission_name).title().replace(" ", "")
    if name and name[0].isdigit():
        name = "Mission" + name
    return name or "MissionUnnamed"


def _escape_cs_string(s: str) -> str:
    """Escape a plain string for use inside a C# double-quoted string literal."""
    return (
        s.replace("\\", "\\\\")
         .replace('"',  '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )


def _float_or_default(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _patrol_speed(value, default: float = 1.0) -> float:
    speed = _float_or_default(value, default)
    if speed <= 0:
        speed = default
    return max(0.25, min(speed, 8.0))


def _patrol_wait_ms(value, default_seconds: float = 0.0) -> int:
    seconds = _float_or_default(value, default_seconds)
    if seconds <= 0:
        return 0
    return int(max(0, min(round(seconds * 1000), 600000)))


def _patrol_behavior(value, default: str = "walk") -> str:
    behavior = str(value or default or "walk").lower()
    if behavior in _PATROL_BEHAVIOR_CS:
        return behavior
    if default in _PATROL_BEHAVIOR_CS:
        return default
    return "walk"


def _patrol_movement(value, default: str = "inherit") -> str:
    movement = str(value or default or "inherit").lower()
    if movement in _PATROL_MOVEMENT_BEHAVIOR or movement == "inherit":
        return movement
    if default in _PATROL_MOVEMENT_BEHAVIOR or default == "inherit":
        return default
    return "inherit"


def _patrol_posture(value, default: str = "normal") -> str:
    posture = str(value or default or "normal").lower()
    if posture in _PATROL_POSTURE_CS:
        return posture
    if default in _PATROL_POSTURE_CS:
        return default
    return "normal"


def _patrol_weapon_state(value, default: str = "unchanged") -> str:
    state = str(value or default or "unchanged").lower()
    if state in _PATROL_WEAPON_STATE_CS:
        return state
    if default in _PATROL_WEAPON_STATE_CS:
        return default
    return "unchanged"


def _patrol_look_target_type(value, default: str = "none") -> str:
    target_type = str(value or default or "none").lower()
    if target_type in _PATROL_LOOK_TARGET_TYPES:
        return target_type
    if default in _PATROL_LOOK_TARGET_TYPES:
        return default
    return "none"


def _patrol_angle(value, default=None):
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(angle):
        return default
    return angle % 360.0


def _patrol_arc(value, default=None):
    try:
        arc = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(arc):
        return default
    return max(0.0, min(arc, 360.0))


def _patrol_behavior_with_movement(behavior: str, movement: str) -> str:
    if behavior in {"walk", "jog", "sprint"} and movement in _PATROL_MOVEMENT_BEHAVIOR:
        return _PATROL_MOVEMENT_BEHAVIOR[movement]
    return behavior


def _patrol_behavior_with_posture(behavior: str, posture: str) -> str:
    # Cautious peds should not keep a sprint task even when the route alias
    # asked for one. Aggressive speed is applied at runtime without rewriting
    # the authored behavior tier.
    if posture == "cautious" and behavior == "sprint":
        return "jog"
    return behavior


def _bool_or_default(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _unique_editor_id_map(items: list[dict]) -> dict[str, int]:
    """Return exact stable-id bindings, excluding ambiguous or empty IDs."""
    resolved: dict[str, int] = {}
    duplicates: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id") or "").strip()
        if not ident:
            continue
        if ident in resolved:
            duplicates.add(ident)
        else:
            resolved[ident] = idx
    for ident in duplicates:
        resolved.pop(ident, None)
    return resolved


def _normalize_editor_route_logic(
    raw_rules,
    source_path_idx: int,
    source_path: dict,
    editor_paths: list[dict],
    trigger_ids: dict[str, int],
    objective_ids: dict[str, int],
    objective_count: int,
) -> list[dict]:
    """Resolve editor route rules into fixed, audit-friendly runtime fields."""
    if raw_rules is None:
        return []
    rules = raw_rules if isinstance(raw_rules, list) else [raw_rules]
    authored_ids = [
        str(rule.get("id") or "").strip() if isinstance(rule, dict) else ""
        for rule in rules
    ]
    duplicate_ids = {
        ident for ident in authored_ids if ident and authored_ids.count(ident) > 1
    }

    normalized: list[dict] = []
    for rule_idx, raw_rule in enumerate(rules):
        reasons: list[str] = []
        rule: dict[str, Any] = raw_rule if isinstance(raw_rule, dict) else {}
        if not isinstance(raw_rule, dict):
            reasons.append("rule must be an object")

        rule_id = authored_ids[rule_idx] or f"route_rule_{rule_idx}"
        if not authored_ids[rule_idx]:
            reasons.append("rule needs a stable id")
        if authored_ids[rule_idx] in duplicate_ids:
            reasons.append(f'rule id "{authored_ids[rule_idx]}" is duplicated')

        priority_value = rule.get("priority", 0)
        try:
            priority = int(priority_value)
        except (TypeError, ValueError):
            priority = 0
            reasons.append("priority must be an integer from -1000 to 1000")
        if priority < -1000 or priority > 1000:
            reasons.append("priority must be an integer from -1000 to 1000")

        raw_when = rule.get("when")
        when: dict[str, Any] = raw_when if isinstance(raw_when, dict) else {}
        condition = str(when.get("type") or "").strip().lower()
        condition_cs = _PATROL_ROUTE_CONDITION_CS.get(condition, "None")
        edge = str(when.get("edge") or "").strip().lower()
        edge_cs = _PATROL_ROUTE_EDGE_CS.get(edge, "Level")
        trigger_index = -1
        objective_index = -1
        objective_state_cs = "Pending"

        if condition not in _PATROL_ROUTE_CONDITION_CS:
            reasons.append(f'condition "{condition or "(missing)"}" is unsupported')
        elif condition in {"combat", "alert"}:
            if str(when.get("source") or "").strip().lower() != "self":
                reasons.append(f"{condition} source must be self")
            if edge:
                reasons.append(f"{condition} does not use a trigger edge")
        elif condition == "player_in_trigger":
            trigger_ref = str(when.get("trigger_id") or "").strip()
            trigger_index = trigger_ids.get(trigger_ref, -1)
            if not trigger_ref:
                reasons.append("player-in-trigger condition needs a trigger id")
            elif trigger_index < 0:
                reasons.append(f'trigger "{trigger_ref}" is missing or duplicated')
            if edge not in {"enter", "inside", "exit"}:
                reasons.append("trigger edge must be enter, inside, or exit")
            edge_cs = _PATROL_ROUTE_EDGE_CS.get(edge, "Enter")
        elif condition == "objective_state":
            step_ref = when.get("step_ref")
            if isinstance(step_ref, int) and not isinstance(step_ref, bool):
                objective_index = step_ref if 0 <= step_ref < objective_count else -1
            else:
                step_text = str(step_ref or "").strip()
                if step_text.isdigit():
                    candidate = int(step_text)
                    objective_index = candidate if 0 <= candidate < objective_count else -1
                else:
                    objective_index = objective_ids.get(step_text, -1)
            if objective_index < 0:
                reasons.append(f'objective "{step_ref}" is missing or duplicated')
            objective_state = str(when.get("state") or "").strip().lower()
            objective_state_cs = _PATROL_ROUTE_OBJECTIVE_STATE_CS.get(objective_state, "Pending")
            if objective_state not in _PATROL_ROUTE_OBJECTIVE_STATE_CS:
                reasons.append(
                    "objective state must be pending, active, complete, or failed"
                )
            if edge:
                reasons.append("objective state does not use a trigger edge")

        def normalize_action(raw_action, phase: str) -> tuple[str, int]:
            action = raw_action if isinstance(raw_action, dict) else {}
            action_name = str(action.get("action") or "").strip().lower()
            allowed = {"pause", "restart", "switch_path", "guard"} if phase == "enter" else {"none", "resume"}
            action_cs = _PATROL_ROUTE_ACTION_CS.get(action_name, "None")
            if action_name not in allowed:
                reasons.append(
                    f'on-{phase} action "{action_name or "(missing)"}" is unsupported'
                )
                return action_cs, -1
            if action_name != "switch_path":
                return action_cs, -1
            path_ref = str(action.get("path_id") or "").strip()
            target_matches = [
                idx for idx, candidate in enumerate(editor_paths)
                if str(candidate.get("id") or "").strip() == path_ref
            ]
            target_idx = target_matches[0] if len(target_matches) == 1 else -1
            if not path_ref:
                reasons.append("switch path has no destination")
            elif not target_matches:
                reasons.append(f'switch path "{path_ref}" is missing')
            elif len(target_matches) > 1:
                reasons.append(f'switch path "{path_ref}" is ambiguous')
            elif (
                source_path.get("ped_idx") is None
                or editor_paths[target_idx].get("ped_idx") != source_path.get("ped_idx")
            ):
                reasons.append(f'switch path "{path_ref}" must use the same patrolling ped')
                target_idx = -1
            return action_cs, target_idx

        on_enter_cs, target_path_index = normalize_action(rule.get("on_enter"), "enter")
        on_exit_cs, exit_target_path_index = normalize_action(rule.get("on_exit"), "exit")
        if exit_target_path_index >= 0:
            target_path_index = exit_target_path_index

        rearm = str(rule.get("rearm") or "").strip().lower()
        rearm_cs = _PATROL_ROUTE_REARM_CS.get(rearm, "Clear")
        if rearm not in _PATROL_ROUTE_REARM_CS:
            reasons.append("rearm must be clear or once")

        normalized.append({
            "id": rule_id,
            "id_cs": _escape_cs_string(rule_id),
            "priority": priority,
            "condition_cs": condition_cs,
            "edge_cs": edge_cs,
            "trigger_index": trigger_index,
            "objective_index": objective_index,
            "objective_state_cs": objective_state_cs,
            "on_enter_cs": on_enter_cs,
            "on_exit_cs": on_exit_cs,
            "target_path_index": target_path_index,
            "rearm_cs": rearm_cs,
            "valid": not reasons,
            "reason": "; ".join(dict.fromkeys(reasons)),
            "source_path_idx": source_path_idx,
        })
    return normalized


def _build_template_context(schema: MissionSchema) -> dict:
    location    = dict(_LOCATIONS.get(schema.location_name, _LOCATIONS["docks"]))
    class_name  = _to_class_name(schema.mission_name)
    weather_str = _WEATHER_NATIVE.get(schema.weather.upper(), "CLEAR")
    _editor = (schema.meta or {}).get("editor") if isinstance(schema.meta, dict) else None

    def _editor_point(value, fallback_z=None):
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return {
                "x": float(value[0]),
                "y": float(value[1]),
                "z": float(value[2]) if len(value) > 2 else float(fallback_z if fallback_z is not None else location["z"]),
            }
        if isinstance(value, dict):
            return {
                "x": float(value.get("x", 0.0)),
                "y": float(value.get("y", 0.0)),
                "z": float(value.get("z", fallback_z if fallback_z is not None else location["z"])),
            }
        return None

    def _coord_reference(value):
        point = _editor_point(value, location["z"])
        if point is not None:
            return point
        if not isinstance(value, str):
            return None
        parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
        if len(parts) < 2:
            return None
        try:
            return {
                "x": float(parts[0]),
                "y": float(parts[1]),
                "z": float(parts[2]) if len(parts) > 2 else float(location["z"]),
            }
        except (TypeError, ValueError):
            return None

    def _editor_entry(kind: str, idx: int) -> dict:
        if not isinstance(_editor, dict):
            return {}
        entries = _editor.get(kind) or []
        if 0 <= idx < len(entries) and isinstance(entries[idx], dict):
            return entries[idx]
        return {}

    def _reference_matches(reference, kind: str, idx: int, item=None, meta=None) -> bool:
        needle = str(reference or "").strip().lower()
        if not needle:
            return False
        candidates = {str(idx), f"{kind}{idx}", f"{kind}_{idx}", f"{kind} {idx}"}
        for source in (item, meta):
            if isinstance(source, dict):
                values = (source.get("id"), source.get("name"), source.get("description"),
                          source.get("model"), source.get("label"))
            else:
                values = (getattr(source, "id", None), getattr(source, "name", None),
                          getattr(source, "description", None), getattr(source, "model", None),
                          getattr(source, "label", None))
            candidates.update(str(value).strip().lower() for value in values if value)
        return needle in candidates

    def _runtime_entity_index(kind: str, idx: int) -> int:
        if kind == "ped":
            return sum(int(item.count) for item in schema.peds[:idx] if item.model)
        if kind == "vehicle":
            return sum(int(item.count) for item in schema.vehicles[:idx] if item.model)
        if kind == "object":
            return sum(1 for item in schema.objects[:idx] if item.model)
        return -1

    def _empty_look_target() -> dict:
        return {
            "kind": "none",
            "kind_cs": "None",
            "index": -1,
            "coord": {"x": 0.0, "y": 0.0, "z": 0.0},
        }

    def _coord_look_target(point: dict) -> dict:
        return {"kind": "coord", "kind_cs": "Coord", "index": -1, "coord": point}

    def _trigger_coord(reference):
        if not isinstance(_editor, dict):
            return None
        for idx, trigger in enumerate(_editor.get("triggers") or []):
            if not isinstance(trigger, dict):
                continue
            if _reference_matches(reference, "trg", idx, meta=trigger):
                return _editor_point(trigger.get("pos"), location["z"])
        return None

    def _logic_coord(reference):
        if not isinstance(_editor, dict):
            return None
        logic = _editor.get("logic")
        nodes = logic.get("nodes") if isinstance(logic, dict) else []
        for idx, node in enumerate(nodes or []):
            if not isinstance(node, dict) or not _reference_matches(reference, "logic", idx, meta=node):
                continue
            params = node.get("params") if isinstance(node.get("params"), dict) else {}
            for value in (
                node.get("pos"), node.get("position"), node.get("coord"),
                params.get("pos"), params.get("position"), params.get("coord"),
            ):
                point = _coord_reference(value)
                if point is not None:
                    return point
            trigger_point = _trigger_coord(params.get("ref") or params.get("trigger_id"))
            if trigger_point is not None:
                return trigger_point
        return None

    def _resolve_editor_look_target(target_type: str, reference) -> dict:
        target_type = _patrol_look_target_type(target_type, "none")
        if target_type == "none":
            return _empty_look_target()
        if target_type == "coord":
            point = _coord_reference(reference)
            return _coord_look_target(point) if point is not None else _empty_look_target()
        if target_type == "trigger":
            point = _trigger_coord(reference)
            return _coord_look_target(point) if point is not None else _empty_look_target()
        if target_type == "logic":
            point = _logic_coord(reference)
            return _coord_look_target(point) if point is not None else _empty_look_target()

        items = getattr(schema, f"{target_type}s", [])
        for idx, item in enumerate(items):
            meta = _editor_entry(f"{target_type}s", idx)
            if not _reference_matches(reference, target_type, idx, item=item, meta=meta):
                continue
            point = _editor_point(meta.get("pos"), location["z"])
            runtime_idx = _runtime_entity_index(target_type, idx)
            if getattr(item, "model", None) and runtime_idx >= 0:
                return {
                    "kind": target_type,
                    "kind_cs": _PATROL_LOOK_TARGET_KIND_CS[target_type],
                    "index": runtime_idx,
                    "coord": point or {"x": 0.0, "y": 0.0, "z": 0.0},
                }
            if point is not None:
                return _coord_look_target(point)
        return _empty_look_target()

    editor_blips: list[dict] = []
    if isinstance(_editor, dict):
        for blip in (_editor.get("blips") or []):
            if not isinstance(blip, dict):
                continue
            point = _editor_point(blip.get("pos"), location["z"])
            if point is None:
                continue
            point["label"] = str(blip.get("label") or "")
            point["step_index"] = blip.get("step_index") if isinstance(blip.get("step_index"), int) else None
            point["objective_id"] = str(blip.get("objective_id") or "")
            editor_blips.append(point)
    start_blip_idx = None
    if editor_blips:
        start_blip_idx = next(
            (idx for idx, b in enumerate(editor_blips)
             if b["label"].strip().lower() in {"start", "mission start"}),
            0,
        )
        start_blip = editor_blips[start_blip_idx]
        location.update({"x": start_blip["x"], "y": start_blip["y"], "z": start_blip["z"]})

    # Step-linked editor blips are objective targets, separate from the mission
    # start and from patrol points. Prefer the explicit step index, then the
    # stable editor objective id.
    step_editor_targets: dict[int, dict] = {}
    linked_editor_blip_indices: set[int] = set()
    for step_index, step in enumerate(schema.steps):
        editor_id = str(step.params.get("editor_id") or "")
        match = next(
            ((idx, blip) for idx, blip in enumerate(editor_blips)
             if blip.get("step_index") == step_index),
            None,
        )
        if match is None and editor_id:
            match = next(
                ((idx, blip) for idx, blip in enumerate(editor_blips)
                 if blip.get("objective_id") == editor_id),
                None,
            )
        if match is not None:
            idx, blip = match
            step_editor_targets[step_index] = blip
            linked_editor_blip_indices.add(idx)

    # ── Waypoints (one per go_to step) ───────────────────────────────────────
    waypoints: list[dict] = []
    waypoint_idx: dict[int, int] = {}
    wp_counter = 0
    # Waypoints spread in compass directions so they aren't all on the same diagonal.
    _WP_DIRS = [
        ( 60,   0),   # east
        (  0,  60),   # north
        (-60,   0),   # west
        (  0, -60),   # south
        ( 45,  45),   # northeast
        (-45,  45),   # northwest
        (-45, -45),   # southwest
        ( 45, -45),   # southeast
    ]
    used_editor_blips: set[int] = set(linked_editor_blip_indices)
    if start_blip_idx is not None:
        used_editor_blips.add(start_blip_idx)

    def _editor_waypoint_for_step(step_index: int, step) -> dict | None:
        linked = step_editor_targets.get(step_index)
        if linked is not None:
            return linked
        for idx, blip in enumerate(editor_blips):
            if idx not in used_editor_blips and blip.get("step_index") is None and not blip.get("objective_id"):
                used_editor_blips.add(idx)
                return blip
        return None

    for i, step in enumerate(schema.steps):
        if step.type == "go_to":
            editor_wp = _editor_waypoint_for_step(i, step)
            if editor_wp is None:
                dx, dy = _WP_DIRS[wp_counter % len(_WP_DIRS)]
                waypoints.append({
                    "x":     location["x"] + dx,
                    "y":     location["y"] + dy,
                    "z":     location["z"],
                    "label": step.label,
                })
            else:
                waypoints.append({
                    "x":     editor_wp["x"],
                    "y":     editor_wp["y"],
                    "z":     editor_wp["z"],
                    "label": step.label or editor_wp.get("label", ""),
                })
            waypoint_idx[i] = wp_counter
            wp_counter += 1

    # ── Race checkpoints (one entry per checkpoint / race_lap step) ──────────
    race_checkpoints: list[dict] = []
    race_checkpoint_idx: dict[int, int] = {}   # step_index → race_checkpoints list index
    rc_counter = 0

    for i, step in enumerate(schema.steps):
        if step.type not in ("checkpoint", "race_lap"):
            continue

        # ── Editor-drawn checkpoints win (exact map→game fidelity) ──────────
        # When the Mission Editor draws a race line it writes the literal points
        # into params.checkpoints. Those are the user's intent — use them verbatim,
        # never snap to a named track.
        drawn = step.params.get("checkpoints")
        if isinstance(drawn, list) and len(drawn) >= 2:
            raw_pts = []
            for q in drawn:
                if isinstance(q, (list, tuple)) and len(q) >= 2:
                    raw_pts.append({"x": float(q[0]), "y": float(q[1]),
                                    "z": float(q[2]) if len(q) > 2 else location["z"]})
                elif isinstance(q, dict):
                    raw_pts.append({"x": float(q.get("x", 0.0)), "y": float(q.get("y", 0.0)),
                                    "z": float(q.get("z", float(location["z"])))})
            if len(raw_pts) >= 2:
                race_checkpoints.append({
                    "array_name":      f"RaceCheckpoints{rc_counter}",
                    "points":          raw_pts,
                    "lap_count":       int(step.params.get("lap_count", 3)),
                    "require_vehicle": bool(step.params.get("require_vehicle", False)),
                    "is_race_lap":     step.type == "race_lap",
                })
                race_checkpoint_idx[i] = rc_counter
                rc_counter += 1
                continue

        track_key = step.params.get("track", "").strip()
        loc_prefix = schema.location_name + "."

        # Allow shorthand like "figure_8" — prepend location if not already qualified
        if track_key and "." not in track_key:
            track_key = loc_prefix + track_key

        if track_key and track_key in _TRACKS:
            raw_pts = _TRACKS[track_key]["checkpoints"]
        else:
            # A race/checkpoint step should never degrade to a meaningless spiral.
            # Snap to the location's real circuit so the course is actual geometry.
            from .archetypes import track_for_location
            fallback_key = track_for_location(schema.location_name)
            if fallback_key in _TRACKS:
                raw_pts = _TRACKS[fallback_key]["checkpoints"]
            else:
                # Last resort only (no tracks file): tight loop around spawn origin.
                n = step.params.get("checkpoint_count", 6)
                raw_pts = []
                for ci in range(n):
                    dx, dy = _WP_DIRS[ci % len(_WP_DIRS)]
                    raw_pts.append({
                        "x": location["x"] + dx * 0.5,
                        "y": location["y"] + dy * 0.5,
                        "z": location["z"],
                    })

        race_checkpoints.append({
            "array_name":      f"RaceCheckpoints{rc_counter}",
            "points":          raw_pts,
            "lap_count":       int(step.params.get("lap_count", 3)),
            "require_vehicle": bool(step.params.get("require_vehicle", False)),
            "is_race_lap":     step.type == "race_lap",
        })
        race_checkpoint_idx[i] = rc_counter
        rc_counter += 1

    # ── Scavenger point sets (one entry per scavenger step) ──────────────────
    scavenger_sets: list[dict] = []
    scavenger_idx: dict[int, int] = {}   # step_index → scavenger_sets list index
    sv_counter = 0
    for i, step in enumerate(schema.steps):
        if step.type != "scavenger":
            continue
        count  = int(step.params.get("target_count", 5))
        radius = float(step.params.get("radius", 3.0))

        # ── Editor-drawn locations win (exact map→game fidelity) ────────────
        # Same shape-parsing as race 'checkpoints': list/tuple or {x,y,z} entries.
        drawn = step.params.get("locations")
        pts: list[dict] = []
        if isinstance(drawn, list) and len(drawn) >= 2:
            for q in drawn:
                if isinstance(q, (list, tuple)) and len(q) >= 2:
                    pts.append({"x": float(q[0]), "y": float(q[1]),
                                "z": float(q[2]) if len(q) > 2 else location["z"]})
                elif isinstance(q, dict):
                    pts.append({"x": float(q.get("x", 0.0)), "y": float(q.get("y", 0.0)),
                                "z": float(q.get("z", float(location["z"])))})
            if len(pts) < 2:
                pts = []
        if not pts:
            # Deterministic compass scatter around the location origin (~90 m),
            # widening ring per lap of the 8 directions so counts > 8 stay distinct.
            for k in range(count):
                dx, dy = _WP_DIRS[k % len(_WP_DIRS)]
                ring = 1.0 + (k // len(_WP_DIRS)) * 0.8
                pts.append({"x": location["x"] + dx * 1.5 * ring,
                            "y": location["y"] + dy * 1.5 * ring,
                            "z": location["z"]})

        scavenger_sets.append({
            "array_name": f"ScavengerPoints{sv_counter}",
            "points":     pts,
            "radius":     radius,
        })
        scavenger_idx[i] = sv_counter
        sv_counter += 1

    # ── Steps with pre-computed next-waypoint index ───────────────────────────
    processed_steps = []
    for i, step in enumerate(schema.steps):
        # Find the first go_to step that comes AFTER step i and has a waypoint assigned.
        next_wp = None
        for j in sorted(waypoint_idx):
            if j > i:
                next_wp = waypoint_idx[j]
                break
        processed_steps.append({
            "index":               i,
            "type":                step.type,
            "label":               _escape_cs_string(step.label),
            "waypoint_idx":        waypoint_idx.get(i),
            "next_waypoint":       next_wp,
            "editor_target":       step_editor_targets.get(i),
            "race_checkpoint_idx": race_checkpoint_idx.get(i),          # None for non-racing steps
            "timer_seconds":       int(step.params.get("seconds", 120)) if step.type == "timer" else None,
            "scavenger_idx":       scavenger_idx.get(i),                # None for non-scavenger steps
            "scavenger_radius":    float(step.params.get("radius", 3.0)) if step.type == "scavenger" else None,
        })

    # ── Patrol-path → ped binding ─────────────────────────────────────────────
    # editor.paths[i].ped_idx points at a schema ped; build ped_idx → path-slot so
    # SpawnPeds can hand that ped its waypoint loop. Computed here (pre-peds) so the
    # ped dict can carry a `patrol_path_idx`.
    def _editor_pos(kind: str, idx: int):
        """Absolute [x,y,z,(heading)] the editor placed for entity `idx`, or None.
        This is what makes 'icon on the map' === 'spawn position in game'."""
        if not isinstance(_editor, dict):
            return None
        arr = _editor.get(kind) or []
        if idx < len(arr) and isinstance(arr[idx], dict):
            pos = arr[idx].get("pos")
            if isinstance(pos, list) and len(pos) >= 3:
                return pos
        return None

    ped_patrol_slot: dict[int, int] = {}
    if isinstance(_editor, dict):
        _slot = 0
        for _p in (_editor.get("paths") or []):
            pts = (_p or {}).get("points") or []
            if len(pts) < 2:
                continue
            pi = _p.get("ped_idx")
            if isinstance(pi, int) and pi not in ped_patrol_slot:
                ped_patrol_slot[pi] = _slot
            _slot += 1

    # ── Peds with spread offsets ──────────────────────────────────────────────
    peds = []
    for idx, p in enumerate(schema.peds):
        bx, by = _PED_OFFSETS[idx % len(_PED_OFFSETS)]
        patrol_slot = ped_patrol_slot.get(idx)
        epos = _editor_pos("peds", idx)
        peds.append({
            "model":       p.model or "UNRESOLVED",
            "role":        p.role,
            "count":       p.count,
            "weapon_name": _WEAPON_NAME.get(p.weapon),
            "ammo":        p.ammo,
            "health":      p.health,
            "armor":       p.armor,
            # A ped on a patrol path patrols the path instead of going hostile —
            # otherwise Task.Combat overrides the walk and it never patrols.
            # "guard" and "patrol" get their own passive stance branches below
            # (GuardCurrentPosition / WanderAround) so they react like normal
            # game AI instead of forced instant-combat at spawn; only "target"
            # is scripted hostile immediately.
            "is_hostile":  p.role == "target" and patrol_slot is None,
            "patrol_path_idx": patrol_slot,
            # Absolute placement from the editor (None → synthetic offset fallback).
            "abs_x":   float(epos[0]) if epos else None,
            "abs_y":   float(epos[1]) if epos else None,
            "abs_z":   float(epos[2]) if epos else None,
            "heading": float(epos[3]) if epos and len(epos) > 3 else None,
            "base_x":      bx,
            "base_y":      by,
        })

    # ── Vehicles with spread offsets ──────────────────────────────────────────
    vehicles = []
    for idx, v in enumerate(schema.vehicles):
        bx, by = _VEHICLE_OFFSETS[idx % len(_VEHICLE_OFFSETS)]
        epos = _editor_pos("vehicles", idx)
        vehicles.append({
            "model":             v.model or "UNRESOLVED",
            "role":              v.role,
            "count":             v.count,
            "abs_x":   float(epos[0]) if epos else None,
            "abs_y":   float(epos[1]) if epos else None,
            "abs_z":   float(epos[2]) if epos else None,
            "heading": float(epos[3]) if epos and len(epos) > 3 else None,
            "base_x":            bx,
            "base_y":            by,
            "is_race_opponent":  v.role == "race_opponent",
        })

    # ── Props with spread offsets ─────────────────────────────────────────────
    props = []
    pickup_idx = 0
    for o_idx, o in enumerate(schema.objects):
        bx, by = _PROP_OFFSETS[pickup_idx % len(_PROP_OFFSETS)]
        if o.role == "pickup":
            pickup_idx += 1
        epos = _editor_pos("objects", o_idx)
        props.append({
            "model":  o.model or "UNRESOLVED",
            "role":   o.role,
            "abs_x":  float(epos[0]) if epos else None,
            "abs_y":  float(epos[1]) if epos else None,
            "abs_z":  float(epos[2]) if epos else None,
            "heading": float(epos[3]) if epos and len(epos) > 3 else None,
            "base_x": bx,
            "base_y": by,
        })

    audio_pass = next((a.name for a in schema.audio if a.type == "mission_passed"), "FRANKLIN_BIG_01")
    audio_fail = next((a.name for a in schema.audio if a.type == "mission_failed"), "FRANKLIN_LOSE_1")

    # ── AI rival difficulty curve (HeliRace-style) ────────────────────────────
    ai_rival_present = any(v["is_race_opponent"] for v in vehicles)
    ai_difficulty_idx = _AI_DIFFICULTY_ORDER.index(schema.ai_difficulty) \
        if schema.ai_difficulty in _AI_DIFFICULTY_ORDER else 1
    ai_speed_curve = [
        {"idx": i, "spawn": _AI_SPAWN_SPEED[name], "chase": _AI_CHASE_SPEED[name]}
        for i, name in enumerate(_AI_DIFFICULTY_ORDER)
    ]

    # ── Optional F10 config menu (weather + AI difficulty, pre-mission only) ──
    config_menu = bool(schema.config_menu)
    weather_default_idx = _WEATHER_MENU_OPTIONS.index(weather_str) \
        if weather_str in _WEATHER_MENU_OPTIONS else 0
    menu_options: list[dict] = []
    if config_menu:
        menu_options.append({"key": "weather", "label": "Weather"})
        if ai_rival_present:
            menu_options.append({"key": "ai_difficulty", "label": "AI Difficulty"})
        menu_options.append({"key": "close", "label": "CLOSE MENU"})

    # ── Mid-mission events (HeliRace-style one-shot twists) ──────────────────
    mission_events: list[dict] = []
    for i, evt in enumerate(schema.events):
        mission_events.append({
            "index":        i,
            "event_type":   evt.event_type,
            "at_progress":  round(float(evt.at_progress), 2),
            "wind_speed":   float(evt.params.get("wind_speed", 40.0)),
            "wanted_level": max(1, min(5, int(evt.params.get("wanted_level", 3)))),
            "spawn_heli":   bool(evt.params.get("spawn_heli", False)),
        })
    events_change_weather = any(e["event_type"] == "thunderstorm" for e in mission_events)

    # ── Teleport points (named location + bounded offset → real Vector3) ─────
    # Same anchor-plus-offset convention as waypoints/theft_garage above — never
    # an LLM-invented absolute position.
    teleport_points: dict[str, dict] = {}
    for t in schema.teleport_points:
        tloc = _LOCATIONS.get(t.location_name, _LOCATIONS["docks"])
        teleport_points[t.name.strip().lower()] = {
            "name": t.name,
            "x": tloc["x"] + t.offset_x,
            "y": tloc["y"] + t.offset_y,
            "z": tloc["z"],
        }

    # ── Keybinds (F1-F12-gated actions, active any time the mission runs) ────
    # schema._validate_keybinds already dropped any dangling/duplicate-key
    # entry, so every `target` here is guaranteed to resolve.
    keybinds: list[dict] = []
    for i, kb in enumerate(schema.keybinds):
        target_norm = kb.target.strip().lower()
        entry = {
            "index":  i,
            "key":    kb.key,
            "action": kb.action,
            "label":  _escape_cs_string(kb.label or kb.target),
        }
        if kb.action == "teleport_player":
            entry.update(teleport_points[target_norm])
        elif kb.action == "spawn_ped_group":
            ped = next(p for p in schema.peds if p.description.strip().lower() == target_norm)
            entry.update({
                "model":       ped.model or "UNRESOLVED",
                "count":       ped.count,
                "weapon_name": _WEAPON_NAME.get(ped.weapon),
                "ammo":        ped.ammo,
                "health":      ped.health,
                "armor":       ped.armor,
                "hostile":     ped.role == "target",
                "ally":        ped.role == "ally",
                "radius":      _KEYBIND_SPAWN_RADIUS,
            })
        elif kb.action == "spawn_vehicle":
            veh = next(v for v in schema.vehicles if v.description.strip().lower() == target_norm)
            entry.update({
                "model":  veh.model or "UNRESOLVED",
                "count":  veh.count,
                "radius": _KEYBIND_SPAWN_RADIUS,
            })
        keybinds.append(entry)

    # ── Minigame skeleton (menu-primary arcade loop) ──────────────────────────
    mg = schema.minigame
    rp = dict(mg.round_params) if mg else {}

    # survive_waves round: enemy model + weapon ride the normal ped-resolution
    # path (Step 4) — first resolved ped wins; FAIL-SOFT to a known-good model.
    _enemy_ped = next((p for p in schema.peds
                       if p.model and p.model != "UNRESOLVED"), None)
    wave_spawner_cfg = {
        "name":          "WaveSpawner",
        "variant":       "radial_arena",
        "ped_model":     _enemy_ped.model if _enemy_ped else "g_m_y_lost_01",
        "vehicle_model": "police2",
        "prop_model":    "prop_barrel_02a",
        "count":         int(rp.get("enemies_per_wave", 3)),
        "radius":        float(rp.get("arena_radius", 35.0)),
        "hostile":       True,
    }
    wave_weapon_native = _WEAPON_NAME.get(_enemy_ped.weapon) if _enemy_ped else "weapon_pistol"
    waves_cfg = {
        "name":                 "WaveDirector",
        # Steep growth reads as exponential escalation; gentle as linear.
        "variant":              "exponential" if float(rp.get("wave_growth", 1.5)) >= 2.0 else "linear",
        "base_count":           int(rp.get("enemies_per_wave", 3)),
        "growth":               float(rp.get("wave_growth", 1.5)),
        "max_waves":            int(rp.get("wave_count", 5)),
        "intermission_seconds": int(rp.get("intermission_seconds", 8)),
        "pressure_seconds":     45,
    }

    minigame_rows: list[dict] = []
    if mg is not None:
        for s in mg.settings:
            if s.kind == "cycle":
                default_idx = s.options.index(s.default) if s.default in s.options else 0
                default_val = default_idx
            elif s.kind == "toggle":
                default_idx = 1 if s.default == "true" else 0
                default_val = default_idx
            else:  # int
                default_idx = 0
                default_val = int(s.default)
            minigame_rows.append({
                "key":         s.key,
                "label":       _escape_cs_string(s.label),
                "kind":        s.kind,
                "options":     [_escape_cs_string(o) for o in s.options],
                "min":         s.min_value,
                "max":         s.max_value,
                "step":        s.step,
                "default_idx": default_idx,
                "default_val": default_val,
                "default_raw": s.default,
            })

    # A settings row keyed "hero" is looked up by position at runtime (the
    # survive_waves round reads _cfgValues[hero_row_index] to pick the active
    # character ability) — -1 means no hero selector, abilities stay off.
    hero_row_index = next((i for i, r in enumerate(minigame_rows) if r["key"] == "hero"), -1)

    # ── Editor-authored shapes: triggers + patrol paths ──────────────────────
    # Pulled from meta.editor.* (Day-2 of the GTA5 Mission Editor). Triggers and
    # patrol paths are lowered into deterministic runtime data for the template.
    editor_triggers: list[dict] = []
    editor_paths:    list[dict] = []
    editor_path_sources: list[dict] = []
    editor_zones:    list[dict] = []
    editor = (schema.meta or {}).get("editor") if isinstance(schema.meta, dict) else None
    if isinstance(editor, dict):
        for i, t in enumerate(editor.get("triggers") or []):
            pos = (t or {}).get("pos") or []
            if len(pos) < 3:
                continue
            linked = t.get("linked_step")
            editor_triggers.append({
                "id":          str(t.get("id") or f"trg{i}"),
                "x":           float(pos[0]),
                "y":           float(pos[1]),
                "z":           float(pos[2]),
                "radius":      float(t.get("radius") or 10),
                "linked_step": int(linked) if isinstance(linked, int) else None,
            })
        for i, p in enumerate(editor.get("paths") or []):
            pts = p.get("points") or []
            if len(pts) < 2:
                continue
            mode = str(p.get("mode") or "loop").lower()
            if mode not in _PATROL_ROUTE_MODE_CS:
                mode = "loop"
            path_movement = _patrol_movement(p.get("movement"), "inherit")
            path_speed_default = _PATROL_MOVEMENT_SPEED.get(path_movement, 1.0)
            path_speed = _patrol_speed(p.get("speed"), path_speed_default)
            path_wait_ms = _patrol_wait_ms(p.get("wait_time"), 0.0)
            path_posture = _patrol_posture(p.get("posture"), "normal")
            path_weapon_state = _patrol_weapon_state(p.get("weapon_state"), "unchanged")
            path_look_type = _patrol_look_target_type(p.get("look_at_type"), "none")
            path_look_ref = p.get("look_at")
            path_look = _resolve_editor_look_target(path_look_type, path_look_ref)
            path_scan_heading = _patrol_angle(p.get("scan_heading"))
            path_scan_arc = _patrol_arc(p.get("scan_arc"))
            path_scan_duration_ms = _patrol_wait_ms(p.get("scan_duration"), 0.0)
            path_behavior = _patrol_behavior(p.get("behavior"), "walk")
            path_behavior = _patrol_behavior_with_movement(path_behavior, path_movement)
            path_behavior = _patrol_behavior_with_posture(path_behavior, path_posture)
            path_guard_until_alert = _bool_or_default(p.get("guard_until_alert"), False)
            wp_meta = p.get("waypoints") if isinstance(p.get("waypoints"), list) else []
            speeds = []
            waits_ms = []
            behaviors = []
            guard_until_alert = []
            postures = []
            weapon_states = []
            look_target_kinds = []
            look_target_indices = []
            look_target_coords = []
            scan_headings = []
            scan_arcs = []
            scan_durations_ms = []
            for j, _pt in enumerate(pts):
                wp = wp_meta[j] if j < len(wp_meta) and isinstance(wp_meta[j], dict) else {}
                movement = _patrol_movement(wp.get("movement"), path_movement)
                speed_default = _PATROL_MOVEMENT_SPEED.get(movement, path_speed)
                speeds.append(_patrol_speed(wp.get("speed"), speed_default))
                waits_ms.append(_patrol_wait_ms(wp.get("wait_time"), path_wait_ms / 1000.0))
                posture = _patrol_posture(wp.get("posture"), path_posture)
                weapon_state = _patrol_weapon_state(wp.get("weapon_state"), path_weapon_state)
                look_type = _patrol_look_target_type(wp.get("look_at_type"), path_look_type)
                look_ref = wp.get("look_at", path_look_ref)
                look_target = _resolve_editor_look_target(look_type, look_ref)
                scan_heading = _patrol_angle(wp.get("scan_heading"), path_scan_heading)
                scan_arc = _patrol_arc(wp.get("scan_arc"), path_scan_arc)
                scan_duration_ms = _patrol_wait_ms(
                    wp.get("scan_duration"), path_scan_duration_ms / 1000.0
                )
                behavior = _patrol_behavior(wp.get("behavior"), path_behavior)
                behavior = _patrol_behavior_with_movement(behavior, movement)
                behavior = _patrol_behavior_with_posture(behavior, posture)
                behaviors.append(_PATROL_BEHAVIOR_CS[behavior])
                guard_until_alert.append(
                    _bool_or_default(wp.get("guard_until_alert"), path_guard_until_alert)
                )
                postures.append(_PATROL_POSTURE_CS[posture])
                weapon_states.append(_PATROL_WEAPON_STATE_CS[weapon_state])
                look_target_kinds.append(look_target["kind_cs"])
                look_target_indices.append(look_target["index"])
                look_target_coords.append(look_target["coord"])
                scan_headings.append(scan_heading)
                scan_arcs.append(scan_arc)
                scan_durations_ms.append(scan_duration_ms)
            path_context = {
                "id":      str(p.get("id") or f"path{i}"),
                "ped_idx": p.get("ped_idx"),
                "mode":    mode,
                "mode_cs": _PATROL_ROUTE_MODE_CS[mode],
                "speed":   path_speed,
                "wait_ms": path_wait_ms,
                "posture": path_posture,
                "posture_cs": _PATROL_POSTURE_CS[path_posture],
                "weapon_state": path_weapon_state,
                "weapon_state_cs": _PATROL_WEAPON_STATE_CS[path_weapon_state],
                "look_target_kind": path_look["kind"],
                "look_target_kind_cs": path_look["kind_cs"],
                "look_target_index": path_look["index"],
                "look_target_coord": path_look["coord"],
                "scan_heading": path_scan_heading,
                "scan_arc": path_scan_arc,
                "scan_duration_ms": path_scan_duration_ms,
                "behavior": path_behavior,
                "behavior_cs": _PATROL_BEHAVIOR_CS[path_behavior],
                "guard_until_alert": path_guard_until_alert,
                "speeds":  speeds,
                "waits_ms": waits_ms,
                "behaviors": behaviors,
                "guard_until_alerts": guard_until_alert,
                "postures": postures,
                "weapon_states": weapon_states,
                "look_target_kinds": look_target_kinds,
                "look_target_indices": look_target_indices,
                "look_target_coords": look_target_coords,
                "scan_headings": scan_headings,
                "scan_arcs": scan_arcs,
                "scan_durations_ms": scan_durations_ms,
                "points":  [{"x": float(q[0]), "y": float(q[1]),
                             "z": float(q[2]) if len(q) > 2 else 0.0} for q in pts],
            }
            editor_paths.append(path_context)
            editor_path_sources.append(p)

        trigger_id_map = _unique_editor_id_map(editor_triggers)
        objective_id_map: dict[str, int] = {}
        duplicate_objective_ids: set[str] = set()
        for step_idx, step in enumerate(schema.steps):
            editor_id = str(step.params.get("editor_id") or "").strip()
            if not editor_id:
                continue
            if editor_id in objective_id_map:
                duplicate_objective_ids.add(editor_id)
            else:
                objective_id_map[editor_id] = step_idx
        for editor_id in duplicate_objective_ids:
            objective_id_map.pop(editor_id, None)

        for path_idx, (path_context, path_source) in enumerate(
            zip(editor_paths, editor_path_sources)
        ):
            raw_route_logic = path_source.get("route_logic")
            if raw_route_logic is not None:
                path_context["route_logic"] = _normalize_editor_route_logic(
                    raw_route_logic,
                    path_idx,
                    path_context,
                    editor_paths,
                    trigger_id_map,
                    objective_id_map,
                    len(schema.steps),
                )
        for i, z in enumerate(editor.get("zones") or []):
            pts = (z or {}).get("points") or []
            if len(pts) < 3:                       # need a real polygon
                continue
            kind = str(z.get("kind") or "mission").lower()
            if kind not in ("mission", "nogo"):
                kind = "mission"
            editor_zones.append({
                "id":    str(z.get("id") or f"zone{i}"),
                "name":  _escape_cs_string(str(z.get("name") or kind)),
                "kind":  kind,
                # 2D footprint (x,y) — zones are ground polygons.
                "points": [{"x": float(q[0]), "y": float(q[1])} for q in pts],
            })

    return {
        "class_name":       class_name,
        "mission_name":     _escape_cs_string(schema.mission_name),
        "weather":          weather_str,
        "time_of_day":      schema.time_of_day,
        "location":         location,
        "waypoints":        waypoints,
        "steps":            processed_steps,
        "peds":             peds,
        "vehicles":         vehicles,
        "props":            props,
        "audio_pass":       audio_pass,
        "audio_fail":       audio_fail,
        "race_checkpoints": race_checkpoints,   # [] when no racing steps
        # Editor-authored shapes (Mission Editor Day 2). Empty unless the schema
        # came from the editor or someone hand-populated meta.editor.*.
        "editor_triggers":  editor_triggers,
        "editor_paths":     editor_paths,
        "editor_route_logic": any(path.get("route_logic") for path in editor_paths),
        "editor_zones":     editor_zones,
        # AI rival difficulty curve + optional F10 config menu (HeliRace-style)
        "ai_rival_present":    ai_rival_present,
        "ai_difficulty_idx":   ai_difficulty_idx,
        "ai_speed_curve":      ai_speed_curve,
        "config_menu":         config_menu,
        "weather_options":     _WEATHER_MENU_OPTIONS,
        "weather_default_idx": weather_default_idx,
        "menu_options":        menu_options,
        # HeliRace-style events / HUD / reward / scavenger (always present —
        # StrictUndefined — empty/falsy when the feature is off)
        "mission_events":        mission_events,
        "events_change_weather": events_change_weather,
        "event_heli_model":      _EVENT_HELI_MODEL,
        "event_pilot_model":     _EVENT_PILOT_MODEL,
        "hud":                   bool(schema.hud),
        "reward_cash":           int(schema.reward_cash),
        "total_steps":           max(1, len(schema.steps)),
        "scavenger_sets":        scavenger_sets,
        # Keybind-gated actions (teleport / on-demand spawn) — empty unless
        # the schema declares them.
        "keybinds":              keybinds,
        # Minigame skeleton (always present — StrictUndefined — empty for missions)
        "skeleton":       schema.skeleton,
        "minigame_rows":  minigame_rows,
        "hero_row_index": hero_row_index,
        # theft_streak's personal garage — a fixed point near the mission
        # location's real spawn coordinates (same anchor peds/vehicles/props
        # already offset from), not an invented/unvalidated position.
        "theft_garage": {"x": location["x"] + 25.0, "y": location["y"] + 25.0, "z": location["z"]},
        "score_metric":   mg.score_metric if mg else "points",
        "score_label":    _escape_cs_string(mg.score_label) if mg else "SCORE",
        "round_type":     mg.round_type if mg else "timed",
        "round_params":   rp,
        "ini_file":       f"scripts\\\\{class_name}.ini",
        "wave_spawner_cfg":   wave_spawner_cfg,
        "wave_weapon_native": wave_weapon_native,
        "waves_cfg":          waves_cfg,
        # LLM-authored mission-specific C# (insertion points)
        "extra_usings":     schema.extra_usings,
        "extra_fields":     schema.extra_fields,
        "extra_init":       schema.extra_init,
        "extra_tick":       schema.extra_tick,
        "extra_methods":    schema.extra_methods,
    }


def _render_minigame_ini(schema: MissionSchema, ctx: dict) -> str:
    """Default .ini for a minigame: [Minigame] marker (scanned by the hub),
    [Settings] defaults (one line per config row), [Scores] best-score slot."""
    lines = [
        "[Minigame]",
        f"Name={schema.mission_name}",
        f"Metric={ctx['score_metric']}",
        f"Label={schema.minigame.score_label if schema.minigame else 'SCORE'}",
        "",
        "[Settings]",
    ]
    for row in ctx["minigame_rows"]:
        lines.append(f"{row['key']}={row['default_raw']}")
    lines += ["", "[Scores]", "Best=0", ""]
    return "\n".join(lines)


class CodeGenerator:
    def __init__(self, template_file: str = "templates/mission.cs.j2",
                 minigame_template_file: str = "templates/minigame.cs.j2"):
        template_path = Path(template_file)
        self._env = Environment(
            loader    = FileSystemLoader(str(template_path.parent)),
            undefined = StrictUndefined,
            trim_blocks   = True,
            lstrip_blocks = True,
        )
        self.template = self._env.get_template(template_path.name)
        # Minigame templates load lazily so mission-only setups (custom template
        # dirs, old configs) never require the minigame files to exist.
        self._minigame_template_name = Path(minigame_template_file).name
        self._minigame_template = None
        self._services_template = None

    def _get_minigame_template(self):
        if self._minigame_template is None:
            self._minigame_template = self._env.get_template(self._minigame_template_name)
        return self._minigame_template

    def _get_services_template(self):
        if self._services_template is None:
            self._services_template = self._env.get_template("minigame_services.cs.j2")
        return self._services_template

    def generate(self, schema: MissionSchema) -> str:
        """Compile MissionSchema → .NET 4.8 C# source."""
        ctx = _build_template_context(schema)
        if schema.skeleton == "minigame":
            return self._get_minigame_template().render(**ctx)
        return self.template.render(**ctx)

    def write(self, schema: MissionSchema, output_dir: str = "output") -> Path:
        """
        Write output/<ClassName>.3.cs  and  output/<ClassName>.schema.json.
        Recompile later without LLM:
            python main.py validate output/<ClassName>.schema.json
        """
        code = self.generate(schema)
        ctx  = _build_template_context(schema)
        out  = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        cs_path     = out / f"{ctx['class_name']}.3.cs"   # SHVDN3 requires .3.cs suffix
        schema_path = out / f"{ctx['class_name']}.schema.json"

        cs_path.write_text(code, encoding="utf-8")
        schema_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

        if schema.skeleton == "minigame":
            # Default .ini (settings + best score) — the mod loads/saves this at
            # runtime via ScriptSettings; the hub reads the [Minigame] marker.
            ini_path = out / f"{ctx['class_name']}.ini"
            ini_path.write_text(_render_minigame_ini(schema, ctx), encoding="utf-8")

            # Shared hub companion — deterministic content, idempotent overwrite.
            services_path = out / "MinigameServices.3.cs"
            services_path.write_text(
                self._get_services_template().render(), encoding="utf-8")

        return cs_path
