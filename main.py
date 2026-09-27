"""
Mission & Project Creator — CLI entry point.

LLM front-end (Steps 1-3) + deterministic compiler back-end (Steps 4-6).
Requires LM Studio running with a model loaded, OR Ollama (set in config.yaml).

━━━ GTA5 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py generate "heist at the docks"        # LLM → C# mission script
  python main.py validate schema.json                 # recompile saved schema (no LLM)
  python main.py resolve "cult member" peds           # resolve one GTA5 asset ID

━━━ RAG index (local ChromaDB — run once, or after corpus updates) ━━━━━━━━━
  python main.py rag-build                            # scrape + embed all corpora
  python main.py rag-build weapons scenarios --force  # rebuild specific scrapers
  python main.py rag-query "cult member" --kind ped  # search the local index
  python main.py rag-build --unity                    # rebuild the Unity corpus only
  python main.py rag-status                           # check index status (gta5 + unity)

━━━ Web UI ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py serve                                # http://127.0.0.1:8000

━━━ Unity — full project ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-create "horror survival in a maze"
  python main.py unity-create "bright 3D FPS" --compose --viz reference.png --viz-overwrite
  python main.py unity-create --file prompts/neon_fugitive.txt
  python main.py unity-create "racing game" --compose         # + wire micro-generators
  python main.py unity-validate MyGame.schema.json            # recompile (no LLM)
  python main.py unity-migrate MyGame.schema.json             # upgrade v1 → v2 schema
  python main.py unity-resolve "universal render pipeline"    # look up package ID

━━━ Unity — imported visual E2E ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-vizconcept-e2e --image F:\\Downloads\\plane.png --model qwen3.5-9b --compile
  # import → visual reasoning → reconciliation → assets → Unity compile,
  # deterministic micro-generators/audio, spatial acceptance, and 6.7 freeze
  # assets: Sketchfab freeze first, then local catalog fallback

━━━ Unity — micro-generators (drop-in focused artefacts) ━━━━━━━━━━━━━━━━━━
  python main.py unity-controller "scuba diver" --variant swimming
  python main.py unity-camera     "horror game camera" --variant cinemachine_3p
  python main.py unity-physics    "floating debris" --variant buoyancy
  python main.py unity-hud        "sci-fi helmet display" --variant full_hud_bundle
  python main.py unity-input      "racing wheel + gamepad" --variant racing_wheel
  python main.py unity-ai         "guard dog that patrols and chases" --variant fsm_guard
  python main.py unity-spawner    "enemy wave system" --variant wave_spawner
  python main.py unity-saveload   "checkpoint autosave" --variant checkpoint
  python main.py unity-inventory  "equipment slots for an RPG" --variant equipment
  python main.py unity-dialogue   "branching NPC conversation" --variant branching
  python main.py unity-menu       "pause menu with settings" --variant pause_menu
  python main.py unity-interactable "locked door with keypad" --variant door
  python main.py unity-transition "fade to black on zone exit" --variant fade
  python main.py unity-health     "regenerating shield + health" --variant shielded
  python main.py unity-statemachine "zombie with 3 patrol states" --variant enemy_fsm
  python main.py unity-trigger    "boss arena that seals on entry" --variant zone_enter
  python main.py unity-animator   "humanoid soldier walk/run/shoot" --variant humanoid

━━━ Unity — audio (Stable Audio 3 prompts + standalone batch) ━━━━━━━━━━━━━━━
  python main.py unity-audio "horror maze ambient + threat SFX"
  python main.py unity-audio-batch output/unity/MyGame/Assets/Micros/audio/audio_batch.json
  python main.py unity-audio "arcade coin game" --no-llm       # deterministic cues
  python main.py unity-audio-batch output/unity/MyGame/Assets/Micros/audio/audio_batch.json -u output/unity/MyGame

━━━ Unity — Blender 3D model micro (car / plane / humanoid / bike / weapon → FBX) ━━
  python main.py unity-model "lifted matte-black pickup" --no-llm
  python main.py unity-model "tall humanoid" --variant humanoid --no-llm
  python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
  python main.py unity-model "red sports coupe" -u output/unity/TopDownRacingGame
  python main.py unity-model "glider" --variant plane --emit    # emit bpy script only

━━━ Sketchfab — freeze real marketplace models into the Unity catalog ━━━━━━
  python main.py sketchfab-fetch "corsair fighter plane" --category vehicle
  python main.py sketchfab-fetch "red high heel shoe" --dry-run   # preview only, no download
  #   Solo tool: searches Sketchfab, freezes the best license-compliant hit to
  #   lookups/unity/sketchfab_catalog.json; unity-create then resolves it OFFLINE.
  #   Needs SKETCHFAB_API_TOKEN + `pip install playwright && playwright install chromium` + Blender.
  #   See `python main.py sketchfab-fetch --help` for the full solo workflow.

━━━ Unity — world terrain (deterministic heightmap/splatmap, no LLM) ━━━━━━
  python main.py unity-terrain-gen --bounds "0,0,200,200" --biome forest
  python main.py unity-terrain-gen --bounds "0,0,120,120" --biome desert --seed 7 --scatter-details
  #   Standalone: bakes Assets/Terrain/<name>/ directly. A schema with
  #   world_spec.regions set also bakes automatically during `unity-create --compose`.

━━━ Atoms (engine-agnostic IR building blocks) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py atom-list                            # all atoms by category
  python main.py atom-list movement --params          # show parameter details
  python main.py atom-list --snippet MoveToTarget     # show Unity C# snippet

━━━ Enumerate tools ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-tools list                     # list every micro + variants

━━━ Verification / batch (manual, slow — real Blender/Unity/LLM calls) ━━━
  python main.py blender-verify-phase3                 # emit Phase-3 script matrix
  python main.py blender-verify-run                    # run matrix against headless Blender
  python main.py blender-verify-manager                # verify BlenderJobManager reuse tiering
  python main.py unity-verify-import                   # verify Phase-3 FBX imports into Unity
  python main.py batch prompts.txt                     # batch-generate GTA5 missions
  python main.py blender-llm-test generate              # LLM-authored bpy test scripts (manual review)

━━━ Global flags (work with any command) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  --verbose / -v   Show pipeline step summaries
  --debug   / -d   Show raw LLM requests and responses
  --model   / -m   Override the LM Studio model for this single run
  --seed           Seed for reproducible twist-seed / theme selection
  --theme          Theme name, 'random', or 'list'
"""
from __future__ import annotations

from cli.common import app  # noqa: F401  (re-exported: tests do `import main; main.app`)

# Each import below registers its module's @app.command(...) functions onto
# the shared `app` from cli.common — side-effect imports, order doesn't matter.
from cli import (  # noqa: F401,E402
    blender_micro,
    gta5,
    gta5_micro,
    infra,
    unity_assets,
    unity_eval,
    unity_micro,
    unity_project,
    verify,
)

if __name__ == "__main__":
    app()
