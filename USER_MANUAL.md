# Bacon & Eggs — Mission & Project Creator
## User Manual

**Codename:** Bacon & Eggs  
**Primary CLI:** `python main.py`  
**Pipelines:** GTA5 Mission Generator · Unity Project Generator  
**Audience:** First-time users (16+), modders, game developers, engineers

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture & Data Flow](#2-architecture--data-flow)
3. [Installation & Setup](#3-installation--setup)
4. [Running the Unity Pipeline](#4-running-the-unity-pipeline)
5. [Running the GTA5 Pipeline](#5-running-the-gta5-pipeline)
6. [Micro-Generators](#6-micro-generators)
7. [Configuration & Customization](#7-configuration--customization)
8. [Troubleshooting](#8-troubleshooting)
9. [Glossary](#9-glossary)

---

## 1. Overview

### What Is This? (Explain Like I'm 16)

Bacon & Eggs is a tool that turns a plain-English description of a game idea into working code — automatically.

You type something like:

```
python main.py unity-create "A 2D platformer where a robot escapes a collapsing space station"
```

…and the system generates a **complete Unity project folder** that you can open directly in the Unity Editor and press Play.

Or for GTA5:

```
python main.py generate "A heist mission at the docks with a getaway boat"
```

…and the system generates a **complete GTA5 mission C# script** you drop into your SHVDN3 scripts folder.

You don't need to know how to write game code. The system figures out the scenes, scripts, packages, assets, lighting, HUD layout, input bindings — everything. It writes hundreds of files in the time it takes you to pour a drink.

---

### Two Pipelines

| Feature | GTA5 Pipeline | Unity Pipeline |
|---|---|---|
| Output | `.3.cs` SHVDN3 mission script | Complete Unity project folder |
| Language | C# (GTA modding framework) | C# (Unity engine) |
| CLI command | `python main.py generate` | `python main.py unity-create` |
| Schema | `MissionSchema` | `UnitySchema` |
| Asset resolver | `ModelResolver` (peds/vehicles/props) | `UnityResolver` (packages) |
| RAG required | Recommended (for model IDs) | Optional |

Unity is the **primary domain** and receives the most features. GTA5 is a **showcase** demonstrating that the same 6-step architecture can target a completely different output format.

---

### The 6-Step Pipeline

Both pipelines share the same architecture — six numbered steps. The critical boundary is between Steps 3 and 4:

| Step | Name | LLM? | Notes |
|---|---|---|---|
| 1 | Theme + Archetype Setup | No | Resolves theme; GTA5 also detects archetype |
| 2 | Concept Generation | **Yes** | LLM writes a free-text design document |
| 3 | Schema Generation | **Yes** | LLM fills a structured JSON form |
| 4 | Resolution | No | Maps names to real asset/package IDs |
| 5 | Fix + Consistency | No | Cleans, validates, and repairs the schema |
| 6 | Compile | No | Writes every output file from templates |

**Steps 2–3 are the only steps that call the LLM.** Steps 4–6 are pure deterministic code — they always produce the same output from the same schema.

> **Tech Tip — Why only Steps 2–3 use the LLM:**  
> LLMs are creative but expensive, slow, and non-deterministic. The system uses the LLM only for the parts that require creativity (inventing a concept, designing the schema). Everything after Step 3 is a compiler — it transforms a validated data structure into output files. This means you can regenerate the entire project from a saved schema at any time, instantly, with no LLM cost. The `unity-validate` and `validate` commands do exactly this.

> **Tech Tip — What "deterministic" means:**  
> Given the same schema file, Steps 4–6 always produce byte-for-byte identical output. GUIDs in `.meta` files are computed as MD5 hashes of the project name + asset path, so they never change across regenerations. This matters for Unity: stable GUIDs mean scene references to scripts don't break when you regenerate.

---

### What You Get at the End

**Unity pipeline output** — a complete project folder at `output/unity/<ProjectName>/`:

```
<ProjectName>/
├── Assets/
│   ├── Scenes/<Scene>.unity + .meta
│   ├── Scripts/<Script>.cs + .meta
│   ├── Materials/<name>.mat + .meta
│   ├── Meshes/<name>.obj + .meta
│   ├── InputSystem/<name>.inputactions
│   ├── HUD/GameHUD.uxml + PanelSettings.asset
│   ├── Prefabs/EntitySpawner.prefab
│   └── _UserAssets/  (placeholder folders for your own assets)
├── Packages/manifest.json
├── ProjectSettings/
├── .concept.txt           (the AI's design document)
└── .compile_map.json      (maps each .cs to its source template)
```

**GTA5 pipeline output** — a single C# script at `output/<MissionName>.3.cs`, ready for SHVDN3.

---

## 2. Architecture & Data Flow

### Full System Diagram

```
User prompt (plain English)
        │
        ▼
┌───────────────────────────────────────────────────────┐
│  STEP 1 — Theme + Archetype Setup  [no LLM]           │
│                                                       │
│  resolve_theme(user_prompt, theme_name, mashup_with)  │
│  → active_theme, rng, theme_meta                      │
│  → theme_block (injected into Step 2 prompt)          │
│  → schema_hints (injected into Step 3 prompt)         │
│                                                       │
│  GTA5 only:                                           │
│  detect_archetype(user_prompt)                        │
│  → archetype_block + structure_block                  │
│    (injected into Step 2 concept prompt)              │
└───────────────────────────┬───────────────────────────┘
                            │ enriched prompt fragments
                            ▼
┌───────────────────────────────────────────────────────┐
│  STEP 2 — Concept Generation  [LLM, temperature 0.85] │
│                                                       │
│  llm.chat(CONCEPT_SYSTEM, CONCEPT_USER)               │
│  → free-text design document (~300–600 chars)         │
│  Saved as .concept.txt sidecar in Unity output        │
└───────────────────────────┬───────────────────────────┘
                            │ concept text
                            ▼
┌───────────────────────────────────────────────────────┐
│  STEP 3 — Schema Generation  [LLM, temperature 0.2]   │
│                                                       │
│  llm.chat(SCHEMA_SYSTEM, SCHEMA_FILL_USER)            │
│  → raw JSON string                                    │
│  extract_json() strips markdown fences + repairs      │
│  Pydantic validates: invalid fields clamped/repaired  │
│  → validated MissionSchema / UnitySchema              │
└───────────────────────────┬───────────────────────────┘
                            │ validated schema
                            ▼
┌───────────────────────────────────────────────────────┐
│  STEP 4 — Resolution  [no LLM]                        │
│                                                       │
│  Unity:  UnityResolver.resolve_schema()               │
│           + inject_required_packages()                │
│           + build_manifest_deps()                     │
│  GTA5:   ModelResolver.resolve_schema()               │
│           RAG → local lookup → "UNRESOLVED"           │
└───────────────────────────┬───────────────────────────┘
                            │ resolved schema
                            ▼
┌───────────────────────────────────────────────────────┐
│  STEP 5 — Fix + Consistency  [no LLM]                 │
│                                                       │
│  _programmatic_fix(): PascalCase, dedup, caps, lint   │
│  _check_consistency(): cross-field logic rules        │
│  GTA5 extra: _fix_unresolved() LLM fallback pass      │
│  GTA5 extra: _check_location() concept vs location    │
└───────────────────────────┬───────────────────────────┘
                            │ clean, consistent schema
                            ▼
┌───────────────────────────────────────────────────────┐
│  STEP 6 — Compile  [no LLM]                           │
│                                                       │
│  Unity: UnityProjectGenerator.generate()              │
│         → dict[relative_path → file_content]          │
│         .write() → project folder on disk             │
│  GTA5:  CodeGenerator + Jinja2 → .3.cs file           │
└───────────────────────────┬───────────────────────────┘
                            │
                            ▼
                      OUTPUT FILES
```

---

### `UnityPipeline.run()` — Detailed Flow

```python
def run(user_prompt, theme=None, mashup_with=None, seed=None, compose=False):

    # Before Step 2: theme resolution
    active_theme, rng, theme_meta = resolve_theme(user_prompt, theme, mashup_with, seed)
    theme_block    = render_concept_bias(active_theme, rng)   # → injected into Step 2
    schema_hints   = render_schema_hints(active_theme)        # → injected into Step 3

    # STEP 2: LLM writes concept
    concept = llm.chat(UNITY_CONCEPT_SYSTEM,
                       UNITY_CONCEPT_USER.format(user_prompt, theme_block),
                       temperature=concept_temp, thinking=True)

    # STEP 3: LLM fills schema
    raw_json = llm.chat(UNITY_SCHEMA_SYSTEM,
                        UNITY_SCHEMA_USER.format(concept, schema_hints),
                        temperature=schema_temp)
    schema = UnitySchema.model_validate(extract_json(raw_json))

    # STEP 4: resolve packages + inject required
    schema = UnityResolver.resolve_schema(schema)
    schema = UnityResolver.inject_required_packages(schema)
    deps   = UnityResolver.build_manifest_deps(schema, inject_mcp_bridge)

    # STEP 5: programmatic fix + consistency check
    schema = _programmatic_fix(schema)
    schema = _check_consistency(schema)

    # STEP 6: generate project
    files  = UnityProjectGenerator.generate(schema, deps)
    root   = UnityProjectGenerator.write(files, output_dir)

    # --compose mode: run MicroComposer on top
    if compose:
        subs   = subsystems_for(schema.game_type, schema.features, schema.locomotion)
        units  = MicroComposer(schema.project_name).compose(subs, root / "Assets")
        # GameWiring.cs emitted; COVERED_SCRIPT_TYPES dropped from main project
```

---

### `UnityProjectGenerator.generate()` — What It Produces

`generate()` returns a `dict[relative_path → file_content]`. It is called once in Step 6 and never touches the LLM. Key outputs:

- **Scenes** — YAML scene files from `scene.unity.j2`; camera positioned by `_CAMERA_PRESETS[game_type]`; lighting by `_LIGHTING_BY_STYLE[visual_style]`; sun rotation by `_sun_rotation(time_of_day)`
- **Scripts** — each script type maps to a Jinja2 template via `_SCRIPT_TEMPLATES`; `extra_*` fields authored by the LLM are injected at marked locations; `lint_extensible()` cleans C# fragments
- **Materials** — palette-based `.mat` files via `_generate_material()`; render pipeline (URP/HDRP/builtin) auto-detected from packages
- **Meshes** — quad-plane `.obj` placeholders via `_generate_obj()`; rebuilt at runtime by `MeshBuilder.cs`
- **InputActions** — `.inputactions` JSON via `_generate_input_actions()`; Player + UI maps; control schemes tailored to game_type (2D/3D/VR/mobile)
- **HUD** — `GameHUD.uxml` + `PanelSettings.asset` emitted when a `ui_manager` script is present and `game_type ≠ VR`; GUIDs are fixed constants
- **`.meta` files** — every asset gets a deterministic `.meta` via `_make_guid(seed)` = MD5(seed string); stable across regenerations
- **`manifest.json`** — three-tier dependency fallback (declared version → re-resolve → SKIP with warning); always injects 27 `BUILTIN_MODULES`; optionally injects MCP bridge
- **`.concept.txt`** — the Step 2 concept text, written to project root
- **`.compile_map.json`** — maps each `.cs` to its source `.j2` template for error attribution
- **`_UserAssets/`** — empty skeleton mirrored from `templates/unity/user_assets_skeleton/`

---

### `MissionPipeline.run()` — GTA5 Detailed Flow

```
Before Step 2:
  resolve_theme()       → theme_block, schema_hints
  detect_archetype()    → archetype_block, structure_block
    (keyword scoring: prompt 3×, concept 1×)

STEP 2: LLM generates mission concept
  Prompt includes: theme_block + archetype_block

STEP 3: LLM fills MissionSchema JSON
  Prompt includes: concept + schema_hints + structure_block
  Schema validated by Pydantic; unknown step types → "wait"
  _check_location(): concept keywords vs location field

STEP 4: ModelResolver.resolve_schema()
  For peds, vehicles, objects, audio:
    RAG (AnythingLLM) → local JSON lookup → "UNRESOLVED"
  _dedup_models(): same description → same model;
                   different description + same model → find unused alternate

STEP 5: _programmatic_fix()
  weather uppercase, count clamping, step param clamping, C# lint
  _check_consistency():
    PICKUP step → pickup prop present
    KILL_ALL   → peds present
    GET_IN_VEHICLE → driveable vehicle present
    RACE_LAP/CHECKPOINT → race opponent vehicle present
    DELIVER → preceding GO_TO step present
  _fix_unresolved(): LLM fallback for remaining UNRESOLVED entries

STEP 6: CodeGenerator renders mission.cs.j2 → output/<Name>.3.cs
```

---

## 3. Installation & Setup

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.10+ | Check: `python --version` |
| LM Studio | Latest | Provides local LLM inference |
| Ollama | Optional | Alternative LLM backend |
| AnythingLLM | Optional | Enables RAG; pipeline works without it |
| Unity Hub + Editor | 6.x+ recommended | For opening Unity output |
| SHVDN3 | Latest | GTA5 output only |

---

### Quick Install (Windows)

```powershell
git clone <repo-url>
cd GTA_mission_LLM
./scripts/install.ps1
```

The installer creates a Python virtual environment and installs all dependencies from `requirements.txt`.

---

### Manual Install

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
```

---

### LLM Backend Setup

**Option A — LM Studio (default)**

1. Install [LM Studio](https://lmstudio.ai) and load a model (e.g., `qwen2.5:7b` or `qwen3.5-9b`)
2. Start the local server (default port `1234`)
3. `config.yaml` → `lm_studio.backend: lm_studio`

**Option B — Ollama**

```bash
ollama pull qwen2.5:7b
```

`config.yaml` → `lm_studio.backend: ollama`, `ollama.base_url: http://localhost:11434/v1`

**Override model for a single run:**

```bash
python main.py unity-create "A racing game" --model mistral-7b
```

> **Tech Tip — How `_cfg()` loads configuration:**  
> Every command calls `_cfg(config_path, model)`. It reads `config.yaml` via `yaml.safe_load()`, then optionally overrides `cfg["lm_studio"]["model"]` if `--model` was passed. This is necessary because LM Studio serves whatever model a request names — `_cfg()` makes the override flow through consistently without modifying the file on disk.

> **Tech Tip — How `_unity_pipe_cfg()` works:**  
> `_unity_pipe_cfg(cfg)` returns `cfg.get("unity_pipeline", cfg.get("pipeline", {}))`. This provides a clean fallback: if no `unity_pipeline` block exists in `config.yaml`, it falls back to the generic `pipeline` block, so the pipeline always gets a valid config dict.

---

### RAG Setup (GTA5 recommended, Unity optional)

RAG (Retrieval-Augmented Generation) improves asset name resolution by letting the system search your reference documents semantically.

**One-time workspace bootstrap:**

```bash
python main.py setup-workspace
```

This creates the AnythingLLM workspaces, sets query mode, and uploads your `rag_docs/` documents automatically.

**Manual RAG document upload:**

```bash
python main.py setup-rag
```

**Check RAG status:**

```bash
python main.py rag-status
```

Prints per-workspace document count and embedding status for both GTA5 and Unity workspaces.

**`config.yaml` RAG settings:**

```yaml
anythingllm:
  base_url: http://localhost:3001
  api_key: YOUR_API_KEY
  workspace_slug: llm_gta           # GTA5 workspace
  unity_workspace_slug: unity        # Unity workspace
  fallback_to_local: true            # Always produce output even without RAG
```

> **Tech Tip:** `fallback_to_local: true` means the pipeline never hard-fails due to a missing RAG connection. If AnythingLLM is unreachable or returns no match, resolution degrades to local JSON lookup tables. GTA5 has a final LLM fallback after that.

**Basic mode (no RAG at all):**

```yaml
basic_mode: true
```

---

### Global Flags

These flags work with **every** `main.py` command:

| Flag | Short | Effect |
|---|---|---|
| `--verbose` | `-v` | Show pipeline step summaries |
| `--debug` | `-d` | Show raw LLM requests and responses |

```bash
python main.py --verbose unity-create "A space shooter"
python main.py --debug generate "A bank heist"
```

> **Tech Tip:** Internally, `--verbose` sets `logging.INFO` and `--debug` sets `logging.DEBUG`. Without either flag, the logging level is `WARNING`, so only errors appear. `show_prompts: true` in `config.yaml` also prints full LLM prompts — useful for prompt engineering.

---

### Local Web UI

```bash
python main.py serve
python main.py serve --host 0.0.0.0 --port 9000
```

Launches a local web interface at `http://127.0.0.1:8000` for setup, connection testing, and generation — useful if you prefer a browser to the command line.

---

## 4. Running the Unity Pipeline

### Primary Command: `unity-create`

```bash
python main.py unity-create "A top-down zombie survival game with crafting"
```

**Full options:**

```bash
# Read prompt from a text file (for long structured briefs)
python main.py unity-create --file prompts/neon_fugitive.txt

# Apply a theme
python main.py unity-create "A dungeon crawler" --theme cyberpunk

# Blend two themes together
python main.py unity-create "A heist" --theme noir --mashup cyberpunk

# Add --compose to wire micro-generators into the project
python main.py unity-create "A 3D action game" --compose

# Use a specific model for this run only
python main.py unity-create "A racing game" --model mistral-7b

# List available themes
python main.py unity-create --theme list
```

---

### Step 1 — Theme Resolution (no LLM)

Before the pipeline steps begin, `resolve_theme()` is called:

- If `--theme <name>` is provided, that theme is loaded from `themes/`.
- If `--theme random` is used, a random theme is selected.
- If `--theme list` is used, all themes are printed and the command exits.
- If `--mashup <name>` is also provided, the two themes are blended.
- If no theme is specified, the prompt is checked for keyword matches against known themes (auto-detect).
- If nothing matches, no theme is applied.

The theme produces two text fragments:
- `theme_block` — injected into the Step 2 prompt to bias concept generation
- `schema_hints` — injected into the Step 3 prompt to bias schema field values

---

### Step 2 — Concept Generation (LLM)

**What the LLM sees:** The `UNITY_CONCEPT_SYSTEM` prompt (instructions for what a game concept document should look like) plus `UNITY_CONCEPT_USER` formatted with your prompt and the `theme_block`.

**What it outputs:** A free-text paragraph or two describing the game's story, mechanics, visual style, and feel. Temperature is `0.85` (creative).

**Where it goes:** Stored as the `concept` variable and passed to Step 3. Also written to `.concept.txt` in the project root.

If `show_prompts: true` is set in `config.yaml`, the full concept text is printed to the console.

---

### Step 3 — Schema Generation (LLM)

**What the LLM sees:** The `UNITY_SCHEMA_SYSTEM` prompt (instructions for filling a structured JSON form) plus `UNITY_SCHEMA_USER` formatted with the concept text and `schema_hints`.

**What it outputs:** A JSON object matching the `UnitySchema` structure. Temperature is `0.2` (precise, structured).

**`extract_json()`** strips markdown code fences (` ```json ` blocks) and performs minor repair before Pydantic validation. Pydantic then validates and auto-repairs the schema:

- Unknown game_type values → replaced with `3D`
- Scene list deduplicated and capped at 8
- Script list capped at 24
- Missing required fields filled with defaults
- Color palette auto-filled if not provided (`fill_default_palette` model validator)

**Required `UnitySchema` fields include:**

| Field | Type | Example |
|---|---|---|
| `project_name` | str | `"NeonFugitive"` |
| `company_name` | str | `"CoPlayDev"` |
| `game_type` | str | `"3D"`, `"2D"`, `"VR"`, `"top_down"` |
| `visual_style` | str | `"toon"`, `"realistic"`, `"pixel"` |
| `scenes` | list[str] | `["MainScene", "MenuScene"]` |
| `scripts` | list[UnityScript] | each has name + script_type |
| `packages` | list[UnityPackage] | each has name + optional package_id |
| `color_palette` | ColorPalette | 6 hex colors |
| `locomotion` | str | `"walk"`, `"drive"`, `"fly"`, `"swim"` |

---

### Step 4 — Package Resolution (no LLM)

**`UnityResolver.resolve_schema(schema)`** processes every entry in `schema.packages`:

For each package, resolution tries in order:

1. If `package_id` is already a valid known ID → accept directly
2. RAG search in the AnythingLLM Unity workspace (semantic similarity)
3. Local JSON lookup in `lookups/unity_packages.json` (exact → substring → fuzzy word-overlap)
4. Skip with a `WARNING` log — the package is omitted but does not block output

**`inject_required_packages(schema)`** then adds packages the schema didn't explicitly request but that are needed based on context:

| Condition | Packages injected |
|---|---|
| Always | TextMeshPro, Input System, Core RP Library |
| `game_type: VR` | XR Interaction Toolkit, XR Plugin Management |
| `game_type: 2D` | 2D Sprite, 2D Tilemap |
| Features include `cinemachine` | Cinemachine |
| Features include `navmesh` | AI Navigation |
| Features include `probuilder` | ProBuilder |
| Features include `shadergraph` | Shader Graph |
| `inject_mcp_bridge: true` | `com.coplaydev.unity-mcp` (git URL) |

**`build_manifest_deps(schema, inject_mcp_bridge)`** builds the final `manifest.json` dependency dict with three-tier fallback per package (declared version → re-resolve → SKIP with warning). Always injects all 27 `BUILTIN_MODULES` (cross-version-stable Unity engine modules).

> **Tech Tip:** `BUILTIN_MODULES` are Unity's built-in engine modules (physics, audio, animation, etc.) that must always be listed in `manifest.json` for a freshly generated project to compile without opening Unity Hub. The 27 modules in this set are specifically chosen for cross-version stability — they work identically in Unity 2021 through 6.x.

---

### Step 5 — Programmatic Fix + Consistency Check (no LLM)

**`_programmatic_fix(schema)`** applies mechanical corrections:

| Fix | Rule |
|---|---|
| `project_name` | Converted to `PascalCase` via `_to_pascal()` |
| `company_name` | Sanitized: non-alphanumeric/space characters removed |
| `scenes` | Deduplicated; capped at 8; each name `PascalCase` |
| `scripts` | Capped at 24; each `name` and `script_type` `PascalCase` |
| Script names | Canonical enforcement: `playercontroller` → `PlayerController`, etc. |

**`_check_consistency(schema)`** applies cross-field logic rules:

| If schema has... | Then fixer adds... |
|---|---|
| `game_type: VR` | `input_system` package |
| `game_type: 2D` | Removes any NavMesh/AI Navigation references |
| No `GameManager` in scripts | Adds `GameManager` script |
| No `PlayerController` in scripts | Adds `PlayerController` script |
| `enemies` or `collectibles` in features | Adds `HealthSystem` script |
| `cinemachine` in any script name | Adds Cinemachine package |
| `story_beats` in features | Adds `DialogueManager` script |
| `procedural_meshes` in features | Adds `MeshBuilder` script |
| `game_type: 3D` | Adds `MaterialPalette` script |
| `level_generator` in features | Adds ProBuilder package |

---

### Step 6 — Project Compilation (no LLM)

`UnityProjectGenerator.generate(schema, deps)` is a pure function — same schema always produces the same output. It returns a `dict[relative_path → str_content]` covering every file.

**How templates are rendered:**  
Each script type in `_SCRIPT_TEMPLATES` maps to a Jinja2 `.cs.j2` file. The template is rendered with the script's schema fields. The `extra_*` fields (`extra_usings`, `extra_fields`, `awake_extra`, `update_extra`, `fixed_update_extra`, `extra_methods`) — authored by the LLM in Step 3 — are injected at clearly marked locations in each template. `lint_extensible()` runs first to remove invalid C# characters.

**How GUIDs are generated:**  
```python
def _make_guid(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()
```
Every `.meta` file's GUID is the MD5 hash of `"<ProjectName>/<relative-asset-path>"`. This is stable: regenerating with the same project name and schema always produces the same GUIDs.

**How InputActions are built:**  
`_generate_input_actions()` builds a `.inputactions` JSON file with a Player action map and a UI action map. Control schemes are tailored to `game_type`:
- `2D` → Keyboard+Mouse + Gamepad schemes
- `3D` → Keyboard+Mouse + Gamepad
- `VR` → XR Controller scheme
- `mobile` → Touchscreen scheme

**How HUD assets are emitted:**  
When `ui_manager` is present in scripts and `game_type ≠ VR`, the generator emits:
- `Assets/HUD/GameHUD.uxml` — the `_HUD_UXML` constant (a UXML health/score layout)
- `Assets/HUD/PanelSettings.asset` — the `_HUD_PANELSETTINGS` constant

HUD GUIDs are fixed constants (not MD5-derived) because the HUD template is hardcoded.

**How procedural meshes work:**  
When `procedural_meshes` is in features, `_generate_obj()` writes a minimal quad-plane `.obj` placeholder for each mesh. These placeholders are loaded by `MeshBuilder.cs` at runtime and replaced with actual procedural geometry. The `.obj` just gives Unity something to import.

**How lighting and materials work:**  
- `_LIGHTING_BY_STYLE[visual_style]` → intensity, RGB color, and fog setting per scene
- `_TIME_OF_DAY_PRESETS[time_of_day]` → sun preset name
- `_sun_rotation(time_of_day)` → computed quaternion for directional light angle
- `_generate_material(name, hex_color, pipeline)` → `.mat` file content for URP, HDRP, or builtin render pipeline; render pipeline is auto-detected from the resolved package list

---

### Recompiling Without the LLM: `unity-validate`

```bash
python main.py unity-validate schema.json
```

Loads a saved schema JSON, runs Steps 4–6 (no LLM), and writes the project. Identical output to the original `unity-create` run.

**When to use it:**

- You edited the schema JSON manually and want to rebuild
- You changed a Jinja2 template and want to see the updated output
- CI/CD pipeline that regenerates from pinned schemas
- Sharing a schema with a teammate to regenerate locally

---

### Resolving a Single Package: `unity-resolve`

```bash
python main.py unity-resolve "universal render pipeline"
python main.py unity-resolve "cinemachine" 2022.3
```

Resolves one package description to its Unity Package Manager ID and version. Useful for debugging resolution or checking what a description maps to before running the full pipeline.

---

## 5. Running the GTA5 Pipeline

### Primary Command: `generate`

```bash
python main.py generate "A heist mission at the docks with a getaway boat"
```

**Full options:**

```bash
# Apply a theme
python main.py generate "A highway chase" --theme noir

# Blend two themes
python main.py generate "An assassination" --theme western --mashup horror

# Reproducible twist seed
python main.py generate "A race through downtown" --seed 42

# Override model
python main.py generate "A kidnapping mission" --model llama3-8b

# List themes
python main.py generate --theme list
```

---

### Step 1 — Theme + Archetype Setup (no LLM)

**Theme resolution** works the same as in the Unity pipeline (see Section 4, Step 1).

**Archetype detection** (GTA5 only): `detect_archetype(user_prompt)` scores the prompt against 6 archetypes:

| Archetype | Keywords scored | Injected content |
|---|---|---|
| `race` | race, lap, circuit, track, checkpoint | Race opponent vehicles, lap-structure steps |
| `heist` | heist, steal, rob, vault, getaway | Security peds, multi-stage structure |
| `dive` | dive, submarine, underwater, scuba | Aquatic location, swimming steps |
| `fly` | fly, aircraft, jet, helicopter, pilot | Air vehicles, altitude steps |
| `assault` | assault, attack, destroy, eliminate | Combat peds, destruction steps |
| `chase` | chase, escape, pursue, evade | Moving vehicle targets, timer steps |

Prompt keywords score 3×; concept keywords (after Step 2) score 1×. The highest-scoring archetype's `archetype_block` is injected into the Step 2 concept prompt, and its `structure_block` into the Step 3 schema prompt.

If the LLM returns a degenerate (too-short) schema, `apply_scaffold()` injects the archetype's canonical step skeleton.

---

### Step 2 — Concept Generation (LLM)

Same mechanism as Unity Step 2. The GTA5 concept prompt also includes `archetype_block` — the archetype's conceptual framing — which steers the LLM toward genre-appropriate storytelling.

`_check_location()` runs as part of this step: it cross-checks the concept text for location keywords (ocean, waterfront, desert, airport, etc.) against the schema's `location` field. A mismatch logs a warning but does not block generation.

---

### Step 3 — Schema Generation (LLM)

The LLM fills a `MissionSchema` JSON at temperature `0.2`. Pydantic validates and auto-repairs:

- Unknown `step_type` values → replaced with `wait`
- Ped/vehicle/object counts clamped to stable in-game ranges
- Missing step parameters filled with defaults
- Weather string validated against known values

**`MissionSchema` key fields:**

| Field | Type | Notes |
|---|---|---|
| `mission_name` | str | Used as C# class name |
| `location` | str | In-game location (checked vs concept) |
| `weather` | str | `CLEAR`, `RAIN`, `THUNDER`, etc. |
| `time_of_day` | str | `MORNING`, `NOON`, `NIGHT`, etc. |
| `peds` | list[MissionPed] | Characters (role, weapon, count, model) |
| `vehicles` | list[MissionVehicle] | Vehicles (role, model, count) |
| `objects` | list[MissionObject] | Props (model) |
| `audio` | list[MissionAudio] | Ambient audio names |
| `steps` | list[MissionStep] | Ordered mission step sequence |

---

### Step 4 — Model Resolution (no LLM)

`ModelResolver.resolve_schema(schema)` processes peds, vehicles, objects, and audio entries.

**Resolution order for each entry:**

1. RAG search in the AnythingLLM GTA5 workspace
2. Local JSON lookup: exact match → substring match → fuzzy word-overlap scoring
3. `"UNRESOLVED"` (handled in Step 5)

**Deduplication** (`_dedup_models()`):
- Two entries with the **same description** → same model (intentional reuse)
- Two entries with **different descriptions** that resolve to the same model → resolver tries to find an unused similar model for the second entry

---

### Step 5 — Fix + Consistency (no LLM … mostly)

**`_programmatic_fix(schema)`:**
- Weather string uppercased (`clear` → `CLEAR`)
- Ped/vehicle counts clamped to safe ranges
- Step parameter values clamped to valid ranges
- C# lint pass: removes invalid identifier characters, normalizes class names

**`_check_consistency(schema)`:**

| Rule | Fix |
|---|---|
| `PICKUP` step exists | Verifies pickup prop in objects list |
| `KILL_ALL` step exists | Verifies peds are present |
| `GET_IN_VEHICLE` step exists | Verifies a driveable vehicle exists |
| `RACE_LAP` or `CHECKPOINT` step | Verifies race opponent vehicle |
| `DELIVER` step exists | Verifies preceding `GO_TO` step |

**`_fix_unresolved()` — LLM fallback:** Any remaining `UNRESOLVED` model entries trigger a final LLM call. The LLM is given a list of known alternates and asked to pick the closest match. This is the **only** LLM call after Step 3.

---

### Step 6 — C# Compilation (no LLM)

`CodeGenerator` renders `templates/mission.cs.j2` with the fully resolved schema. Output: a single SHVDN3-compatible `.3.cs` file at `output/<MissionName>.3.cs`.

---

### Additional GTA5 Commands

```bash
# Recompile a saved schema without re-running the LLM
python main.py validate schema.json

# Resolve a single asset description
python main.py resolve "cult member" peds
python main.py resolve "sports car" vehicles
python main.py resolve "crate" objects

# RAG setup and status
python main.py setup-rag
python main.py setup-workspace
python main.py rag-status
```

---

### Installing the GTA5 Output

1. Copy `output/<MissionName>.3.cs` to your GTA5 `scripts/` folder
2. Ensure [ScriptHookVDotNet3](https://github.com/scripthookvdotnet/scripthookvdotnet) is installed
3. Launch GTA5 — the script loads automatically

---

## 6. Micro-Generators

### What Is a Micro-Generator?

A micro-generator is a self-contained module that generates **one game subsystem** — complete with C# scripts, configuration assets, and Jinja2 templates. Each micro is:

- **Deterministic** — no LLM; pure template expansion
- **Standalone** — can be run independently via `main.py unity-<kind>`
- **Composable** — can be assembled by `MicroComposer` in `--compose` mode

Micros live in `src/micro/<kind>/`. Each defines a pipeline class, a schema class, and template files.

**To list all available micros and their variants:**

```bash
python main.py unity-tools list
```

---

### `CodeExtensibleSchema` — Custom Code Hooks

Every micro schema inherits from `CodeExtensibleSchema`. This provides hook fields where the LLM (in the full pipeline) or you (manually) can inject C# code fragments:

| Field | Injected into |
|---|---|
| `extra_usings` | Additional `using` statements at file top |
| `extra_fields` | Extra field declarations in the class body |
| `awake_extra` | Code appended inside `Awake()` |
| `update_extra` | Code appended inside `Update()` |
| `fixed_update_extra` | Code appended inside `FixedUpdate()` |
| `extra_methods` | Additional methods at end of class |

`lint_extensible()` runs before output to strip invalid C# characters from these fields.

---

### How Composed Units Are Injected Into Scenes

In `--compose` mode, `MicroComposer.compose()`:

1. Calls `plan()` to compute `ComposedUnit` objects — each has a deterministic GUID, fileID, class name, and target attachment point
2. Calls each micro's `write_files()` to generate scripts into `Assets/Micros/<kind>/`
3. Writes deterministic `.cs.meta` files for each script (GUID = MD5 of `"<ProjectName>/Micros/<kind>/<ClassName>"`)
4. Emits `Assets/Micros/wiring/Scripts/GameWiring.cs` — a glue MonoBehaviour that cross-wires all subsystems via Unity events

Script types in `COVERED_SCRIPT_TYPES` (player, camera, enemy, audio_manager, ui_manager, collectible, dialogue_manager, health) are dropped from the main project's script list in compose mode, since the composer becomes the single source of truth for them.

`GameWiring.cs` is special: it is generated from `templates/unity/compose/GameWiring.cs.j2` and references every composed unit's class name. If a `health` + `hud` combo with `hud.variant == "health_bar"` is present, it also wires `HUD.SetHealth(float, float)` to the health system's damage event.

---

### Subsystem Selection in `--compose` Mode

`subsystems_for(game_type, features, locomotion)` maps project properties to a default subsystem set — no LLM:

**Always included:**

| Micro | Variant | Target |
|---|---|---|
| `controller` | game_type-dependent (see below) | player |
| `camera` | game_type-dependent | camera |
| `health` | `simple` | player |
| `hud` | `health_bar` | ui |
| `menu` | `pause_menu` | ui |
| `saveload` | `json_file` | systems |

**Controller/camera variant by game type:**

| `game_type` | Controller variant | Camera variant |
|---|---|---|
| `2D`, `2.5D` | `2d_platformer` | `follow` |
| `top_down`, `isometric` | `top_down` | `cinemachine_topdown` |
| `VR` | `first_person` | `follow` |
| `3D` (default) | `third_person` | `follow` |

**Locomotion overrides (3D/top_down/isometric only):**

| `locomotion` | Controller variant | Camera variant |
|---|---|---|
| `drive` | `vehicle_car` | `vehicle_chase` |
| `fly` | `flying` | `follow` |
| `swim` | `swimming` | `follow` |

**Feature-driven additions:**

| Feature | Micro added |
|---|---|
| `ai`, `navmesh`, `enemies` | `ai` (navmesh_patrol or waypoint_follow for drive) + `spawner` (wave_spawner) |
| `audio` | `audio` (categorical_mixer) |
| `inventory` | `inventory` (slot_based) |

---

### The 15 Micro-Generators

#### 1. `controller` — Character / Vehicle Movement

```bash
python main.py unity-controller "A slippery ice-skating character"
python main.py unity-controller "A racing car with drift" --variant vehicle_car
```

| Variant | Description |
|---|---|
| `third_person` | Rigidbody 3D character, mouse look, WASD |
| `first_person` | First-person controller with head bob |
| `2d_platformer` | Side-scrolling jump and run |
| `2d_topdown` | Top-down 2D movement |
| `top_down` | Top-down 3D movement |
| `vehicle_car` | WheelCollider car with acceleration/braking/steering |
| `vehicle_bike` | WheelCollider motorbike with lean |
| `flying` | 6DOF flying controller (pitch/roll/yaw/thrust) |
| `swimming` | Buoyancy-based underwater movement |

---

#### 2. `camera` — Camera Behavior

```bash
python main.py unity-camera "A cinematic orbit camera for a strategy game"
python main.py unity-camera "Chase camera for vehicle game" --variant vehicle_chase
```

| Variant | Description |
|---|---|
| `follow` | Spring-arm follow camera (3D default) |
| `orbit` | Mouse-drag orbital camera |
| `first_person` | Camera locked to player head |
| `cinemachine_3p` | Cinemachine third-person FreeLook rig |
| `cinemachine_topdown` | Cinemachine top-down orthographic |
| `rts_pan_zoom` | RTS-style pan, zoom, and edge-scroll |
| `vehicle_chase` | Chase camera for vehicle games |
| `cinematic_dolly` | Timeline-triggered dolly/cutscene camera |

---

#### 3. `physics` — Physics Behavior

```bash
python main.py unity-physics "A breakable crate that shatters on impact"
python main.py unity-physics "Buoyant debris" --variant buoyancy
```

| Variant | Description |
|---|---|
| `rigidbody_2d` | 2D Rigidbody with gravity and drag |
| `rigidbody_3d` | 3D Rigidbody with gravity and drag |
| `kinematic_character` | Kinematic body for custom movement |
| `ragdoll` | Enable/disable ragdoll on death |
| `vehicle_wheels` | Multi-axle WheelCollider setup |
| `soft_body_stub` | Spring-mesh soft-body approximation |
| `trigger_zone` | Trigger collider with enter/exit events |
| `destructible_object` | Object fractures on impact |
| `buoyancy` | Float/sink in water via trigger volume |

---

#### 4. `hud` — Heads-Up Display

```bash
python main.py unity-hud "A minimalist ammo counter"
python main.py unity-hud "Full HUD with health, ammo, and minimap" --variant full_hud_bundle
```

| Variant | Description |
|---|---|
| `health_bar` | Horizontal fill-bar health display |
| `ammo_counter` | Ammo count with reload indicator |
| `minimap` | Render-texture minimap |
| `crosshair` | Dynamic crosshair with spread |
| `interaction_prompt` | Context-sensitive "Press E" prompt |
| `score_panel` | Score and combo multiplier display |
| `pause_menu` | Basic pause overlay |
| `dialogue_box` | NPC speech panel with portrait |
| `full_hud_bundle` | Combines health + ammo + crosshair in one |

---

#### 5. `input` — Input Handling

```bash
python main.py unity-input "Keyboard and mouse for a first-person game"
python main.py unity-input "Dual stick mobile controls" --variant dual_stick
```

| Variant | Description |
|---|---|
| `keyboard_mouse` | Unity Input System KB+Mouse action map |
| `gamepad` | Gamepad bindings with rumble support |
| `touch` | Touch gesture recognition (tap, swipe, pinch) |
| `dual_stick` | Mobile dual-stick virtual joystick |
| `racing_wheel` | Steering wheel + pedals bindings |
| `vr_xr` | XR hand tracking and controller input |

---

#### 6. `audio` — Audio Management

```bash
python main.py unity-audio "Background music with dynamic intensity layers"
python main.py unity-audio "Spatial 3D sound effects" --variant 3d_spatial
```

| Variant | Description |
|---|---|
| `2d_simple` | Single AudioSource, play-on-trigger |
| `3d_spatial` | 3D positioned audio with falloff curves |
| `music_layers` | Layered music stems that blend dynamically |
| `crossfade_zones` | Trigger-based ambient soundscape switcher |
| `categorical_mixer` | AudioMixer with SFX/Music/Voice buses |

---

#### 7. `ai` — Enemy / NPC Intelligence

```bash
python main.py unity-ai "A guard that patrols and chases the player"
python main.py unity-ai "A flock of birds" --variant flocking
```

| Variant | Description |
|---|---|
| `navmesh_patrol` | NavMeshAgent patrol between waypoints |
| `chase_player` | NavMeshAgent pursuit of player |
| `fsm_guard` | FSM with Idle/Patrol/Chase/Attack/Alert states |
| `behaviour_tree_lite` | Lightweight Selector/Sequence behaviour tree |
| `flocking` | Boid flocking simulation |
| `cover_shooter` | Cover-to-cover advance and fire |
| `flying_swarm` | Aerial swarm with formation rules |

---

#### 8. `spawner` — Object / Entity Spawning

```bash
python main.py unity-spawner "Wave-based enemy spawner"
python main.py unity-spawner "Object pooler for bullets" --variant object_pool
```

| Variant | Description |
|---|---|
| `wave_spawner` | Round-based enemy waves with delay |
| `continuous_spawner` | Constant rate spawning with max cap |
| `object_pool` | Pre-instantiated object pool for performance |
| `trigger_spawner` | Spawn on trigger enter |
| `point_spawner` | Spawn at explicit transform points |
| `radial_spawner` | Random scatter within radius |

---

#### 9. `saveload` — Save / Load System

```bash
python main.py unity-saveload "JSON save with multiple slots"
python main.py unity-saveload "Auto-save every 30 seconds" --variant autosave
```

| Variant | Description |
|---|---|
| `json_file` | JSON serialization to `persistentDataPath` |
| `playerprefs_kv` | Unity PlayerPrefs key-value store |
| `save_slots` | Multiple named save slots |
| `checkpoint` | Scene checkpoint save on trigger |
| `autosave` | Periodic auto-save on timer |
| `encrypted_json` | JSON with AES encryption |

---

#### 10. `inventory` — Inventory System

```bash
python main.py unity-inventory "A grid-based item inventory with tooltips"
python main.py unity-inventory "Equipment slots RPG style" --variant equipment
```

| Variant | Description |
|---|---|
| `slot_based` | Fixed slot grid |
| `grid` | 2D grid with item sizes |
| `weight_based` | Carry weight limit |
| `equipment` | Head/body/weapon/accessory slots |
| `hotbar` | Quick-access hotbar with keybinds |
| `unlimited` | Simple flat list with no cap |

---

#### 11. `dialogue` — Dialogue System

```bash
python main.py unity-dialogue "Branching dialogue tree with player choices"
python main.py unity-dialogue "Barks: random one-liners" --variant barks
```

| Variant | Description |
|---|---|
| `linear` | Sequential lines, no choices |
| `branching` | Player choice tree |
| `typewriter` | Character-by-character reveal |
| `cutscene` | Timeline-synced dialogue |
| `barks` | Random one-liner ambient speech |
| `localized` | Multi-language string table support |

---

#### 12. `menu` — Menus

```bash
python main.py unity-menu "A main menu with animated background"
python main.py unity-menu "Settings screen with volume sliders" --variant settings_menu
```

| Variant | Description |
|---|---|
| `main_menu` | Title screen with Play/Settings/Quit |
| `pause_menu` | In-game pause overlay |
| `settings_menu` | Graphics/audio/controls settings |
| `game_over` | Game over + retry screen |
| `level_select` | Level selection grid |
| `confirm_dialog` | Generic "Are you sure?" modal |

---

#### 13. `interactable` — Interactive Objects

```bash
python main.py unity-interactable "A sliding door that opens with a keycard"
python main.py unity-interactable "A readable book with page content" --variant readable
```

| Variant | Description |
|---|---|
| `pickup` | Player walks over → collected |
| `door` | Open/close/lock door with animation |
| `lever` | Toggle switch triggering an event |
| `button_switch` | Momentary button with press event |
| `npc_talk` | Enters dialogue when player approaches |
| `readable` | Displays readable text on interact |

---

#### 14. `transition` — Scene / Level Transitions

```bash
python main.py unity-transition "Fade to black between levels"
python main.py unity-transition "Async loading with progress bar" --variant loading_bar
```

| Variant | Description |
|---|---|
| `instant` | Immediate `SceneManager.LoadScene()` |
| `fade` | Fade to/from black |
| `loading_bar` | Async load with progress bar UI |
| `async_additive` | Additive async scene streaming |
| `trigger_door` | Walk through door → load next scene |

---

#### 15. `health` — Health & Damage

```bash
python main.py unity-health "Player health with shield that recharges"
python main.py unity-health "Three lives system" --variant lives
```

| Variant | Description |
|---|---|
| `simple` | HP integer, `TakeDamage(int)`, `Die()` |
| `regenerating` | HP regenerates after delay |
| `shielded` | Shield absorbs damage before HP |
| `armor` | Armor reduces incoming damage percentage |
| `lives` | Death → lose a life → respawn |

---

### Adding a New Micro-Generator

1. Create `src/micro/<kind>/` directory
2. Write a schema class inheriting `CodeExtensibleSchema`
3. Write a pipeline class inheriting `MicroPipelineBase`; implement `default_fix()`, `write_files()`, `attach_class()`, `display_name`, `variants`
4. Register in `src/micro/__init__.py` REGISTRY: `"<kind>": MyPipelineCls`
5. Add a `@app.command("unity-<kind>")` entry in `main.py` following the existing pattern
6. If the micro needs Unity modules, add its kind to `MicroComposer._KIND_MODULES`

---

## 7. Configuration & Customization

### `config.yaml` — Full Reference

```yaml
# ── LLM Backend ─────────────────────────────────────────────────────────
lm_studio:
  backend: lm_studio        # "lm_studio" or "ollama"
  base_url: http://localhost:1234/v1   # LM Studio server URL
  model: qwen3.5-9b         # Model loaded in LM Studio
  temperature: 0.7          # Default generation temperature
  max_tokens: 8192          # Max tokens per LLM response
  timeout: 600              # Request timeout in seconds
  concept_temperature: 0.85 # Step 2 temperature (more creative)
  schema_temperature: 0.2   # Step 3 temperature (more precise/structured)

# ── Ollama (used when backend = ollama) ──────────────────────────────────
ollama:
  base_url: http://localhost:11434/v1
  model: qwen2.5:7b

# ── AnythingLLM / RAG ────────────────────────────────────────────────────
anythingllm:
  base_url: http://localhost:3001
  api_key: YOUR_API_KEY
  workspace_slug: llm_gta             # GTA5 workspace slug
  unity_workspace_slug: unity         # Unity workspace slug
  fallback_to_local: true             # Never hard-fail; degrade gracefully

# ── Global mode ──────────────────────────────────────────────────────────
basic_mode: false             # true = local lookups only, skip RAG entirely

# ── GTA5 pipeline paths ──────────────────────────────────────────────────
pipeline:
  output_dir: output
  lookups_dir: lookups
  rag_docs_dir: rag_docs
  template_file: templates/mission.cs.j2

# ── Unity pipeline paths ─────────────────────────────────────────────────
unity_pipeline:
  lookups_dir: lookups
  rag_docs_dir: rag_docs/unity
  unity_template_dir: templates/unity
  unity_output_dir: output/unity
  inject_mcp_bridge: true   # Add com.coplaydev.unity-mcp to manifest.json

# ── Logging ──────────────────────────────────────────────────────────────
logging:
  level: INFO               # DEBUG | INFO | WARNING | ERROR
  show_prompts: false       # true = print full LLM prompts to console
```

---

### Lookup Tables

Lookup tables are JSON files in `lookups/` used by resolvers when RAG is unavailable.

| File | Used by | Contents |
|---|---|---|
| `lookups/peds.json` | GTA5 ModelResolver | Ped model hash strings |
| `lookups/vehicles.json` | GTA5 ModelResolver | Vehicle model hash strings |
| `lookups/objects.json` | GTA5 ModelResolver | Prop/object hash strings |
| `lookups/weapons.json` | GTA5 ModelResolver | Weapon hash strings |
| `lookups/weather.json` | GTA5 ModelResolver | Weather state names |
| `lookups/audio.json` | GTA5 ModelResolver | Ambient audio names |
| `lookups/unity_packages.json` | UnityResolver | Package ID → version |

**To extend:** Add entries in the same JSON format as existing entries. Resolution tries exact match first, then substring, then fuzzy word-overlap scoring — so descriptive keys work better than terse ones.

---

### Themes

Themes are YAML files in `themes/` that layer aesthetic vocabulary onto the concept and schema prompts.

**Theme fields:**

| Field | Effect on pipeline |
|---|---|
| `concept_bias` | Text injected before concept prompt (biases Step 2) |
| `vocabulary` | Preferred words injected into schema prompt (Step 3) |
| `step_hints` | Suggested mission step structures |
| `twist_seeds` | Surprising elements to introduce |
| `anti_tropes` | Common clichés to explicitly avoid |

**List available themes:**

```bash
python main.py generate --theme list
python main.py unity-create --theme list
```

**Mashup two themes:**

```bash
python main.py unity-create "A heist" --theme cyberpunk --mashup western
```

The two themes' `concept_bias`, `vocabulary`, and `step_hints` fields are merged. The result is a blended aesthetic.

---

### Jinja2 Templates

**GTA5:** `templates/mission.cs.j2` — renders the complete SHVDN3 C# class from `MissionSchema`.

**Unity:** `templates/unity/` — per-script-type templates:

| Template | Generates |
|---|---|
| `game_manager.cs.j2` | `GameManager.cs` |
| `player_controller.cs.j2` | `PlayerController.cs` |
| `camera_follow.cs.j2` | `CameraFollow.cs` |
| `enemy.cs.j2` | `Enemy.cs` |
| `health_system.cs.j2` | `HealthSystem.cs` |
| `dialogue_manager.cs.j2` | `DialogueManager.cs` |
| `entity_spawner.cs.j2` | `EntitySpawner.cs` |
| `level_generator.cs.j2` | `LevelGenerator.cs` |
| `mesh_builder.cs.j2` | `MeshBuilder.cs` |
| `material_palette.cs.j2` | `MaterialPalette.cs` |
| `scene.unity.j2` | Unity scene YAML |
| `compose/GameWiring.cs.j2` | Compose-mode glue script |

To change generated code, edit the template. Template variables are the schema field names. Do not change the Python source.

---

### Extending the Schema Safely

**To add a new field to `UnitySchema`:**

1. Add the field with a default value to `UnitySchema` in `src/unity_schema.py`
2. Add a Pydantic `@field_validator(mode='before')` to handle invalid LLM output
3. Reference the new field in the relevant `.cs.j2` template
4. Add handling in `_programmatic_fix()` or `_check_consistency()` if the field affects other fields

> **Tech Tip — Pydantic v2 validators:**  
> All validators use `mode='before'`, meaning they run on the raw LLM string before type coercion. A schema field that expects `int` and receives `"3"` is silently converted. A schema field that receives `null` gets its default. The pipeline never raises a `ValidationError` on schema generation — it always produces a repaired schema.

> **Tech Tip — Deterministic GUID guarantees:**  
> `_make_guid(seed)` = `MD5(seed.encode()).hexdigest()`. As long as `project_name` and asset paths don't change, GUIDs are identical across all regenerations — on any machine, at any time. This means Unity scene files that reference scripts by GUID will always find their scripts, even after regenerating the scripts from an updated template.

> **Tech Tip — Entity behavior reconciliation:**  
> `EntitySpec` objects in the schema describe scene content groups (e.g., "6 rival cars in a ring"). `EntitySpawner.cs` reads these at runtime and instantiates them. The spec fields `placement` (single/line/ring/grid/scatter/path), `count`, `label`, and `prefab_hint` give the spawner enough information to recreate the scene without any Unity prefab being present at generation time. Procedural placement decouples scene design from asset creation.

---

## 8. Troubleshooting

### LLM Connection Issues

**`ConnectionRefusedError` or timeout during Step 2 or 3**  
Cause: LM Studio or Ollama is not running.  
Fix: Start the LM Studio server (default port 1234) or run `ollama serve`. Verify `config.yaml` `base_url` matches the running server.

**Model not found (404 from LM Studio)**  
Cause: The model name in `config.yaml` doesn't match the model loaded in LM Studio.  
Fix: Check the LM Studio UI for the exact loaded model name, or use `--model <name>` to override for this run.

**Schema generation times out**  
Cause: Large schemas with many scripts can take several minutes on a slow GPU.  
Fix: Increase `lm_studio.timeout` in `config.yaml` (default: 600 seconds). Consider reducing `max_tokens` or using a faster model.

**Schema is empty or missing most fields**  
Cause: The LLM returned a truncated response.  
Fix: Enable `show_prompts: true` and `logging.level: DEBUG` to inspect the raw response. Try increasing `max_tokens` or switching to a model with a larger context window.

---

### RAG / AnythingLLM Issues

**`rag-status` shows 0 documents**  
Fix: Run `python main.py setup-workspace` to bootstrap the workspace and upload documents. Or manually upload your reference files in the AnythingLLM UI.

**RAG returns hallucinated / wrong model names**  
Cause: Reference documents in the workspace don't contain the relevant entries.  
Fix: Add authoritative reference documents to your workspace. Or switch to `basic_mode: true` and extend the local lookup tables.

**AnythingLLM workspace not found**  
Fix: Run `python main.py setup-workspace` which creates the workspace automatically. Or create it manually in the AnythingLLM UI and verify `workspace_slug` in `config.yaml` matches exactly.

---

### Unresolved Assets

**GTA5: Script contains `UNRESOLVED` strings**  
Cause: Neither RAG nor local lookup found a matching model.  
Fix: Check `python main.py resolve "<description>" peds` to debug resolution for a specific entry. Add the correct model hash to the appropriate lookup JSON. If `_fix_unresolved()` also failed, it means the LLM fallback didn't find an alternative — add the entry to the lookup table.

**Unity: `WARNING: skipping unresolved package`**  
Cause: A package description couldn't be matched to a Unity Package Manager ID.  
Fix: Run `python main.py unity-resolve "<description>"` to test resolution. Add the correct `"package_id": "com.unity.xxx"` to `lookups/unity_packages.json`.

---

### Unity Project Issues

**Compile errors when opening in Unity**  
Cause: Generated C# has a syntax error.  
Fix: Check `.compile_map.json` — it maps each `.cs` to its source `.j2` template. If `inject_mcp_bridge: true`, the MCP bridge loads independently of compile errors and can be used to diagnose from the AI interface. Enable `logging.level: DEBUG` to see the exact schema that fed the template.

**Scene is empty when pressing Play**  
Cause: The `GameManager` or `PlayerController` scripts were not injected into the scene YAML.  
Fix: Check that `_check_consistency()` ran (Step 5 logs should show it). If the project was generated from a schema missing these scripts, add them and rerun `unity-validate`.

**`.meta` GUIDs change between regenerations**  
Cause: `project_name` changed between runs (the GUID seed includes the project name).  
Fix: Use a fixed schema file with `unity-validate` to guarantee identical output. Ensure `project_name` is stable in your prompt or schema.

**HUD is missing**  
Cause: `game_type: VR` (HUD is intentionally omitted for VR) or no `ui_manager` script in the schema.  
Fix: For non-VR projects, add `ui_manager` to the script list in the schema and rerun `unity-validate`.

**Packages not installing in Unity Package Manager**  
Cause: Package ID or version in `manifest.json` is wrong.  
Fix: Run `python main.py unity-resolve "<package name>"` to check the resolved ID. Add the correct entry to `lookups/unity_packages.json` for future runs.

---

### GTA5 Mission Issues

**Mission script fails to compile in SHVDN3**  
Cause: C# lint may not have caught all edge cases.  
Fix: Open the `.3.cs` file and search for `UNRESOLVED` or any non-ASCII characters. Enable `--debug` on the next run to inspect the raw LLM output and schema.

**Characters / vehicles don't spawn in-game**  
Cause: Model hash strings are wrong or `UNRESOLVED`.  
Fix: Search the generated script for `UNRESOLVED`. Run `python main.py resolve "<description>" peds` to test resolution. Add correct hashes to the lookup tables.

---

### Schema Validation Issues

**Pydantic `ValidationError` raised during Step 3**  
Cause: The LLM returned entirely non-JSON content (e.g., a plain-text refusal).  
Fix: Enable `show_prompts: true` to see exactly what the LLM returned. Try a different model or increase `max_tokens`. Check that the model is instruction-tuned (base models don't follow JSON instructions reliably).

**Script list shorter than expected**  
Cause: `_programmatic_fix()` caps scripts at 24.  
Fix: To raise the cap, edit the cap value in `_programmatic_fix()` in `src/unity_pipeline.py`.

**Micro variant mismatch**  
Cause: A variant name passed with `--variant` doesn't match any defined variant.  
Fix: Run `python main.py unity-tools list` to see exact variant names. Variant names are lowercase with underscores.

---

## 9. Glossary

### Archetype
A named genre pattern for GTA5 missions (race, heist, dive, fly, assault, chase). Detected from prompt keywords in Step 1. Injects canonical step skeletons when the LLM produces a weak schema.

> **Tech Tip:** Detection uses keyword scoring: prompt matches score 3×, concept matches score 1×. The highest-scoring archetype wins. `apply_scaffold()` is only called when the LLM schema is degenerate (step count too low).

---

### Basic Mode
A pipeline mode (`basic_mode: true`) that skips AnythingLLM entirely. Asset resolution uses only local JSON lookup tables. Useful when AnythingLLM is not running.

---

### Compile Map (`.compile_map.json`)
A JSON file written to the Unity project root that maps every generated `.cs` file to the Jinja2 `.j2` template that produced it. Used for error attribution.

---

### Concept
The free-text design document generated by the LLM in Step 2. Describes the game/mission story, mechanics, and feel. Stored as `.concept.txt` in the project root. Also fed as context into the Step 3 schema prompt.

---

### Consistency Checker
The Step 5 function (`_check_consistency()`) that applies cross-field logic rules to the schema. It catches things the LLM missed — like a 2D game that accidentally has NavMesh, or an enemy feature without a HealthSystem script.

---

### Deterministic
A step that always produces the same output from the same input, with no randomness. Steps 4–6 are deterministic. The `unity-validate` and `validate` commands exploit this to regenerate without re-running the LLM.

---

### Entity Spec
A schema-level vocabulary object (`EntitySpec`) describing a group of scene entities with placement rules. Fields: `label`, `count`, `placement` (single/line/ring/grid/scatter/path), `prefab_hint`. Consumed by `EntitySpawner.cs` at runtime.

---

### Fixer (`_programmatic_fix`)
The Step 5 function that applies mechanical corrections to the schema: PascalCase conversion, name sanitization, list deduplication, count clamping, and C# lint.

---

### GUID (Unity)
A 32-hex-character unique identifier stored in every `.meta` file. Unity uses GUIDs to link scene files to scripts. In this system, GUIDs are computed as MD5 hashes of `"<ProjectName>/<relative-path>"` — making them deterministic and stable across regenerations.

> **Tech Tip:** If a GUID changes, Unity loses the script reference in the scene file and displays a "Missing Script" error. Deterministic GUIDs prevent this entirely.

---

### InputActions
A Unity Input System asset (`.inputactions` file) that defines action maps and control schemes. Generated by `_generate_input_actions()` in Step 6. Tailored to the project's `game_type` — 2D, 3D, VR, and mobile each get appropriate control schemes.

---

### Jinja2
The Python templating language used to generate all code files. Templates (`.j2` files) use `{{ variable }}` syntax. Step 6 renders templates with schema field values.

---

### LLM (Large Language Model)
An AI model that generates text. This system uses locally-running LLMs via LM Studio or Ollama. LLMs are called only in Steps 2 and 3. No cloud or internet connection is needed.

---

### Manifest (`manifest.json`)
The Unity Package Manager dependency file. Built in Step 4 by `build_manifest_deps()`. Contains all resolved package IDs + versions plus the 27 `BUILTIN_MODULES`. Optionally includes the MCP bridge git-URL dependency.

---

### MCP Bridge
A Unity Editor plugin (`com.coplaydev.unity-mcp`) optionally injected into `manifest.json` via git URL. Loads independently of script compile errors, allowing an AI agent to read the Unity console and fix errors. Enabled by `inject_mcp_bridge: true`.

---

### Micro-Generator
A self-contained module that generates one game subsystem (controller, HUD, audio, etc.) using no LLM — pure template expansion. 15 micro kinds are available. Each has multiple variants.

---

### MicroComposer
The `--compose` mode assembler. Calls each micro's `write_files()`, assigns deterministic GUIDs, and emits `GameWiring.cs` to wire all subsystems together. Located in `src/unity_compose.py`.

---

### ModelResolver
The GTA5 asset lookup system (`src/resolver.py`). Maps plain-English descriptions to GTA5 model hash strings via RAG → local lookup → LLM fallback. Also handles deduplication.

---

### Palette
A set of six hex-color values (`ColorPalette` in the schema): primary, secondary, accent, background, trim, and a name. Used by `_generate_material()` to create `.mat` files for all game objects.

---

### Pipeline
The 6-step process that transforms a plain-English prompt into output files. Steps 1–3 handle creative generation; Steps 4–6 are deterministic compilation.

---

### Procedural Mesh
A `.obj` file generated as a quad-plane placeholder in Step 6 by `_generate_obj()`. The placeholder is imported by Unity and replaced with actual generated geometry at runtime by `MeshBuilder.cs`.

---

### Programmatic Fixer
See *Fixer*.

---

### RAG (Retrieval-Augmented Generation)
A technique where relevant documents are retrieved from a knowledge base and included in an LLM prompt. This system uses AnythingLLM as the RAG server. Used in Step 4 to find accurate asset/package IDs from uploaded reference documents.

---

### Resolver
The Step 4 asset-matching system. Unity uses `UnityResolver` (packages); GTA5 uses `ModelResolver` (peds/vehicles/props/audio). Both follow the same pattern: RAG → local lookup → fallback.

---

### Schema
The structured JSON form that describes a complete project (Unity) or mission (GTA5). Generated by the LLM in Step 3, validated by Pydantic, repaired in Steps 4–5, and compiled in Step 6. The schema is the single source of truth for all generated output.

---

### SHVDN3
ScriptHookVDotNet version 3 — the GTA5 modding framework that loads `.3.cs` scripts. The GTA5 pipeline generates files compatible with SHVDN3.

---

### Theme
A YAML-backed aesthetic layer applied to concept and schema prompts. Themes inject vocabulary, structural hints, twist seeds, and anti-trope rules to push the output toward a particular genre aesthetic (cyberpunk, western, horror, etc.).

---

### UnityProjectGenerator
The Step 6 compiler for Unity (`src/unity_generator.py`). A pure function: takes a resolved schema + package deps, returns a `dict[relative_path → file_content]` covering every file in the project. Never calls the LLM.

---

### UnityResolver
The Unity package resolution system (`src/unity_resolver.py`). Maps package names to `com.unity.*` IDs and versions via RAG → local lookup. Also injects required packages based on game type, features, and locomotion.

---

### Visual Style
A schema field that affects lighting, materials, and color generation. Values include `toon`, `realistic`, `pixel`, `low_poly`, `painterly`. Mapped by `_LIGHTING_BY_STYLE` to light intensity, RGB color, and fog density settings.

---

*End of User Manual — Bacon & Eggs v1.0*
