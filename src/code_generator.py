"""
Step 6 — .NET 4.8 C# code generator.
LLM never touches this. Jinja2 template + pre-processed schema data only.
"""

import json
import re
from pathlib import Path

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


def _build_template_context(schema: MissionSchema) -> dict:
    location    = _LOCATIONS.get(schema.location_name, _LOCATIONS["docks"])
    class_name  = _to_class_name(schema.mission_name)
    weather_str = _WEATHER_NATIVE.get(schema.weather.upper(), "CLEAR")

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
    for i, step in enumerate(schema.steps):
        if step.type == "go_to":
            dx, dy = _WP_DIRS[wp_counter % len(_WP_DIRS)]
            waypoints.append({
                "x":     location["x"] + dx,
                "y":     location["y"] + dy,
                "z":     location["z"],
                "label": step.label,
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
                                    "z": float(q.get("z", location["z"]))})
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
            "race_checkpoint_idx": race_checkpoint_idx.get(i),          # None for non-racing steps
            "timer_seconds":       int(step.params.get("seconds", 120)) if step.type == "timer" else None,
        })

    # ── Patrol-path → ped binding ─────────────────────────────────────────────
    # editor.paths[i].ped_idx points at a schema ped; build ped_idx → path-slot so
    # SpawnPeds can hand that ped its waypoint loop. Computed here (pre-peds) so the
    # ped dict can carry a `patrol_path_idx`.
    _editor = (schema.meta or {}).get("editor") if isinstance(schema.meta, dict) else None

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
            # A ped on a patrol path patrols the path instead of going hostile —
            # otherwise Task.Combat overrides the walk and it never patrols.
            "is_hostile":  p.role in ("guard", "patrol", "target") and patrol_slot is None,
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

    # ── Editor-authored shapes: triggers + patrol paths ──────────────────────
    # Pulled from meta.editor.* (Day-2 of the GTA5 Mission Editor). Triggers wire
    # into the OnTick loop; paths are emitted as Vector3 arrays for future use.
    editor_triggers: list[dict] = []
    editor_paths:    list[dict] = []
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
            editor_paths.append({
                "id":      str(p.get("id") or f"path{i}"),
                "ped_idx": p.get("ped_idx"),
                "points":  [{"x": float(q[0]), "y": float(q[1]),
                             "z": float(q[2]) if len(q) > 2 else 0.0} for q in pts],
            })
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
        "editor_zones":     editor_zones,
        # LLM-authored mission-specific C# (insertion points)
        "extra_usings":     schema.extra_usings,
        "extra_fields":     schema.extra_fields,
        "extra_init":       schema.extra_init,
        "extra_tick":       schema.extra_tick,
        "extra_methods":    schema.extra_methods,
    }


class CodeGenerator:
    def __init__(self, template_file: str = "templates/mission.cs.j2"):
        template_path = Path(template_file)
        env = Environment(
            loader    = FileSystemLoader(str(template_path.parent)),
            undefined = StrictUndefined,
            trim_blocks   = True,
            lstrip_blocks = True,
        )
        self.template = env.get_template(template_path.name)

    def generate(self, schema: MissionSchema) -> str:
        """Compile MissionSchema → .NET 4.8 C# source."""
        ctx = _build_template_context(schema)
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

        return cs_path
