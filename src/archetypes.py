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

from .mission_schema import MissionSchema, MissionStep, MissionVehicle, MissionObject, VehicleRole


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
    # "" = classic mission skeleton. "minigame" = this archetype IS a replayable
    # arcade loop — apply_scaffold always runs its builder to guarantee the
    # skeleton flag + round config, whatever the LLM emitted.
    wants_skeleton:  str                  = ""


# ── Archetype catalogue ───────────────────────────────────────────────────────

ARCHETYPES: list[Archetype] = [
    Archetype(
        name="race", display="Race",
        keywords=["race", "racing", "lap", "circuit", "track", "rally",
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
        name="drift", display="Drift Score",
        keywords=["drift", "drifting", "power slide", "powerslide", "sideways",
                  "drift score", "drift challenge", "drift attack", "touge",
                  "canyon run", "tandem drift", "drift king", "drift combo"],
        signature_steps=[],
        wants_skeleton="minigame",
        concept_hint=(
            "This is a DRIFT SCORE MINIGAME — a replayable arcade round, not a "
            "story mission. Frame it as chaining drifts through a canyon/street "
            "course for points against the clock; no plot, no combat, just the "
            "loop of sliding, combo, repeat."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: DRIFT SCORE MINIGAME.\n"
            "Set \"skeleton\": \"minigame\" and fill the \"minigame\" block:\n"
            "  \"score_metric\": \"points\", \"score_label\": \"DRIFT SCORE\",\n"
            "  \"round_type\": \"drift_score\",\n"
            "  \"round_params\": {\"time_limit_s\": 30-300, \"min_drift_angle\": 5-60,\n"
            "                     \"min_drift_speed\": 3-25, \"combo_decay_s\": 0.3-5}\n"
            "  \"settings\": 0-4 config-menu rows the player can tune before a round\n"
            "    (e.g. a cycle row for time limit).\n"
            "REQUIREMENTS:\n"
            "  - Leave \"steps\" and \"peds\" as [] — the round loop replaces the step machine\n"
            "    and drift state comes from whatever vehicle the player is already driving.\n"
            "  - Use a canyon/street location (vinewood works well) for flavour only —\n"
            "    it has no effect on scoring."
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
        name="survival_waves", display="Survival Waves",
        keywords=["survival", "horde", "waves", "wave defense", "arena",
                  "last stand", "onslaught", "swarm", "endless enemies",
                  "hold out against"],
        signature_steps=[],                 # minigames don't use the step machine
        wants_skeleton="minigame",
        concept_hint=(
            "This is a SURVIVAL WAVES MINIGAME — a replayable arcade round, not a "
            "story mission. Frame it as an arena last-stand: escalating enemy "
            "waves, a kill score, short breathers between waves, and an instant "
            "'play again'. No plot, no fetch objectives — just the loop."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: SURVIVAL WAVES MINIGAME.\n"
            "Set \"skeleton\": \"minigame\" and fill the \"minigame\" block:\n"
            "  \"score_metric\": \"kills\", \"score_label\": \"KILLS\",\n"
            "  \"round_type\": \"survive_waves\",\n"
            "  \"round_params\": {\"wave_count\": 3-10, \"enemies_per_wave\": 2-8,\n"
            "                     \"wave_growth\": 1.0-3.0, \"arena_radius\": 20-100,\n"
            "                     \"intermission_seconds\": 3-30}\n"
            "  \"settings\": 2-4 config-menu rows the player can tune before a round\n"
            "    (e.g. an int row for wave count, a toggle for hardcore).\n"
            "REQUIREMENTS:\n"
            "  - Add 1-2 ped entries describing the enemy type (role \"target\", a real\n"
            "    weapon) — the compiler resolves them to models and spawns them per wave.\n"
            "  - Leave \"steps\" as [] — the minigame loop replaces the step machine.\n"
            "  - No go_to/deliver/story steps."
        ),
    ),
    Archetype(
        name="xmen", display="Mutant Uprising",
        keywords=["x-men", "xmen", "mutant", "mutants", "superhero showdown",
                  "super powers", "danger room", "brotherhood of mutants",
                  "mutant uprising", "magneto", "wolverine", "mystique"],
        signature_steps=[],
        wants_skeleton="minigame",
        concept_hint=(
            "This is a MUTANT UPRISING MINIGAME — a survive_waves arena round "
            "where the player picks a hero before each attempt from an in-game "
            "menu, and that choice changes what the E/Q keys do all round: "
            "Magneto (E push / Q pull nearby vehicles with magnetism), "
            "Wolverine (passive super jump + E claw swipe on nearby enemies), "
            "Mystique (E drop a warp mark / Q teleport back to it)."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: MUTANT UPRISING MINIGAME.\n"
            "Set \"skeleton\": \"minigame\" and fill the \"minigame\" block:\n"
            "  \"score_metric\": \"kills\", \"score_label\": \"KILLS\",\n"
            "  \"round_type\": \"survive_waves\",\n"
            "  \"round_params\": {\"wave_count\": 3-10, \"enemies_per_wave\": 2-8,\n"
            "                     \"wave_growth\": 1.0-3.0, \"arena_radius\": 20-100,\n"
            "                     \"intermission_seconds\": 3-30}\n"
            "  \"settings\" MUST include exactly this cycle row first (the compiler\n"
            "    indexes it by position to pick the live ability, so keep the exact\n"
            "    key/options — do not rename, reorder, or translate them):\n"
            "      {\"key\": \"hero\", \"label\": \"Hero\", \"kind\": \"cycle\",\n"
            "       \"options\": [\"Magneto\", \"Wolverine\", \"Mystique\"], \"default\": \"Magneto\"}\n"
            "  You may add up to 3 more settings rows after it (e.g. wave count, hardcore).\n"
            "REQUIREMENTS:\n"
            "  - Add 1-2 ped entries describing the enemy type (role \"target\", a real\n"
            "    weapon) — human soldiers/agents hunting mutants read best.\n"
            "  - Leave \"steps\" as [] — the minigame loop replaces the step machine."
        ),
    ),
    Archetype(
        name="gangster_thief", display="Theft Streak",
        keywords=["gangster thief", "grand theft spree", "car theft spree",
                  "boosting cars", "chop shop rush", "joyride streak",
                  "car boosting spree", "theft streak"],
        signature_steps=[],
        wants_skeleton="minigame",
        concept_hint=(
            "This is a THEFT STREAK MINIGAME — a replayable arcade round, not "
            "a story mission. The player jacks any vehicle they can find and "
            "drives it to their personal garage to secure it; score is cars "
            "secured. No narrative target, no vault, no plot — just steal, "
            "deliver, repeat against the clock."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: THEFT STREAK MINIGAME.\n"
            "Set \"skeleton\": \"minigame\" and fill the \"minigame\" block:\n"
            "  \"score_metric\": \"points\", \"score_label\": \"CARS SECURED\",\n"
            "  \"round_type\": \"theft_streak\",\n"
            "  \"round_params\": {\"time_limit_s\": 60-300, \"garage_radius\": 4-20}\n"
            "  \"settings\": 0-4 config-menu rows the player can tune before a round\n"
            "    (e.g. a cycle row for time limit).\n"
            "REQUIREMENTS:\n"
            "  - Leave \"steps\" and \"peds\"/\"vehicles\" as [] — the round loop replaces\n"
            "    the step machine, the garage is a fixed compiler-placed point, and the\n"
            "    stolen vehicle is always whatever the player is currently driving.\n"
            "  - Do NOT frame this as a heist (no vault/guards/getaway) — it's an\n"
            "    endless arcade loop, not a one-shot narrative job."
        ),
    ),
    Archetype(
        name="pursuit_escape", display="Pursuit Escape",
        keywords=["survive the heat", "most wanted", "cop chase survival",
                  "outlast the police", "evade the police", "five stars",
                  "wanted level survival", "outlast the cops", "survive the cops",
                  "police survival", "survive the police", "endless police pursuit",
                  "survive as long as possible"],
        signature_steps=[],
        wants_skeleton="minigame",
        concept_hint=(
            "This is a PURSUIT ESCAPE MINIGAME — a replayable arcade round, not a "
            "story mission. The player survives an escalating police pursuit as "
            "long as possible; the score is seconds survived. Heat keeps rising "
            "on a timer, eventually sending an attack helicopter."
        ),
        structure_hint=(
            "MISSION ARCHETYPE: PURSUIT ESCAPE MINIGAME.\n"
            "Set \"skeleton\": \"minigame\" and fill the \"minigame\" block:\n"
            "  \"score_metric\": \"seconds\", \"score_label\": \"SURVIVED\",\n"
            "  \"round_type\": \"pursuit_survive\",\n"
            "  \"round_params\": {\"start_wanted\": 1-3, \"escalation_interval_s\": 15-120,\n"
            "                     \"max_wanted\": 3-5, \"spawn_heli_at_level\": 0-5}\n"
            "  \"settings\": 2-4 config-menu rows (e.g. int row for starting heat,\n"
            "    cycle row for escalation speed).\n"
            "REQUIREMENTS:\n"
            "  - Leave \"steps\" as [] and \"peds\" as [] — the game itself supplies police.\n"
            "  - spawn_heli_at_level 0 means the helicopter never comes."
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

# Step types that are a real sub-game in their own right, whatever archetype the
# keyword classifier picked. A scavenger hunt with combat-flavoured wording must
# not have its any-order spine replaced by an assault/chase scaffold.
_SELF_SUFFICIENT_STEPS: frozenset[str] = frozenset({"scavenger"})


def is_degenerate(schema: MissionSchema, arch: Archetype) -> bool:
    """True when the LLM's steps contain NONE of the genre-defining step types."""
    present = {s.type for s in schema.steps}
    if present & _SELF_SUFFICIENT_STEPS:
        return False
    return not (present & set(arch.signature_steps))


def apply_scaffold(schema: MissionSchema, arch: Archetype) -> list[str]:
    """
    If the generated steps are degenerate for this archetype (missing every
    signature step), replace them with the archetype's canonical skeleton so the
    output is at least a real <genre> sub-game rather than generic filler. Also
    injects the minimum entities each scaffold needs. Returns log messages.

    Minigame archetypes (wants_skeleton == "minigame") ALWAYS run their builder:
    the builder is the deterministic guard that guarantees the skeleton flag and
    round config even when the LLM ignored the minigame block entirely. The
    builders are idempotent — LLM-authored settings/params survive.

    Non-degenerate mission output is trusted and left alone (the structure hint
    already nudged the LLM, and _check_consistency fills in defaults).
    """
    if arch.wants_skeleton == "minigame":
        builder = _SCAFFOLDS.get(arch.name)
        if builder is None:
            return []
        msgs = [f"Archetype '{arch.display}': enforcing the minigame skeleton"]
        builder(schema, msgs)
        return msgs

    if not is_degenerate(schema, arch):
        return []

    msgs = [f"Archetype '{arch.display}': steps lacked a {'/'.join(arch.signature_steps)} "
            f"step — injecting a {arch.display} scaffold"]
    builder = _SCAFFOLDS.get(arch.name)
    if builder is None:
        return []
    builder(schema, msgs)
    return msgs


def _ensure_vehicle(schema: MissionSchema, *, description: str, role: VehicleRole,
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


def _scaffold_survival_waves(schema: MissionSchema, msgs: list[str]) -> None:
    from .mission_schema import MinigameSetting, MinigameSpec, MissionPed
    schema.skeleton = "minigame"
    schema.steps = []                       # the round loop replaces the step machine
    if schema.minigame is None:
        schema.minigame = MinigameSpec()
    mg = schema.minigame
    mg.round_type = "survive_waves"
    # Re-validate params through the round model (idempotent — LLM values kept).
    from .mission_schema import WaveRoundParams
    mg.round_params = WaveRoundParams.model_validate(mg.round_params or {}).model_dump()
    if mg.score_metric == "points":
        mg.score_metric = "kills"
        mg.score_label = "KILLS"
    if not mg.settings:
        mg.settings = [
            MinigameSetting(key="wave_count", label="Waves", kind="int",
                            min_value=3, max_value=10,
                            default=str(mg.round_params["wave_count"])),
            MinigameSetting(key="hardcore", label="Hardcore Mode", kind="toggle",
                            default="false"),
        ]
    # Wave enemies ride the normal ped resolution path (Step 4 RAG).
    if not schema.peds:
        schema.peds.append(MissionPed(
            description="gang enforcer", role="target", count=1, weapon="pistol"))
        msgs.append("  survival scaffold: added a default wave enemy ped")
    msgs.append("  survival scaffold: skeleton=minigame, round_type=survive_waves")


# Canonical hero roster + order — the round loop indexes the "hero" settings
# row by position (0=Magneto/1=Wolverine/2=Mystique) to pick the live E/Q
# ability, so this exact list must never drift from the template's switch.
_XMEN_HERO_OPTIONS = ["Magneto", "Wolverine", "Mystique"]


def _scaffold_xmen(schema: MissionSchema, msgs: list[str]) -> None:
    from .mission_schema import MinigameSetting, MinigameSpec, MissionPed, WaveRoundParams
    schema.skeleton = "minigame"
    schema.steps = []
    if schema.minigame is None:
        schema.minigame = MinigameSpec()
    mg = schema.minigame
    mg.round_type = "survive_waves"
    mg.round_params = WaveRoundParams.model_validate(mg.round_params or {}).model_dump()
    if mg.score_metric == "points":
        mg.score_metric = "kills"
        mg.score_label = "KILLS"
    # The hero row is enforced (not just added-if-missing): its options are
    # read by position at runtime, so an LLM-authored row with the wrong
    # wording or order would silently scramble which ability each hero gets.
    hero_default = "Magneto"
    for s in mg.settings:
        if s.key == "hero" and s.default in _XMEN_HERO_OPTIONS:
            hero_default = s.default
            break
    other_settings = [s for s in mg.settings if s.key != "hero"][:3]
    mg.settings = [
        MinigameSetting(key="hero", label="Hero", kind="cycle",
                        options=list(_XMEN_HERO_OPTIONS), default=hero_default),
    ] + other_settings
    if not schema.peds:
        schema.peds.append(MissionPed(
            description="mutant-hunting soldier", role="target", count=1, weapon="rifle"))
        msgs.append("  xmen scaffold: added a default wave enemy ped")
    msgs.append("  xmen scaffold: skeleton=minigame, round_type=survive_waves, "
                "hero selector enforced (Magneto/Wolverine/Mystique)")


def _scaffold_pursuit_escape(schema: MissionSchema, msgs: list[str]) -> None:
    from .mission_schema import MinigameSetting, MinigameSpec, PursuitRoundParams
    schema.skeleton = "minigame"
    schema.steps = []
    if schema.minigame is None:
        schema.minigame = MinigameSpec()
    mg = schema.minigame
    mg.round_type = "pursuit_survive"
    mg.round_params = PursuitRoundParams.model_validate(mg.round_params or {}).model_dump()
    if mg.score_metric == "points":
        mg.score_metric = "seconds"
        mg.score_label = "SURVIVED"
    if not mg.settings:
        mg.settings = [
            MinigameSetting(key="start_wanted", label="Starting Heat", kind="int",
                            min_value=1, max_value=3,
                            default=str(mg.round_params["start_wanted"])),
            MinigameSetting(key="heli", label="Attack Helicopter", kind="toggle",
                            default="true" if mg.round_params["spawn_heli_at_level"] else "false"),
        ]
    msgs.append("  pursuit scaffold: skeleton=minigame, round_type=pursuit_survive")


def _scaffold_drift(schema: MissionSchema, msgs: list[str]) -> None:
    from .mission_schema import MinigameSetting, MinigameSpec, DriftRoundParams
    schema.skeleton = "minigame"
    schema.steps = []
    if schema.minigame is None:
        schema.minigame = MinigameSpec()
    mg = schema.minigame
    mg.round_type = "drift_score"
    mg.round_params = DriftRoundParams.model_validate(mg.round_params or {}).model_dump()
    if mg.score_label in ("", "SCORE"):
        mg.score_label = "DRIFT SCORE"
    if not mg.settings:
        mg.settings = [
            MinigameSetting(key="time_limit", label="Time Limit", kind="cycle",
                            options=["60", "90", "120"],
                            default=str(mg.round_params["time_limit_s"])
                                    if str(mg.round_params["time_limit_s"]) in ("60", "90", "120")
                                    else "90"),
        ]
    msgs.append("  drift scaffold: skeleton=minigame, round_type=drift_score")


def _scaffold_gangster_thief(schema: MissionSchema, msgs: list[str]) -> None:
    from .mission_schema import MinigameSetting, MinigameSpec, TheftRoundParams
    schema.skeleton = "minigame"
    schema.steps = []
    if schema.minigame is None:
        schema.minigame = MinigameSpec()
    mg = schema.minigame
    mg.round_type = "theft_streak"
    mg.round_params = TheftRoundParams.model_validate(mg.round_params or {}).model_dump()
    if mg.score_label in ("", "SCORE"):
        mg.score_label = "CARS SECURED"
    if not mg.settings:
        mg.settings = [
            MinigameSetting(key="time_limit", label="Time Limit", kind="cycle",
                            options=["60", "120", "180"],
                            default=str(mg.round_params["time_limit_s"])
                                    if str(mg.round_params["time_limit_s"]) in ("60", "120", "180")
                                    else "120"),
        ]
    msgs.append("  gangster thief scaffold: skeleton=minigame, round_type=theft_streak")


_SCAFFOLDS = {
    "race":    _scaffold_race,
    "heist":   _scaffold_heist,
    "dive":    _scaffold_dive,
    "fly":     _scaffold_fly,
    "assault": _scaffold_assault,
    "chase":   _scaffold_chase,
    "survival_waves": _scaffold_survival_waves,
    "pursuit_escape": _scaffold_pursuit_escape,
    "drift":          _scaffold_drift,
    "xmen":           _scaffold_xmen,
    "gangster_thief": _scaffold_gangster_thief,
}
