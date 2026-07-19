# Bacon & Eggs — a combinatorial game-design tool

> **© 2026 — All rights reserved. Private beta build.** (Codename "Bacon & Eggs".)
> Do not redistribute the bundle, post screenshots publicly, or commit changes
> to a public repository without permission. See `docs/TESTERS.md` for
> tester-onboarding details, and `QUICKSTART.md` for the 5-minute happy path.

**Describe a game → get a wired Unity project, composed from a library of 15 drop-in
subsystems.** A local LLM drafts the spec; everything after — schema validation, asset
resolution, code generation, composition, and a verify gate — is deterministic Python with no
LLM involved.

- **Compose a whole game** — `unity-create --compose` assembles micros (controller, camera, HUD,
  health, save, menu, AI, spawner…) into one project, attached to the right GameObjects and wired
  together (`GameWiring.cs`). Verified by a headless Unity compile gate.
- **Or generate one subsystem** — any of the 15 micro-generators standalone (see
  `docs/micro-generators.md`).
- **GTA5 mission generation** is a **secondary showcase** (the project's origin), available via the
  CLI and the UI's *Showcase* tab.

> Fastest path: **`QUICKSTART.md`**.

The engine is domain-agnostic at heart (LLM → validated schema → templated codegen → verify); Unity
is the first and primary domain.

---

## Quick start (web UI)

The whole app also runs as a local web UI; the CLI commands below still work.

```powershell
# One-time install (creates a venv, installs deps)
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# Launch the UI
python main.py serve
# → open the printed http://127.0.0.1:8000 in any browser
```

The UI's sidebar has six pages:
- **Prompt** — GTA / Unity toggle, theme, seed, prompt textarea, Custom Args (full flag list in Advanced), live progress, output panel with Copy-path / Open / ScriptHookV install reminder.
- **LLM Settings** — Creativity / Detail / Complexity sliders that write to the real config keys.
- **Backends** — pick **LM Studio** or **Ollama** (both OpenAI-compatible). RAG (local ChromaDB index) is **optional**; turn **Basic Mode** on to skip it and resolve assets from the bundled local lookups. Two index rows (GTA + Unity), each with its own status light.
- **Library** — every saved output, with star ratings, "Share" (copy path), "Open" (reveal in Explorer), and the captured LLM concept story under each row.
- **Tutorial** — first-run walkthrough.
- **Errors & Fixes** — searchable catalog mapping real failure signatures to actionable steps.

---

## What's new (private-beta build)

- **Web UI** at `python main.py serve` (FastAPI + dark sidebar SPA).
- **Ollama** supported alongside LM Studio (`lm_studio.backend: "ollama"` in `config.yaml`, or pick it in the UI).
- **Basic Mode** — toggle in Backends; skips RAG and uses local lookups.
- **Unity RAG** — local ChromaDB `unity` domain built from `rag/corpus/unity/*.jsonl`; `UnityResolver` does RAG-first / local-fallback.
- **Library** captures the LLM's Step-2 concept story alongside every saved output (GTA: in `schema.meta.concept`; Unity: in a `.concept.txt` sidecar inside the project).
- **`python main.py rag-status`** — prints per-domain index status (gta5 + unity).
- **Smoke gate** — `pytest -m smoke` runs a real-output structural check on everything under `output/` (see `docs/TESTING.md`).

---

## How it works

Both pipelines share the same 6-step architecture:

```
Your prompt
    │
    ▼
Step 2 — LLM writes a concept (story / game brief, plain English)
    │
    ▼
Step 3 — LLM fills a structured JSON schema from the concept
    │
    ▼
Step 4 — Resolver maps descriptions → real IDs (RAG + local lookup tables)
    │
    ▼
Step 5 — Programmatic fixer + consistency enforcement (no LLM)
    │
    ▼
Step 6 — Compiler generates output from schema (no LLM)
    │
    ▼
GTA5: .3.cs script + .schema.json
Unity: complete project folder ready to open
```

The LLM never touches Step 4-6. The same schema can be recompiled instantly without re-running the LLM.

---

## Requirements

| Tool | Purpose | Download |
|---|---|---|
| Python 3.11+ | Runtime | [python.org](https://python.org) |
| [LM Studio](https://lmstudio.ai) **or** [Ollama](https://ollama.com) | Local LLM inference | Required for both pipelines (pick one) |
| Local RAG index (`python main.py rag-build`) | ChromaDB index for sharper GTA + Unity asset resolution | **Optional** — turn on Basic Mode in the UI to skip it |

**Recommended model:** Qwen3 8B or similar instruction-tuned model loaded in LM Studio.

---

## Installation

```bash
cd GTA_mission_LLM

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux

pip install -r requirements.txt
```

---

## Setup

### 1 — Start LM Studio

Open LM Studio, load a model, and start the local server on port `1234` (the default).

### 2 — Configure `config.yaml`

```yaml
lm_studio:
  base_url: "http://localhost:1234/v1"
  model: "your-loaded-model-name"   # must match exactly what LM Studio shows
```

### 3 — Build the local RAG index (optional, sharper asset resolution)

```bash
python main.py rag-build            # gta5 corpus
python main.py rag-build --unity    # unity corpus
```

---

## GTA5 Pipeline

Generates a `.3.cs` SHVDN3 script that creates a fully playable mission — peds, vehicles, props, steps, blips, weather, and audio — ready to drop into your GTA5 `scripts/` folder.

### **Tutorial: Your first mission**

**1. Run one command:**

```bash
python main.py generate "Infiltrate the docks at night and steal a drug shipment"
```

**2. Watch the pipeline work.** Each step prints its progress:

```
╭─ GTA5 Mission Pipeline ────────────────────────────────╮
│ Infiltrate the docks at night and steal a drug shipment │
╰─────────────────────────────────────────────────────────╯

STEP 2 -- LLM generates mission concept
  OK 1247 chars of story generated

STEP 3 -- LLM fills mission schema
  OK 4 steps  2 peds  1 vehicles  1 objects
  OK Location 'docks' matches concept keywords

STEP 4 -- RAG resolves model names
  OK All model names resolved

STEP 5 -- Validating schema
  OK Programmatic fixes applied (weather, counts, whitespace)
  OK All models resolved — no skipped spawns

STEP 6 -- Pipeline compiles to .NET 4.8 C#
  OK Written -> output/DocksInfiltration.3.cs
  OK Schema  -> output/DocksInfiltration.schema.json
```

**3. Look at the output.** The `.3.cs` file is a complete SHVDN3 script:

```csharp
// Auto-generated by GTA5 Mission Pipeline — DO NOT EDIT
// Mission: Docks Infiltration

using GTA;
using GTA.Math;
using GTA.Native;

public class DocksInfiltration : Script
{
    private enum State { Inactive, Active, Complete, Failed }
    private State _state = State.Inactive;
    private int   _step  = 0;

    private static readonly Vector3 SpawnOrigin =
        new Vector3(-1009.0f, -2704.0f, 13.8f);

    private static readonly Vector3[] Waypoints =
    {
        new Vector3(-949.0f, -2704.0f, 13.8f),  // Infiltrate the shipping yard
        new Vector3(-1009.0f, -2644.0f, 13.8f),  // Plant beacon on container chassis
    };
    // ... Init(), OnTick() state machine, End() cleanup
}
```

**4. Install it.** Copy the `.3.cs` file into your GTA5 `scripts/` folder. The mission auto-loads and places a start blip on the map.

**5. Iterate without the LLM.** Edit the `.schema.json` file (change weapons, add peds, swap locations) and recompile instantly:

```bash
python main.py validate output/DocksInfiltration.schema.json
```

---

### Generate a mission

```bash
python main.py generate "A night raid on the military base to steal a prototype missile"
```

Output: `output/NightRaid.3.cs` + `output/NightRaid.schema.json`

### Recompile a saved schema (no LLM)

```bash
python main.py validate output/NightRaid.schema.json
```

### Resolve a single asset

```bash
python main.py resolve "armoured cop" peds
python main.py resolve "sports car" vehicles
```

### Batch generation

Generate multiple missions from a file (one prompt per line, `#` for comments):

```bash
python batch_generate.py prompts.txt
```

### Flags

```bash
python main.py generate "..." --verbose    # show pipeline step summaries
python main.py generate "..." --debug      # show raw LLM requests and responses
```

### What gets generated

The `.3.cs` file contains a complete SHVDN3 script:

- `Init()` — spawns all peds, vehicles, and props with spread offsets; sets weather, time, and blip
- `OnTick()` — state machine stepping through your mission objectives
- `End(bool)` — cleanup and audio sting on pass/fail

---

## Unity Pipeline

Generates a complete, openable Unity project folder with real scripts, scenes, prefabs, a `manifest.json`, input actions, editor setup, and a README — all from a single description.

### **Tutorial: Your first Unity project**

**1. Run one command:**

```bash
python main.py unity-create "A cyberpunk bounty-hunter game with parkour, enemy AI, and neon visuals"
```

Or use a detailed prompt file for full control:

```bash
python main.py unity-create --file prompts/neon_fugitive.txt
```

**2. Watch the pipeline work:**

```
╭─ Unity Project Creator Pipeline ─────────────────────────────╮
│ A cyberpunk bounty-hunter game with parkour and neon visuals  │
╰──────────────────────────────────────────────────────────────╯

STEP 2 -- LLM generates project concept
  OK  2031 chars of project concept generated

STEP 3 -- LLM fills project schema
  OK  Game: 3D  Pipeline: URP  Platform: PC  Version: 6000.0
  OK  8 packages  14 scripts  8 scenes  4 features

STEP 4 -- Resolver maps packages to Unity Package Manager IDs
  OK  All 8 packages resolved

STEP 5 -- Programmatic fixer + consistency check
  OK  Programmatic fixes applied (naming, scenes, script names)
  FIX Injected MaterialPalette (3D project)
  FIX Injected MeshBuilder (procedural_meshes defined)

STEP 6 -- Generator compiles schema -> Unity project folder
  OK  Project written to: output/unity/NeonFugitive
```

**3. See what was created.** A real, openable Unity project:

```
NeonFugitive/
├── Assets/
│   ├── Scripts/               # 17 C# scripts
│   │   ├── GameManager.cs
│   │   ├── PlayerController.cs
│   │   ├── EnemyAIController.cs
│   │   ├── HealthSystem.cs
│   │   ├── DialogueManager.cs
│   │   ├── BountySystem.cs
│   │   ├── DayNightCycle.cs
│   │   ├── AudioManager.cs
│   │   ├── MaterialPalette.cs
│   │   └── ...
│   ├── Scenes/                # 8 scenes
│   │   ├── Main.unity
│   │   ├── MainMenu.unity
│   │   ├── MarketZone.unity
│   │   ├── CorporateHQ.unity
│   │   ├── UndergroundTunnels.unity
│   │   ├── RooftopParkour.unity
│   │   ├── DistrictSlums.unity
│   │   └── FloodWaters.unity
│   ├── Prefabs/               # Player + Enemy prefabs
│   ├── Materials/             # Palette-based materials (Primary, Secondary, Accent, Trim)
│   ├── Models/                # Procedural mesh .obj files
│   ├── Editor/                # URP setup + level generator
│   └── Settings/              # InputActions, URP assets
├── Packages/
│   └── manifest.json          # All resolved dependencies
├── ProjectSettings/           # Version, quality, tags, build settings
└── README.md
```

**4. Open it.** In Unity Hub: **Add → Add project from disk** → select the `NeonFugitive` folder. Unity imports everything and you're in the editor.

**5. Iterate.** Edit the schema JSON, tweak scripts or scenes, recompile without the LLM:

```bash
python main.py unity-validate output/unity/NeonFugitive.schema.json
```

---

### Generate a project

```bash
python main.py unity-create "A 2D puzzle platformer with Cinemachine and the Input System"
```

Output: `output/unity/PuzzlePlatformer/` — open directly in Unity Hub.

### From a detailed prompt file

For complex projects, write a `.txt` brief and pass it with `--file`:

```bash
python main.py unity-create --file prompts/neon_fugitive.txt
```

See `prompts/neon_fugitive.txt` for an example of a full project specification with scenes, audio, procedural meshes, and colour palettes.

### Recompile a saved schema (no LLM)

```bash
python main.py unity-validate output/unity/PuzzlePlatformer.schema.json
```

### Resolve a single package

```bash
python main.py unity-resolve "cinemachine"
python main.py unity-resolve "universal render pipeline" 6000.0
```

### What gets generated

```
ProjectName/
├── Assets/
│   ├── Scripts/          # All C# scripts from templates
│   ├── Scenes/           # .unity scene files + .meta files
│   ├── Prefabs/          # Player and Enemy prefabs
│   ├── Settings/         # InputActions, URP/HDRP asset stubs
│   ├── Editor/           # Render pipeline auto-setup script
│   ├── Materials/        # Placeholder folder
│   ├── Audio/            # Placeholder folder
│   └── Textures/         # Placeholder folder
├── Packages/
│   └── manifest.json     # All resolved Package Manager dependencies
├── ProjectSettings/
│   ├── ProjectVersion.txt
│   ├── ProjectSettings.asset
│   ├── TagManager.asset
│   ├── QualitySettings.asset
│   └── EditorBuildSettings.asset
└── README.md             # Per-project setup notes
```

**Supported game types:** `2D` · `2.5D` · `3D` · `top_down` · `isometric` · `VR`  
**Render pipelines:** `URP` · `HDRP` · `Built-in`  
**Platforms:** `PC` · `Android` · `iOS` · `WebGL` · `Console`

---

## Writing effective prompts

### One-liners

Short prompts work well when you name the key elements:

**GTA5** — mention the **location**, **characters**, and **objective**:

```
"A prison break at Bolingbroke where your ally is trapped in the yard"
"Escort a stolen military truck from Fort Zancudo to the docks at night"
"Ambush a drug deal at Sandy Shores and steal the cash"
```

**Unity** — mention the **game type**, **key features**, and **platform**:

```
"A 2D roguelike dungeon crawler with procedural rooms and permadeath"
"A VR archery game with physics-based arrows and Cinemachine"
"Top-down zombie survival for mobile with touch controls and NavMesh AI"
```

### Prompt files for complex projects

When a one-liner isn't enough, write a `.txt` file and use `--file`:

```bash
python main.py unity-create --file prompts/neon_fugitive.txt
```

A good prompt file has clear sections the LLM can parse. Here's the structure from `prompts/neon_fugitive.txt`:

```
Neon Fugitive — a third-person open-world cyberpunk bounty-hunter game for PC.

NARRATIVE — eight acts of escalating story beats:
1. Activation: player boots up for the first time in an alley (on_start)
2. First Mark: player receives the first bounty contract (on_enter_zone)
...

SYSTEMS — everything this game needs:
- Third-person player movement with parkour (wall-run, vault, slide)
- Cinemachine third-person camera with lock-on targeting mode
- Enemy AI with NavMesh patrol, chase, and alert states
...

VISUAL STYLE: stylized — heavy neon on deep dark backgrounds.
COLOR PALETTE name "NeonFugitive":
  primary   #00F5FF  (player energy — cyan glow)
  secondary #1A0A2E  (environment base — near-black violet)
  accent    #FF2D78  (enemies and hazards — hot pink)
...

RENDER PIPELINE: URP
UNITY VERSION: 6000.0
PLATFORM: PC
```

The more specific you are, the better the schema. Vague prompts produce generic projects. Named systems, explicit scene lists, and colour palettes all carry through to the generated code.

---

## Config reference

```yaml
lm_studio:
  base_url:            "http://localhost:1234/v1"
  model:               "model-name"       # exact name from LM Studio
  temperature:         0.7                # default generation temperature
  max_tokens:          8192
  timeout:             600                # seconds; local models can be slow
  concept_temperature: 0.85              # higher creativity for story step
  schema_temperature:  0.2               # lower for JSON accuracy

rag:
  fallback_to_local: true               # use local lookups if RAG index is unavailable
  # index_dir: rag/index                # optional override for the ChromaDB path

pipeline:
  output_dir:    "output"               # where .3.cs files are written
  lookups_dir:   "lookups"
  template_file: "templates/mission.cs.j2"

unity_pipeline:
  lookups_dir:          "lookups"
  unity_template_dir:   "templates/unity"
  unity_output_dir:     "output/unity"  # where Unity project folders are written

logging:
  show_prompts: false                   # set true to print LLM prompts to console
```

---

## Asset resolution

### GTA5

The GTA pipeline resolves plain-English descriptions to GTA5 model IDs in two passes:

1. **RAG (local ChromaDB index)** — vector search over `rag/corpus/gta5/*.jsonl` for an exact model name
2. **Local lookup tables** — `lookups/peds.json`, `lookups/vehicles.json`, `lookups/objects.json` with synonym maps
3. **LLM fallback** — if both fail, asks the LLM to pick the closest model from the valid list

`fallback_to_local: true` means the pipeline always produces output even when the RAG index isn't built.

### Unity

The Unity pipeline uses only local lookup tables (`lookups/unity/packages.json`) — no RAG server needed. Package resolution is fully offline.

---

## Extending

### Add GTA5 asset synonyms

Edit `lookups/peds.json` (or `vehicles.json`, `objects.json`):

```json
{
  "synonyms": {
    "your description": "model_id_here",
    "another alias":    "model_id_here"
  },
  "valid_models": ["model_id_here", ...]
}
```

### Add Unity packages

Edit `lookups/unity/packages.json`:

```json
{
  "synonyms": {
    "my package description": "com.company.package-name"
  },
  "versions": {
    "com.company.package-name": {
      "6000.0": "1.2.3"
    }
  }
}
```

### Add Unity script templates

Add a `.cs.j2` Jinja2 template to `templates/unity/scripts/` and register the script type in `src/unity_generator.py` under `_SCRIPT_TEMPLATES`.

---

## Testing

```bash
python -m pytest tests/ -q
```

326 tests covering schema validation, code generation, asset resolution, pipeline consistency checks, and end-to-end runs for both pipelines. LLM and RAG calls are mocked — no running servers needed to run the test suite.

```bash
python -m pytest tests/ -v          # verbose per-test output
python -m pytest tests/test_gta_pipeline.py   # one module only
```

---

## Project structure

```
GTA_mission_LLM/
├── main.py                    # CLI entry point (Typer)
├── batch_generate.py          # Batch GTA mission generator
├── config.yaml                # All configuration
├── requirements.txt
│
├── src/
│   ├── pipeline.py            # GTA5 6-step pipeline
│   ├── unity_pipeline.py      # Unity 6-step pipeline
│   ├── mission_schema.py      # GTA5 Pydantic schema
│   ├── unity_schema.py        # Unity Pydantic schema
│   ├── code_generator.py      # GTA5 → .3.cs (Jinja2)
│   ├── unity_generator.py     # Unity schema → project folder
│   ├── resolver.py            # GTA5 model resolver (RAG + local)
│   ├── unity_resolver.py      # Unity package resolver (local)
│   ├── unity_asset_resolver.py
│   ├── lm_studio_client.py    # OpenAI-compatible local LLM client
│   ├── backends.py            # Chat + RAG backend selection, health checks
│   ├── prompts.py             # GTA5 LLM prompt templates
│   └── unity_prompts.py       # Unity LLM prompt templates
│
├── templates/
│   ├── mission.cs.j2          # GTA5 C# template
│   └── unity/                 # Unity file templates (scenes, scripts, prefabs…)
│
├── lookups/
│   ├── peds.json              # GTA5 ped synonyms + valid models
│   ├── vehicles.json
│   ├── objects.json
│   ├── weapons.json
│   └── unity/
│       └── packages.json      # Unity Package Manager lookup table
│
├── rag/
│   ├── corpus/                # gta5/ + unity/ RAG source corpora (.jsonl)
│   ├── index/                 # local ChromaDB index (rag-build output)
│   └── query.py                # RagClient — local vector search
├── prompts/                   # Reusable prompt files for --file flag
│   └── neon_fugitive.txt      # Example: detailed Unity project brief
│
├── tests/                     # pytest suite (326 tests)
└── output/                    # Generated files (gitignored)
    ├── *.3.cs                 # GTA5 mission scripts
    └── unity/                 # Unity project folders
```

---

## Troubleshooting

**LM Studio returns an empty response**  
Check that a model is actually loaded and the server is running (`http://localhost:1234/v1`). Also check `temperature` in `config.yaml` — some models produce empty output at exactly `0.0`. The pipeline clamps to `0.05` minimum, but verify your config isn't overriding that.

**RAG index not built / empty**  
Run `python main.py rag-build` (gta5) and `python main.py rag-build --unity` (unity) to build the local ChromaDB index, then check with `python main.py rag-status`. If the index isn't built but `fallback_to_local: true` is set, the GTA pipeline will still work using the local lookup tables — just with less accurate model resolution.

**"UNRESOLVED" models in the generated C# output**  
The description didn't match anything in RAG or the local synonyms. Add your own synonym to the relevant lookup file (`lookups/peds.json`, `vehicles.json`, or `objects.json`) and regenerate. See [Extending → Add GTA5 asset synonyms](#add-gta5-asset-synonyms).

**Unity project won't open in the editor**  
Check that your installed Unity version matches the `unity_version` in the schema (default is `6000.0` = Unity 6 LTS). Open `ProjectSettings/ProjectVersion.txt` inside the generated folder to see the exact version string.

**JSON parse error / truncated schema**  
The LLM ran out of tokens mid-response. Increase `max_tokens` in `config.yaml` (default is `8192`). For complex Unity prompts with many scripts and scenes, try `16384`.

**Pipeline is slow**  
Local LLM inference is the bottleneck. The `timeout` config (default `600` seconds) exists because large models can take minutes per call. Steps 4-6 are instant — only Steps 2 and 3 wait on the LLM.
