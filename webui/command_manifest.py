"""
Static manifest of every `python main.py <command>` CLI command, for the
Pipeline Builder UI. Mirrors main.py's Typer command definitions — positional
args, options (flag/short/type/default/choices/help), and a category
("gta" | "unity" | "shared") so the UI can toggle between backends.

This is a plain data file (no LLM, no dynamic introspection): when a command
is added or changed in main.py, mirror the change here. Kept separate from
main.py so the web UI doesn't need to import Typer's CLI internals.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class Opt(TypedDict, total=False):
    name: str            # form field key, e.g. "theme"
    flag: str             # long flag, e.g. "--theme"
    short: Optional[str]  # short flag, e.g. "-c"
    kind: str              # "string" | "int" | "float" | "bool" | "choice" | "path" | "multi"
    default: Any
    choices: list[str]
    help: str
    positional: bool


def _opt(name: str, flag: str = "", short: str = "", kind: str = "string",
         default: Any = None, choices: Optional[list[str]] = None,
         help: str = "", positional: bool = False) -> Opt:
    o: Opt = {"name": name, "flag": flag, "short": short, "kind": kind,
              "default": default, "help": help, "positional": positional}
    if choices:
        o["choices"] = choices
    return o


# Options shared by every "simple micro" command (unity-<kind> / gta-<kind>).
def _micro_opts(variant_choices: Optional[list[str]]) -> list[Opt]:
    return [
        _opt("prompt", kind="string", positional=True, help="Plain-English description"),
        _opt("file", "--file", "-f", "path", help="Read prompt from a text file instead"),
        _opt("variant", "--variant", "", "choice", choices=variant_choices or [],
             help="Pick a specific variant; omitted = auto-inferred from the prompt"),
        _opt("theme", "--theme", "", "string", help="Theme name, 'random', or 'list'"),
        _opt("mashup_with", "--mashup", "", "string", help="Second theme to fuse with --theme"),
        _opt("seed", "--seed", "", "int", help="RNG seed for reproducibility"),
        _opt("out", "--out", "", "string", help="Override output directory"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("model", "--model", "-m", "string", help="Override the configured LM Studio model for this run"),
    ]


_SIMPLE_MICRO_VARIANTS: dict[str, list[str]] = {
    "controller": "first_person|third_person|top_down|2d_platformer|2d_topdown|platformer_2_5d|"
                  "vehicle_car|vehicle_bike|flying|swimming|aircraft|fighting".split("|"),
    "camera": "follow|follow_2d|follow_2_5d|orbit|first_person|cinemachine_3p|cinemachine_topdown|"
              "rts_pan_zoom|vehicle_chase|cinematic_dolly".split("|"),
    "physics": "rigidbody_2d|rigidbody_3d|kinematic_character|ragdoll|vehicle_wheels|soft_body_stub|"
               "trigger_zone|destructible_object|buoyancy".split("|"),
    "hud": "health_bar|ammo_counter|minimap|crosshair|interaction_prompt|score_panel|"
           "pause_menu|dialogue_box|full_hud_bundle".split("|"),
    "input": "keyboard_mouse|gamepad|touch|dual_stick|racing_wheel|vr_xr".split("|"),
    "ai": "navmesh_patrol|chase_player|fsm_guard|behaviour_tree_lite|flocking|cover_shooter|"
          "flying_swarm|waypoint_follow".split("|"),
    "spawner": "wave_spawner|continuous_spawner|object_pool|trigger_spawner|point_spawner|radial_spawner".split("|"),
    "saveload": "json_file|playerprefs_kv|save_slots|checkpoint|autosave|encrypted_json".split("|"),
    "inventory": "slot_based|grid|weight_based|equipment|hotbar|unlimited".split("|"),
    "dialogue": "linear|branching|typewriter|cutscene|barks|localized".split("|"),
    "menu": "main_menu|pause_menu|settings_menu|game_over|level_select|confirm_dialog".split("|"),
    "interactable": "pickup|door|lever|button_switch|npc_talk|readable".split("|"),
    "transition": "instant|fade|loading_bar|async_additive|trigger_door".split("|"),
    "health": "simple|regenerating|shielded|armor|lives".split("|"),
    "objectives": ["tracker"],
    "behavior_tree": ["tree"],
    "statemachine": "player_fsm|enemy_fsm|game_fsm|object_fsm".split("|"),
    "trigger": "zone_enter|zone_exit|timer_trigger|distance_trigger|compound".split("|"),
    "animator": "humanoid|vehicle|creature|object|ui".split("|"),
    "shader": "color_cycle|dissolve|hit_flash|uv_scroll|outline_pulse".split("|"),
    "vfx": "particle_system|vfx_sequence|screen_fx|impact_fx".split("|"),
}

_GTA_MICRO_VARIANTS: dict[str, list[str]] = {
    "menu": ["settings_panel", "pause_menu", "leaderboard_view"],
    "hud": ["score_ticker", "wave_banner", "timer_bar"],
    "spawner": ["radial_arena", "vehicle_pursuit", "prop_scatter"],
    "waves": ["linear", "exponential", "timed_pressure"],
    "saveload": ["highscore_table", "settings_profile"],
}

_MICRO_HELP: dict[str, str] = {
    "controller": "Focused Unity character controller (drop-in script + optional input asset).",
    "camera": "Focused Unity camera rig (Cinemachine or plain MonoBehaviour).",
    "physics": "Focused Unity physics/collider body.",
    "hud": "Focused Unity Canvas / UI Toolkit HUD element.",
    "input": "Unity Input System action map + InputReader ScriptableObject.",
    "ai": "Unity AI behaviour (NavMesh patrol/chase/FSM/flocking).",
    "spawner": "Unity spawner (waves, object pool, trigger/point/radial).",
    "saveload": "Unity save/load system (JSON file, PlayerPrefs, slots, checkpoint, autosave).",
    "inventory": "Unity inventory system (slot/grid/weight/equipment/hotbar).",
    "dialogue": "Unity dialogue system (linear/branching/typewriter/cutscene/barks).",
    "menu": "Unity menu (main/pause/settings/game-over/level-select/confirm).",
    "interactable": "Unity interactable (pickup/door/lever/switch/npc/readable).",
    "transition": "Unity scene transition (instant/fade/loading bar/async/trigger door).",
    "health": "Unity health system (simple/regenerating/shielded/armor/lives).",
    "objectives": "Unity mission objective tracker (progress + completion events).",
    "behavior_tree": "Minimal Unity behavior-tree interpreter (Sequence/Selector).",
    "statemachine": "Unity FSM (player_fsm/enemy_fsm/game_fsm/object_fsm).",
    "trigger": "Unity event trigger zone (enter/exit/timer/distance/compound).",
    "animator": "Unity Animator parameter driver (humanoid/vehicle/creature/object/ui).",
    "shader": "Unity runtime shader/material controller via MaterialPropertyBlock.",
    "vfx": "Unity VFX controller (particle system/scripted sequence/screen fx/impact fx).",
}

_GTA_MICRO_HELP: dict[str, str] = {
    "menu": "GTA5 native-draw overlay menu (drop-in SHVDN3 .3.cs).",
    "hud": "GTA5 round HUD drawn with natives (drop-in SHVDN3 .3.cs).",
    "spawner": "GTA5 wave spawner (deterministic ring placement, drop-in .3.cs).",
    "waves": "GTA5 wave-escalation director (drop-in SHVDN3 .3.cs).",
    "saveload": "GTA5 ScriptSettings .ini persistence vault (drop-in .3.cs).",
}


class Command(TypedDict):
    name: str          # CLI command name, e.g. "unity-create"
    category: str      # "gta" | "unity" | "shared"
    group: str          # UI grouping label, e.g. "Unity — Project", "Unity — Micro-generators"
    help: str
    options: list[Opt]


def _cmd(name: str, category: str, group: str, help: str, options: list[Opt]) -> Command:
    return {"name": name, "category": category, "group": group, "help": help, "options": options}


COMMANDS: list[Command] = [
    # ── GTA5 ────────────────────────────────────────────────────────────────
    _cmd("generate", "gta", "GTA5 — Core",
         "Generate a GTA5 mission C# script from a plain-English prompt (full LLM pipeline).", [
        _opt("prompt", kind="string", positional=True, help="Mission idea in plain English"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("theme", "--theme", "", "string", help="Theme name, 'random', or 'list'"),
        _opt("mashup_with", "--mashup", "", "string", help="Second theme to fuse with --theme"),
        _opt("seed", "--seed", "", "int", help="RNG seed for reproducibility"),
        _opt("model", "--model", "-m", "string", help="Override the configured LM Studio model"),
    ]),
    _cmd("validate", "gta", "GTA5 — Core",
         "Recompile a saved GTA5 schema to C# without re-running the LLM.", [
        _opt("schema_file", kind="path", positional=True, help="Path to a mission JSON schema file"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("resolve", "gta", "GTA5 — Core",
         "Resolve one GTA5 asset description to a model ID (RAG + local fallback).", [
        _opt("description", kind="string", positional=True, help="e.g. 'cult member'"),
        _opt("category", kind="choice", positional=True,
             choices=["peds", "vehicles", "objects", "weapons"], help="Asset category"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("batch", "gta", "GTA5 — Core",
         "Generate multiple GTA5 missions from a prompts file (one prompt per line, # comments ignored).", [
        _opt("prompts_file", kind="path", positional=True, help="Text file, one GTA5 mission prompt per line"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("setup-rag", "gta", "GTA5 — Legacy / AnythingLLM",
         "Upload GTA5 RAG documents to the AnythingLLM workspace.", [
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),

    # ── Unity — project ─────────────────────────────────────────────────────
    _cmd("unity-create", "unity", "Unity — Project",
         "Generate a complete Unity project folder from a plain-English prompt.", [
        _opt("prompt", kind="string", positional=True, help="Project idea in plain English"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("file", "--file", "-f", "path", help="Read prompt from a .txt file instead"),
        _opt("theme", "--theme", "", "string", help="Theme name, 'random', or 'list'"),
        _opt("mashup_with", "--mashup", "", "string", help="Second theme to fuse with --theme"),
        _opt("seed", "--seed", "", "int", help="RNG seed for reproducibility"),
        _opt("model", "--model", "-m", "string", help="Override the configured LM Studio model"),
        _opt("compose", "--compose", "", "bool", default=False,
             help="Assemble micros into a wired project (GameWiring + GameContext)"),
        _opt("blender_models", "--blender-models", "", "bool", default=False,
             help="Build real Blender models from the story and wire them in, replacing placeholders"),
        _opt("route", "--route", "", "path", help="routes.json from unity-path, feeds P1 path authoring"),
    ]),
    _cmd("unity-path", "unity", "Unity — Project",
         "Author a P1-normalized route file (≤64 points) from text points or an image drawing — deterministic, LLM-free.", [
        _opt("points", "--points", "-p", "string", help='Point spec: "x,z; x,z; ..." (mutually exclusive w/ --file/--image)'),
        _opt("file", "--file", "-f", "path", help="Text file of points (one 'x,z' per line)"),
        _opt("image", "--image", "-i", "path", help="Top-down course drawing image"),
        _opt("width", "--width", "-w", "float", default=10.0, help="Ribbon width in meters"),
        _opt("world_size", "--world-size", "", "float", default=160.0, help="Image-only: meters the image maps to"),
        _opt("closed", "--closed/--open", "", "bool", help="Force closed/open loop (default: auto-detect)"),
        _opt("out", "--out", "-o", "string", default="routes.json", help="Output path for the route JSON"),
    ]),
    _cmd("unity-resolve", "unity", "Unity — Project",
         "Resolve one Unity package description to a Package Manager ID + version.", [
        _opt("description", kind="string", positional=True, help="e.g. 'universal render pipeline'"),
        _opt("unity_version", kind="choice", positional=True, default="6000.0",
             choices=["6000.0", "6000.1", "2022.3", "2021.3"]),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("unity-validate", "unity", "Unity — Project",
         "Recompile a saved Unity schema to a project folder without re-running the LLM.", [
        _opt("schema_file", kind="path", positional=True, help="Path to a Unity schema JSON file"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("blender_models", "--blender-models", "", "bool", default=False,
             help="Build Blender FBX models (player/enemy/vehicle) and wire them in"),
    ]),
    _cmd("unity-migrate", "unity", "Unity — Project",
         "Upgrade a data-first (v1) schema.json to a capability-aware (v2) schema.", [
        _opt("schema_file", kind="path", positional=True, help="Path to a Unity .schema.json file"),
        _opt("in_place", "--in-place", "-i", "bool", default=False, help="Overwrite the input file"),
        _opt("output", "--output", "-o", "string", help="Explicit output path"),
        _opt("dry_run", "--dry-run", "", "bool", default=False, help="Print migrated JSON without writing"),
    ]),
    _cmd("unity-catalog", "unity", "Unity — Project",
         "Scan the asset library and (re)generate lookups/unity/asset_catalog.json.", [
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("lookups", "--lookups", "", "string", default="lookups"),
        _opt("dry_run", "--dry-run", "", "bool", default=False, help="Print stats without writing"),
        _opt("check", "--check", "", "bool", default=False,
             help="Exit 1 if the on-disk catalog is stale (CI hook)"),
    ]),
    _cmd("sketchfab-fetch", "unity", "Unity — Project",
         "Search Sketchfab and freeze the best licensed model into the catalog. "
         "Requires SKETCHFAB_API_TOKEN, Playwright+Chromium, and Blender.", [
        _opt("query", kind="string", positional=True, help="e.g. 'corsair fighter plane'"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("lookups", "--lookups", "", "string", default="lookups"),
        _opt("category", "--category", "", "string", default="prop"),
        _opt("kind_hints", "--kind-hints", "", "string", help="Comma-separated entity kinds"),
        _opt("limit", "--limit", "", "int", default=24, help="Search results to rank"),
        _opt("dry_run", "--dry-run", "", "bool", default=False, help="Search+rank+print only, no download"),
    ]),
    _cmd("unity-model", "unity", "Unity — Project",
         "Generate a procedural 3D model in Blender (car/plane/humanoid/bike/furniture/weapon), "
         "export a Unity-ready FBX, and optionally wire it into a project.", [
        _opt("prompt", kind="string", positional=True,
             help="Description, e.g. 'lifted matte-black pickup' or 'tall slim humanoid'"),
        _opt("variant", "--variant", "", "choice",
             choices=["car", "plane", "humanoid", "bike", "furniture", "weapon"],
             help="Base type; inferred from the prompt if omitted"),
        _opt("knob", "--knob", "-k", "multi", help="Repeatable key=value param override, e.g. wheel_r=0.8"),
        _opt("seed", "--seed", "", "int", help="RNG seed for reproducibility"),
        _opt("rig", "--rig/--no-rig", "", "bool", default=True, help="Build armature + skin-bind"),
        _opt("demo", "--demo", "", "bool", default=False, help="Pose the rig after building"),
        _opt("name", "--name", "", "string", help="Asset name (PascalCase; inferred if omitted)"),
        _opt("unity_project", "--unity-project", "-u", "string",
             help="Wire the FBX into this Unity project via schema regen"),
        _opt("role", "--role", "", "choice", choices=["player", "enemy", "vehicle", "prop"]),
        _opt("emit", "--emit", "", "bool", default=False, help="Emit the bpy script only, skip the headless build"),
        _opt("no_llm", "--no-llm", "", "bool", default=False, help="Deterministic keyword mapping only, skip LLM"),
        _opt("out", "--out", "", "string", help="Override Blender output directory"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("model", "--model", "-m", "string", help="Override the configured LM Studio model"),
    ]),
    _cmd("unity-tools", "unity", "Unity — Project",
         "List available Unity micro-generators and their variants.", [
        _opt("action", kind="string", positional=True, default="list"),
    ]),
    _cmd("unity-verify-import", "unity", "Unity — Verification (slow)",
         "Import Phase-3 verified FBX assets into a throwaway Unity project and assert correctness. "
         "Requires Unity Editor.", []),

    # ── Unity — bespoke micros ──────────────────────────────────────────────
    _cmd("unity-audio", "unity", "Unity — Micro-generators",
         "Generate a Unity audio bank (AudioCue+Bank+AudioManager) and .wav clips via Stable Audio 3.", [
        _opt("prompt", kind="string", positional=True, help="Plain-English description"),
        _opt("file", "--file", "-f", "path", help="Read prompt from file"),
        _opt("variant", "--variant", "", "choice",
             choices=["2d_simple", "3d_spatial", "music_layers", "crossfade_zones", "categorical_mixer"]),
        _opt("theme", "--theme", "", "string"),
        _opt("mashup_with", "--mashup", "", "string"),
        _opt("seed", "--seed", "", "int"),
        _opt("out", "--out", "", "string"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("model", "--model", "-m", "string"),
        _opt("no_llm", "--no-llm", "", "bool", default=False,
             help="Skip the LLM; build the bank from the game-type's required cues (deterministic)"),
        _opt("clips", "--clips/--no-clips", "", "bool", default=True, help="Generate actual .wav clips"),
        _opt("unity", "--unity", "-u", "string",
             help="Overlay generated clips + manifest into this Unity project's Assets/Resources/"),
    ]),
    _cmd("unity-npc-dialog", "unity", "Unity — Micro-generators",
         "Generate NPC dialog lines and WAV clips via Higgs Audio v2 TTS.", [
        _opt("prompt", kind="string", positional=True, help="Plain-English NPC description"),
        _opt("file", "--file", "-f", "path", help="Read prompt from file"),
        _opt("variant", "--variant", "", "choice",
             choices=["quest_giver", "vendor", "ambient_bark", "combat_chatter", "follower", "cutscene"]),
        _opt("theme", "--theme", "", "string"),
        _opt("mashup_with", "--mashup", "", "string"),
        _opt("seed", "--seed", "", "int"),
        _opt("out", "--out", "", "string"),
        _opt("config", "--config", "-c", "string", default="config.yaml"),
        _opt("model", "--model", "-m", "string"),
        _opt("no_llm", "--no-llm", "", "bool", default=False, help="Skip the LLM; use preset dialog lines"),
        _opt("audio", "--audio/--no-audio", "", "bool", default=True, help="Generate WAV clips via Higgs Audio v2 TTS"),
        _opt("audio_style", "--audio-style", "", "choice",
             choices=["natural", "stylized", "robotic", "whisper", "radio"]),
    ]),
]

# Simple unity-<kind> / gta-<kind> micros, auto-generated from their variant tables.
for _kind, _variants in _SIMPLE_MICRO_VARIANTS.items():
    COMMANDS.append(_cmd(f"unity-{_kind}", "unity", "Unity — Micro-generators",
                          _MICRO_HELP.get(_kind, f"Unity {_kind} micro-generator."),
                          _micro_opts(_variants)))
for _kind, _variants in _GTA_MICRO_VARIANTS.items():
    COMMANDS.append(_cmd(f"gta-{_kind}", "gta", "GTA5 — Micro-generators",
                          _GTA_MICRO_HELP.get(_kind, f"GTA5 {_kind} micro-generator."),
                          _micro_opts(_variants)))

# ── Shared / engine-agnostic tooling ─────────────────────────────────────────
COMMANDS += [
    _cmd("rag-build", "shared", "Shared — RAG",
         "Scrape data into a JSONL corpus, then embed into the local ChromaDB index.", [
        _opt("targets", kind="string", positional=True, help="Scrapers to run, space-separated (default: all)"),
        _opt("force", "--force", "-f", "bool", default=False, help="Re-scrape even if the corpus exists"),
        _opt("limit", "--limit", "-l", "int", default=0, help="Cap entries (0 = unlimited)"),
    ]),
    _cmd("rag-query", "shared", "Shared — RAG",
         "Search the local RAG vector index (ChromaDB).", [
        _opt("query", kind="string", positional=True, help="Natural-language search term"),
        _opt("kind", "--kind", "-k", "string", help="Filter by kind: ped|vehicle|animation|package|component|atom"),
        _opt("domain", "--domain", "-d", "choice", default="gta5", choices=["gta5", "unity", "shared"]),
        _opt("n", "--n", "-n", "int", default=5, help="Number of results"),
    ]),
    _cmd("rag-status", "shared", "Shared — RAG / Legacy",
         "Print doc count + status for the AnythingLLM workspaces (gta + unity).", [
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("setup-workspace", "shared", "Shared — RAG / Legacy",
         "Bootstrap an AnythingLLM workspace.", [
        _opt("config", "--config", "-c", "string", default="config.yaml"),
    ]),
    _cmd("atom-list", "shared", "Shared — Atoms",
         "List Phase 2 atoms — engine-agnostic IR building blocks for the compiler.", [
        _opt("category", kind="string", positional=True,
             help="Filter by category: movement|animation|camera|audio|fx|logic|entity"),
        _opt("show_params", "--params", "-p", "bool", default=False, help="Show parameter details"),
        _opt("show_snippet", "--snippet", "-s", "string", help="Show the C# snippet for a named atom"),
        _opt("engine", "--engine", "-e", "choice", default="unity", choices=["unity", "gta5"],
             help="Snippet backend"),
    ]),
    _cmd("blender-verify-phase3", "shared", "Shared — Blender tooling",
         "Emit the Phase-3 Blender reuse/rig verification script matrix (no Blender call).", []),
    _cmd("blender-verify-run", "shared", "Shared — Blender tooling",
         "Run the Phase-3 verification matrix against headless Blender. Slow; run "
         "blender-verify-phase3 first.", []),
    _cmd("blender-verify-manager", "shared", "Shared — Blender tooling",
         "Verify BlenderJobManager cold-build/geo-reuse tiering against real Blender.", []),
    _cmd("blender-llm-test", "shared", "Shared — Blender tooling",
         "Prompt the local LLM to author bpy scripts for airplane/jeep/humanoid test cases "
         "(manual review — doesn't touch Blender).", [
        _opt("action", kind="choice", positional=True, default="generate", choices=["generate", "repair"]),
        _opt("name", kind="choice", positional=True, choices=["airplane", "jeep", "humanoid"],
             help="repair only"),
        _opt("errfile", kind="path", positional=True, help="repair only: path to an error/traceback file"),
    ]),
]


def commands_by_category() -> dict[str, list[Command]]:
    out: dict[str, list[Command]] = {"gta": [], "unity": [], "shared": []}
    for c in COMMANDS:
        out[c["category"]].append(c)
    return out


def find_command(name: str) -> Optional[Command]:
    return next((c for c in COMMANDS if c["name"] == name), None)


# Commands too slow / heavy / interactive for a one-click "Run" button — the UI
# shows them but disables Run with an explanation instead of launching a
# subprocess that could hang the panel for many minutes.
SLOW_COMMANDS = {
    "unity-verify-import", "blender-verify-run", "blender-verify-manager",
    "blender-llm-test", "sketchfab-fetch", "batch", "rag-build",
}
