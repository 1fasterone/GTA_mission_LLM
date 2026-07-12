"""
Mission archetypes — the "sub-game" structure layer.

Themes (themes.py) shape *setting* — vocabulary, tone, atmosphere. Archetypes
shape *gameplay structure* — the skeleton of steps and entities that makes a
mission actually feel like a race, a heist, a dogfight, rather than the generic
"drive here, kill there, drive here, wait" the LLM defaults to.

An archetype is detected from the user's prompt. It contributes:
  - a concept hint  -> Step 2 (so the story is genre-coherent)
  - a structure hint -> Step 3 (so the LLM fills a genre-shaped steps array)
  - a deterministic scaffold the pipeline injects when the LLM *still* produces
    a degenerate steps array missing the genre-defining step (anti-repetition).

The pipeline (resolver, consistency check, compiler) is otherwise untouched —
archetypes only shape the steps/entities, which then flow through the existing
machinery (e.g. _check_consistency already adds race_opponent vehicles and lap
defaults for race_lap steps).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .mission_schema import MissionSchema, MissionStep, MissionVehicle, MissionObject


# ── Track selection ───────────────────────────────────────────────────────────
# Map a mission location to a real circuit from lookups/tracks.json so an
# injected race_lap lands on actual checkpoint geometry, not an empty track.

_LOCATION_TRACKS: dict[str, str] = {
    "airport":      "airport.circuit",
    "downtown":     "highway.circuit",
    "vinewood":     "vinewood.circuit",
    "highway":      "highway.circuit",
    "docks":        "docks.circuit",
    "port":         "docks.circuit",
    "sandy_shores": "sandy_shores.circuit",
    "alamo_sea":    "sandy_shores.circuit",
    "paleto_bay":   "sandy_shores.circuit",
}
_DEFAULT_TRACK = "airport.circuit"


def track_for_location(location: str) -> str:
    return _LOCATION_TRACKS.get(location, _DEFAULT_TRACK)


# Course keywords in the user's prompt/concept → a real track key. Checked before
# falling back to the location default, so "figure-8 on the runway" lands on
# airport.figure_8 instead of a generic circuit (preserving the user's intent).
_TRACK_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("figure 8", "figure-8", "figure eight", "figure-eight"), "airport.figure_8"),
    (("runway", "airstrip", "tarmac", "airport", "lsia"),       "airport.circuit"),
    (("freeway", "highway", "interstate", "olympic"),           "highway.circuit"),
    (("vinewood", "hills", "winding", "canyon"),                "vinewood.circuit"),
    (("desert", "sandy", "dirt", "off-road", "offroad", "rally"), "sandy_shores.circuit"),
    (("docks", "container", "port", "harbor", "harbour", "yard"), "docks.circuit"),
]


def track_from_text(text: str, location: str, *, valid_tracks: Optional[set[str]] = None) -> str:
    """Infer a concrete race track from free text (prompt/concept), preserving the
    user's stated course. Falls back to the location's default circuit. If
    `valid_tracks` is given, only return a key that exists in it."""
    t = (text or "").lower()
    for needles, track in _TRACK_KEYWORDS:
        if any(n in t for n in needles):
            if valid_tracks is None or track in valid_tracks:
                return track
    return track_for_location(location)


# ── Schema ────────────────────────────────────────────────────────────────────

class Archetype(BaseModel):
    name:            str
    display:         str
    keywords:        list[str]           = Field(default_factory=list)
    concept_hint:    str                  = ""
    structure_hint:  str                  = ""
    # If NONE of these step types appears in the LLM's output, the mission is
    # degenerate for this genre and the scaffold is injected.
    signature_steps: list[str]           = Field(default_factory=list)


# ── Archetype catalogue ───────────────────────────────────────────────────────

ARCHETYPES: list[Archetype] = [
    Archetype(
        name="race", display="Race",
        keywords=["race", "racing", "lap", "circuit", "track", "drift", "rally",
                  "grand prix", "time trial", "checkpoint race", "street race", "sprint"],
        signature_steps=["race_lap", "checkpoint"],
        concept_hint=(
            "This is a RACE. Frame the story around a defined course with a start "
            "line, a finish line, laps, and rival drivers — not a shootout."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: RACE — build an actual race, not a combat mission.\n"
            "The steps array MUST describe a course:\n"
            "  1. get_in_vehicle — player boards their car at the START LINE\n"
            "  2. race_lap — the core objective. params MUST set:\n"
            "       \"track\"  : one of airport.figure_8 | airport.circuit | vinewood.circuit |\n"
            "                   highway.circuit | docks.circuit | sandy_shores.circuit\n"
            "       \"lap_count\": an integer 2-5\n"
            "       \"require_vehicle\": true\n"
            "  (optional) timer — a target time to beat\n"
            "REQUIREMENTS:\n"
            "  - Add 2-3 vehicles with role \"race_opponent\" (the rivals) besides the player's car.\n"
            "  - Do NOT add kill_all or unrelated combat — finishing the laps IS the mission.\n"
            "  - Add a \"mission_passed\" audio cue for crossing the finish line.\n"
            "  - Set \"ai_difficulty\" (easy|medium|hard) to match how tough the concept says the\n"
            "    rivals are — e.g. \"a punishing ace pilot\" or \"the toughest street racer in the "
            "city\" is hard.\n"
            "  - Set \"config_menu\": true only if the concept explicitly wants the player to be\n"
            "    able to open a pre-race settings menu (adjust weather / rival difficulty)."
        ),
    ),
    Archetype(
        name="heist", display="Heist",
        keywords=["heist", "rob", "robbery", "steal", "vault", "loot", "score",
                  "burglary", "break in", "break-in", "grab the", "stash"],
        signature_steps=["pickup"],
        concept_hint=(
            "This is a HEIST. Frame the story as approach -> break in -> grab the "
            "score -> get out, with guards and a getaway."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: HEIST — build an approach/grab/escape sub-game.\n"
            "The steps array SHOULD follow this spine:\n"
            "  1. go_to — approach / case the target\n"
            "  2. kill_all — neutralise the guards (optional if stealthy)\n"
            "  3. pickup — grab the score (a loot/cash/valuables prop)\n"
            "  4. get_in_vehicle — reach the getaway vehicle\n"
            "  5. escape — lose the cops (wanted level to zero)\n"
            "REQUIREMENTS:\n"
            "  - Add at least one object with role \"pickup\" (the loot).\n"
            "  - Add guard peds and one getaway vehicle (role \"escape\")."
        ),
    ),
    Archetype(
        name="dive", display="Ocean / Dive",
        keywords=["ocean", "dive", "diving", "underwater", "submarine", "sub",
                  "scuba", "shipwreck", "wreck", "sea", "deep sea", "salvage", "reef"],
        signature_steps=["pickup", "deliver"],
        concept_hint=(
            "This is an OCEAN / DIVE mission. Frame it around reaching a dive site "
            "by boat or sub, recovering something underwater, and bringing it back."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: OCEAN / DIVE — build a recover-and-return sub-game.\n"
            "The steps array SHOULD follow this spine:\n"
            "  1. get_in_vehicle — board the boat or submarine\n"
            "  2. go_to — travel to the dive site (use a water-adjacent location: docks/port/alamo_sea)\n"
            "  3. pickup — recover the underwater item (a salvage/treasure prop)\n"
            "  4. deliver — bring it back to shore (a go_to precedes deliver to set the blip)\n"
            "REQUIREMENTS:\n"
            "  - Use a water location: docks | port | alamo_sea.\n"
            "  - Add a boat/submarine vehicle and a pickup object."
        ),
    ),
    Archetype(
        name="fly", display="Aerial / Flight",
        keywords=["fly", "flying", "flight", "plane", "jet", "aircraft", "helicopter",
                  "chopper", "dogfight", "aerial", "pilot", "airshow", "skydive", "stunt plane"],
        signature_steps=["checkpoint", "race_lap"],
        concept_hint=(
            "This is an AERIAL mission. Frame it around boarding an aircraft and "
            "flying a course of checkpoints through the sky."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: AERIAL / FLIGHT — build a flying course, not a ground fight.\n"
            "The steps array SHOULD follow this spine:\n"
            "  1. get_in_vehicle — board the aircraft (plane or helicopter)\n"
            "  2. checkpoint — fly through a course. params: {\"track\": \"airport.circuit\", \"require_vehicle\": true}\n"
            "  (optional) timer — a time limit\n"
            "  3. go_to — land at the airstrip to finish\n"
            "REQUIREMENTS:\n"
            "  - Use the airport location.\n"
            "  - Add an aircraft vehicle (describe it as a plane or helicopter).\n"
            "  - Do NOT add kill_all — flying the course is the objective."
        ),
    ),
    Archetype(
        name="assault", display="Assault / Raid",
        keywords=["assault", "raid", "shootout", "ambush", "gun", "gunfight",
                  "eliminate", "wipe out", "take out", "storm", "siege", "war", "attack"],
        signature_steps=["kill_all"],
        concept_hint=(
            "This is an ASSAULT. Frame it as approaching a defended position, "
            "eliminating the hostiles, and getting clear."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: ASSAULT / RAID.\n"
            "The steps array SHOULD follow this spine:\n"
            "  1. go_to — approach the target area\n"
            "  2. kill_all — eliminate the hostiles (the objective)\n"
            "  3. escape — get clear once the area is cleared\n"
            "REQUIREMENTS:\n"
            "  - Add several armed guard/patrol peds with weapons.\n"
            "  - Keep it focused — don't pad with unrelated pickups or waits."
        ),
    ),
    Archetype(
        name="chase", display="Chase / Pursuit",
        keywords=["chase", "pursuit", "pursue", "tail", "follow", "getaway",
                  "escape the", "outrun", "catch the", "hunt down", "intercept"],
        signature_steps=["timer", "get_in_vehicle"],
        concept_hint=(
            "This is a CHASE. Frame it around a vehicle pursuit — keeping up with "
            "or fleeing from a target under time pressure."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: CHASE / PURSUIT.\n"
            "The steps array SHOULD follow this spine:\n"
            "  1. get_in_vehicle — get into the chase car\n"
            "  2. go_to — close in on the target's location\n"
            "  3. timer — stay on the target / reach the goal before time runs out (params: {\"seconds\": 60-180})\n"
            "REQUIREMENTS:\n"
            "  - Add a target vehicle with role \"chase\".\n"
            "  - Do NOT resolve the chase with a static kill_all — keep it kinetic."
        ),
    ),
]

_BY_NAME = {a.name: a for a in ARCHETYPES}


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_archetype(prompt: str, concept: str = "") -> Optional[Archetype]:
    """
    Pick the archetype whose keywords have the strongest signal. The user prompt
    is weighted more heavily than the (longer, noisier) concept text. Returns
    None when nothing scores — the pipeline then behaves exactly as before.
    """
    p = (prompt or "").lower()
    c = (concept or "").lower()
    best: Optional[Archetype] = None
    best_score = 0
    for arch in ARCHETYPES:
        score = 0
        for kw in arch.keywords:
            if kw in p:
                score += 3              # prompt hit is the strong signal
            elif kw in c:
                score += 1              # concept hit is a soft signal
        if score > best_score:
            best, best_score = arch, score
    return best


def get_archetype(name: str) -> Optional[Archetype]:
    return _BY_NAME.get(name)


# ── Prompt rendering ──────────────────────────────────────────────────────────

def render_concept_hint(arch: Optional[Archetype]) -> str:
    return arch.concept_hint if arch else ""


def render_structure_hint(arch: Optional[Archetype]) -> str:
    return arch.structure_hint if arch else ""


# ── Anti-repetition scaffold (deterministic guard) ────────────────────────────

def is_degenerate(schema: MissionSchema, arch: Archetype) -> bool:
    """True when the LLM's steps contain NONE of the genre-defining step types."""
    present = {s.type for s in schema.steps}
    return not (present & set(arch.signature_steps))


def apply_scaffold(schema: MissionSchema, arch: Archetype) -> list[str]:
    """
    If the generated steps are degenerate for this archetype (missing every
    signature step), replace them with the archetype's canonical skeleton so the
    output is at least a real <genre> sub-game rather than generic filler. Also
    injects the minimum entities each scaffold needs. Returns log messages.

    Non-degenerate output is trusted and left alone (the structure hint already
    nudged the LLM, and _check_consistency fills in race_opponent/lap defaults).
    """
    if not is_degenerate(schema, arch):
        return []

    msgs = [f"Archetype '{arch.display}': steps lacked a {'/'.join(arch.signature_steps)} "
            f"step — injecting a {arch.display} scaffold"]
    builder = _SCAFFOLDS.get(arch.name)
    if builder is None:
        return []
    builder(schema, msgs)
    return msgs


def _ensure_vehicle(schema: MissionSchema, *, description: str, role: str,
                    model: str, count: int = 1) -> None:
    if any(v.role == role for v in schema.vehicles):
        return
    schema.vehicles.append(MissionVehicle(
        description=description, model=model, role=role, count=count))


def _ensure_pickup_object(schema: MissionSchema, *, description: str, model: str) -> None:
    if any(o.role == "pickup" for o in schema.objects):
        return
    schema.objects.append(MissionObject(description=description, model=model, role="pickup"))


def _scaffold_race(schema: MissionSchema, msgs: list[str]) -> None:
    # Preserve the user's stated course (e.g. "figure-8 on the runway") rather
    # than blindly picking the location default.
    intent_text = " ".join(filter(None, [
        str(schema.meta.get("user_prompt", "")), str(schema.meta.get("concept", "")),
    ]))
    track = track_from_text(intent_text, schema.location_name)
    schema.steps = [
        MissionStep(type="get_in_vehicle", label="Get to the start line"),
        MissionStep(type="race_lap", label="Complete the laps",
                    params={"track": track, "lap_count": 3, "require_vehicle": True}),
    ]
    # Rival racers — _check_consistency also handles this, but seed two so the
    # finished mission has a field to race against.
    if sum(1 for v in schema.vehicles if v.role == "race_opponent") < 2:
        for i in range(2):
            schema.vehicles.append(MissionVehicle(
                description="rival sports car", model="sultan",
                role="race_opponent", count=1))
        # Respect the schema's 4-vehicle cap.
        schema.vehicles = schema.vehicles[:4]
    msgs.append(f"  race scaffold: get_in_vehicle -> race_lap on {track} (3 laps) + rival racers")


def _scaffold_heist(schema: MissionSchema, msgs: list[str]) -> None:
    schema.steps = [
        MissionStep(type="go_to",          label="Approach the target"),
        MissionStep(type="kill_all",       label="Neutralize the guards"),
        MissionStep(type="pickup",         label="Grab the score"),
        MissionStep(type="get_in_vehicle", label="Reach the getaway vehicle"),
        MissionStep(type="escape",         label="Lose the cops"),
    ]
    _ensure_pickup_object(schema, description="bag of cash", model="prop_cash_pile_01")
    _ensure_vehicle(schema, description="getaway car", role="escape", model="sultan")
    msgs.append("  heist scaffold: go_to -> kill_all -> pickup -> get_in_vehicle -> escape")


def _scaffold_dive(schema: MissionSchema, msgs: list[str]) -> None:
    if schema.location_name not in ("docks", "port", "alamo_sea"):
        schema.location_name = "docks"
        msgs.append("  dive scaffold: moved to a water location (docks)")
    schema.steps = [
        MissionStep(type="get_in_vehicle", label="Board the boat"),
        MissionStep(type="go_to",          label="Travel to the dive site"),
        MissionStep(type="pickup",         label="Recover the item"),
        MissionStep(type="go_to",          label="Head back to shore"),
        MissionStep(type="deliver",        label="Deliver the recovered item"),
    ]
    _ensure_pickup_object(schema, description="salvage package", model="prop_drug_package_01")
    _ensure_vehicle(schema, description="speedboat", role="parked", model="speeder")
    msgs.append("  dive scaffold: board -> travel -> recover -> return -> deliver")


def _scaffold_fly(schema: MissionSchema, msgs: list[str]) -> None:
    schema.location_name = "airport"
    schema.steps = [
        MissionStep(type="get_in_vehicle", label="Board the aircraft"),
        MissionStep(type="checkpoint", label="Fly the course",
                    params={"track": "airport.circuit", "require_vehicle": True}),
        MissionStep(type="go_to", label="Land at the airstrip"),
    ]
    _ensure_vehicle(schema, description="stunt plane", role="race_opponent", model="duster")
    msgs.append("  fly scaffold: board aircraft -> checkpoint course -> land")


def _scaffold_assault(schema: MissionSchema, msgs: list[str]) -> None:
    schema.steps = [
        MissionStep(type="go_to",    label="Approach the target area"),
        MissionStep(type="kill_all", label="Eliminate the hostiles"),
        MissionStep(type="escape",   label="Get clear"),
    ]
    msgs.append("  assault scaffold: go_to -> kill_all -> escape")


def _scaffold_chase(schema: MissionSchema, msgs: list[str]) -> None:
    schema.steps = [
        MissionStep(type="get_in_vehicle", label="Get in the car"),
        MissionStep(type="go_to",          label="Close in on the target"),
        MissionStep(type="timer", label="Stay on the target", params={"seconds": 90}),
    ]
    _ensure_vehicle(schema, description="target car", role="chase", model="sultan")
    msgs.append("  chase scaffold: get_in_vehicle -> go_to -> timer")


_SCAFFOLDS = {
    "race":    _scaffold_race,
    "heist":   _scaffold_heist,
    "dive":    _scaffold_dive,
    "fly":     _scaffold_fly,
    "assault": _scaffold_assault,
    "chase":   _scaffold_chase,
}
