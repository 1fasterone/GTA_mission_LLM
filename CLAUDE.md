# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **domain-agnostic, multi-backend compiler for procedural game content**. A local LLM is the
*creative front-end*; the rest is a deterministic Python compiler. Two domains ship as proof-of-concept
backends: **GTA5** (mission C# scripts) and **Unity** (whole project folders that run on Play).

The mental model is a **compiler, not a prompt chain**: LLM = semantic front-end, the Pydantic schema =
the IR/contract, resolver = type-checker+linker, fixer = optimizer, generator = backend codegen.

## The 6-step pipeline (the core abstraction)

Both domains share one flow. Steps 2–3 are the *only* LLM steps; **steps 4–6 are pure Python,
deterministic, and must stay LLM-free and reproducible**:

1. (prompt in)
2. **Concept** — LLM writes a plain-English brief/story
3. **Schema** — LLM fills a strict Pydantic schema (the IR; once valid, the LLM is done)
4. **Resolver** — maps descriptions → concrete IDs (GTA model hashes / Unity package IDs / model files), RAG-first with local-lookup fallback
5. **Fixer** — naming, injects required components, cross-field consistency, auto-corrects LLM mistakes
6. **Generator** — compiles the schema → project artifacts (no LLM)

The same schema can be recompiled instantly without the LLM (`validate` / `unity-validate`).

A domain = **schema extension + resolver table + generator backend**. Adding capability means
plugging in at those three points, not changing the core.

## Where things live

| Concern | Files |
|---|---|
| CLI entry (all commands) | `main.py` (Typer) |
| GTA5 pipeline | `src/pipeline.py`, `src/mission_schema.py`, `src/code_generator.py`, `src/resolver.py` |
| Unity pipeline | `src/unity_pipeline.py`, `src/unity_schema.py`, `src/unity_generator.py`, `src/unity_resolver.py`, `src/unity_prompts.py`, `templates/unity/` |
| LLM backends | `src/backends.py` (`make_chat_client` — LM Studio **or** Ollama), `src/lm_studio_client.py`, `src/anythingllm_client.py` (RAG, optional) |
| Theming | `src/themes.py` |
| Micro-generators | `src/micro_pipeline.py` (6-step base), `src/micro/` (`REGISTRY`), `templates/unity/micro/` |
| Composition (micros → scene) | `src/unity_compose.py` (`unity-create --compose`) |
| Blender asset factory | `blender_cli.py`, `src/blender/` (`job_manager`, `mcp_backend`, `locator`, `manifest`, `unity_overlay`, `recipes`, `rigging`, `diffusion`) |
| Web UI | `webui/server.py` (FastAPI), `python main.py serve` |

**Everything is invoked through `main.py`** (and surfaced in the web UI). New capabilities should be
added as a `main.py` command, not a standalone script.

### Model Pack v1 — how real models replace primitive placeholders

Generated Unity scenes spawn primitives (capsule player, cubes) **as a fallback**. Real models are
swapped in at runtime by this chain — understand it before touching model/asset wiring:

- `lookups/unity/default_models.json` — catalog: `kind` → `{model, rig, scale, clips}`
- `src/unity_model_resolver.py` — resolves catalog entries against `Models/Defaults/<kind>/` (**missing file → stays a primitive**)
- `src/unity_generator.py` — copies them to `Assets/Resources/Models/Default/<Kind>/`, writes ModelImporter `.meta` (Humanoid rig for player/enemy)
- `templates/unity/ModelSwap.cs.j2` + `EntitySpawner.cs.j2` — at Play, hide the primitive, `Resources.Load` the model, attach an Animator
- The scene/entity wiring keys off `entity_model_paths` (entity name → `resource_path`). `Assets/BlenderAssets/` is **not** consumed by the scene.

### Procedural 3D models (Blender → FBX)

- `vehicle_generator.py` (repo root) — SpaceshipGenerator-style bmesh builders: `generate_vehicle`/`rig_vehicle` (car) and `generate_plane`/`rig_plane` (plane); `build_detailed_code()` emits a self-contained bpy script (optionally with FBX export).
- `procedural_pipeline.py` (repo root) — prompt → `{base, params}` mapping (LLM + keyword fallback).
- `src/micro/model.py` — `ModelPipeline` (`unity-model` command): maps prompt/knobs → params, builds headless via `src/blender/locator`, exports a Unity-friendly FBX, overlays into a Unity project.

## Common commands

```bash
# Install
pip install -r requirements.txt          # Python 3.11+ ; or: powershell -File scripts/install.ps1

# Web UI (all pipelines)
python main.py serve                      # http://127.0.0.1:8000

# GTA5
python main.py generate "Heist at the docks"
python main.py validate path/to.schema.json        # recompile, no LLM
python main.py resolve "cult member" peds

# Unity
python main.py unity-create "A 2D platformer with Cinemachine"
python main.py unity-create --file prompts/brief.txt --compose   # assemble + wire micros
python main.py unity-validate path/to.schema.json  # recompile, no LLM
python main.py unity-resolve "universal render pipeline"

# Micro-generators (focused drop-in artefacts)
python main.py unity-tools list
python main.py unity-controller "scuba diver with oxygen" --variant swimming
python main.py unity-model "a lifted matte-black pickup" --no-llm -u output/unity/<Project>

# Blender asset factory
python blender_cli.py doctor
python blender_cli.py from-schema output/unity/<Project>/<Project>.schema.json

# Per-run model override (LM Studio serves whatever the request names)
python main.py unity-create "..." --model your-loaded-model
```

## Tests

```bash
pytest                              # default suite (excludes the slow unity_compile gate)
pytest tests/test_unity_pipeline.py::test_name   # single test
pytest -m smoke                     # structural check against real outputs under output/ (beta gate)
pytest -m unity_compile            # launches a real Unity Editor in batchmode (minutes, opt-in)
```

`pytest.ini` sets `addopts = -m "not unity_compile"`; passing `-m` on the CLI overrides it.

## Conventions & gotchas

- **Steps 4–6 must remain deterministic and LLM-free.** Reproducibility (a fixed `--seed` makes the
  whole prompt→schema→assets chain repeatable) is a core invariant. The schema is the single contract.
- **Graceful degradation everywhere**: a missing model file → primitive; Blender absent → STUBBED job /
  emit-only; LLM unreachable → keyword fallback (procedural model mapping). Don't make these hard failures.
- **Config**: `config.yaml` drives LM Studio/Ollama (`lm_studio.backend`, `base_url`, `model`),
  AnythingLLM (RAG, optional), `basic_mode` (skip RAG, local lookups only), and `blender` /
  `unity_pipeline` paths. The configured model name must match what LM Studio has loaded exactly.
- **rich on Windows**: `main.py` and `blender_cli.py` reconfigure stdout/stderr to UTF-8 — rich Panels
  use box-drawing chars that crash a cp1252 pipe. Keep that guard in any new top-level CLI.
- **sentence-transformers crashes on Windows** (exit -1073741819, access violation in tokenizer DLL), so the RAG indexer uses `fastembed` (ONNX) with model `BAAI/bge-small-en-v1.5` instead — run it as `python -m rag.indexer` (not `rag.index`).
- **Live MCP servers** are available in some sessions: the **capital-B `mcp__Blender__`** server works
  (lowercase `mcp__blender__` times out); Unity MCP needs an Editor running with the plugin. Headless
  Blender (`--background --factory-startup --python`) is the no-GUI path used by the asset factory.
- Canonical script names are forced in the Unity fixer (`GameManager`, `MeshBuilder`, `HealthSystem`)
  because templates hard-reference those class names — renaming them breaks compilation (CS0246).
