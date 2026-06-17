# unity_cli.py

Unity project builder with live editor integration.  
Generates a complete Unity project from a plain-English prompt, then copies the
result directly into a running Unity Editor — or opens it from scratch.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Commands](#commands)
   - [new](#new)
   - [from-schema](#from-schema)
   - [overlay](#overlay)
   - [open](#open)
   - [refresh](#refresh)
   - [list-editors](#list-editors)
4. [Typical workflows](#typical-workflows)
5. [Bridge integration](#bridge-integration)
6. [Overlay rules](#overlay-rules)
7. [Unity Editor discovery](#unity-editor-discovery)
8. [Options reference](#options-reference)

---

## How it works

```
Prompt / Schema
      |
      v
 LLM pipeline          <- src/unity_pipeline.py  (steps 2-5)
      |
      v
 File generator        <- src/unity_generator.py (step 6)
      |
      v
 output/unity/<Name>/  <- complete Unity project on disk
      |
      +--[--target]---> existing Unity project directory
      |                        |
      +--[--running]--> auto-detected open project
                               |
                               v
                    AssetDatabase.Refresh()  <- via MCP bridge (port 7890)
```

`new` runs the full pipeline (LLM included).  
`from-schema` skips the LLM and recompiles a saved JSON schema directly.  
`overlay` copies an already-generated project into any Unity project you point it at.

---

## Prerequisites

```
pip install typer rich pyyaml python-dotenv pydantic jinja2
```

The pipeline also requires **LM Studio** running locally with a model loaded
(configure `lm_studio.base_url` in `config.yaml`).

`from-schema`, `overlay`, `open`, `refresh`, and `list-editors` do **not**
require LM Studio.

---

## Commands

### `new`

Run the full LLM pipeline, write files to disk, and optionally overlay into
Unity or open the project.

```
python unity_cli.py new PROMPT [OPTIONS]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--file PATH` | `-f` | Read the prompt from a `.txt` file instead |
| `--config PATH` | `-c` | Config YAML (default: `config.yaml`) |
| `--output-dir PATH` | `-o` | Override `unity_output_dir` from config |
| `--target PATH` | `-t` | Overlay generated assets into this Unity project |
| `--running` | `-r` | Overlay into the project currently open in Unity |
| `--open` | | Open the project in Unity Editor when done |
| `--force` | | Overwrite all files without skipping unchanged ones |
| `--port INT` | | MCP bridge port (default: `7890`) |

**Examples**

```bash
# Generate and open immediately
python unity_cli.py new "A top-down RPG with NavMesh enemies" --open

# Long structured brief from a file
python unity_cli.py new --file prompts/neon_fugitive.txt --open

# Generate and inject straight into the project Unity has open right now
python unity_cli.py new "2D platformer with Cinemachine" --running

# Generate into a specific existing project folder
python unity_cli.py new "Endless runner" --target D:/Projects/MyRunner --open
```

---

### `from-schema`

Skip the LLM.  Load a previously saved JSON schema, resolve packages,
run the consistency fixer, generate files, and optionally overlay / open.

```
python unity_cli.py from-schema SCHEMA_FILE [OPTIONS]
```

Accepts the same `--target`, `--running`, `--open`, `--force`, `--port`
options as `new`.

**Examples**

```bash
# Fast re-bake after editing a schema by hand
python unity_cli.py from-schema output/NeonFugitive_schema.json --open

# Re-bake and push into the running project
python unity_cli.py from-schema output/NeonFugitive_schema.json --running --force
```

---

### `overlay`

Copy generated project files from a source directory into a target Unity
project, then trigger `AssetDatabase.Refresh()` if Unity is running.

```
python unity_cli.py overlay SOURCE [OPTIONS]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--target PATH` | `-t` | Destination Unity project (default: source itself) |
| `--running` | `-r` | Auto-detect destination from the running Unity Editor |
| `--force` | | Overwrite even files that haven't changed |
| `--dry-run` | | Show what would change without writing anything |
| `--no-refresh` | | Skip the `AssetDatabase.Refresh()` call |
| `--port INT` | | MCP bridge port (default: `7890`) |

**Examples**

```bash
# Copy generated project into an explicit folder
python unity_cli.py overlay output/unity/NeonFugitive --target D:/Projects/NeonFugitive

# Push into whatever project Unity currently has open
python unity_cli.py overlay output/unity/NeonFugitive --running

# Preview changes without writing anything
python unity_cli.py overlay output/unity/NeonFugitive --target D:/Projects/NeonFugitive --dry-run

# Force-refresh everything (useful after a large regen)
python unity_cli.py overlay output/unity/NeonFugitive --running --force
```

**Output**

```
  Added      42
  Updated     3
  Skipped    80
```

---

### `open`

Find the correct Unity Editor for the project's version and launch it.

```
python unity_cli.py open PROJECT_PATH [--unity-version VERSION]
```

```bash
python unity_cli.py open output/unity/NeonFugitive
python unity_cli.py open D:/Projects/MyGame --unity-version 2022.3
```

The version is read automatically from `ProjectSettings/ProjectVersion.txt`
if `--unity-version` is not given.  Falls back to the most recently
installed editor if no version match is found.

---

### `refresh`

Tell a running Unity Editor to re-import all changed assets.

```
python unity_cli.py refresh [--force] [--port INT]
```

| Flag | Description |
|------|-------------|
| `--force` | Use `ImportAllAssets(ForceUpdate)` instead of a light `Refresh()` |
| `--port INT` | MCP bridge port (default: `7890`) |

```bash
python unity_cli.py refresh          # lightweight — only reimports changed files
python unity_cli.py refresh --force  # full reimport of everything
```

Requires the unity-mcp plugin to be active in the running Editor.

---

### `list-editors`

Show all Unity Editor installations found on this machine, Unity Hub
status, and whether the MCP bridge is alive.

```
python unity_cli.py list-editors
```

Example output:

```
Unity Hub:  not found

         Installed Unity Editors
+-------------+------------------------------------------+--------+
| Version     | Path                                     | Status |
+-------------+------------------------------------------+--------+
| 6000.1.4f1  | C:\Program Files\Unity\Hub\Editor\...    |   OK   |
| 2022.3.61f1 | C:\Program Files\Unity\Hub\Editor\...    |   OK   |
| 2022.3.30f1 | C:\Program Files\Unity\Hub\Editor\...    |   OK   |
+-------------+------------------------------------------+--------+

Unity bridge not running on port 7890
```

---

## Typical workflows

### A — Generate and open a brand-new project

```bash
python unity_cli.py new "Neon-lit top-down stealth game with NavMesh guards" --open
```

1. LM Studio generates a project concept and fills the schema.
2. Packages are resolved against the local lookup tables.
3. All project files are written to `output/unity/<ProjectName>/`.
4. Unity Editor is launched with that folder as the project.

---

### B — Iterate on an existing project (fast loop)

Edit `output/unity/NeonFugitive_schema.json` by hand, then:

```bash
python unity_cli.py from-schema output/unity/NeonFugitive_schema.json --running
```

1. Schema is loaded and validated (no LLM).
2. Files are regenerated in `output/unity/NeonFugitive/`.
3. Changed files are copied into the project Unity has open.
4. `AssetDatabase.Refresh()` is called — Unity picks up the changes instantly.

---

### C — Overlay generated assets into an existing hand-crafted project

You already have a Unity project at `D:/Projects/MyGame` with custom scenes
and art. You want to add the generated scripts and prefabs without touching
anything else.

```bash
python unity_cli.py overlay output/unity/MyGame --target D:/Projects/MyGame --dry-run
# Review the change list, then:
python unity_cli.py overlay output/unity/MyGame --target D:/Projects/MyGame
```

`Packages/manifest.json` is **merged** — new dependencies are added but
existing package versions are never removed or downgraded.

---

### D — Refresh after manual edits

Made changes to scripts on disk outside Unity?

```bash
python unity_cli.py refresh
```

---

## Bridge integration

When the **unity-mcp** plugin is active inside the Unity Editor it exposes
an HTTP server on `127.0.0.1:7890`.  `unity_cli.py` uses two endpoints:

| Action | Code sent |
|--------|-----------|
| Light refresh | `UnityEditor.AssetDatabase.Refresh();` |
| Force reimport | `UnityEditor.AssetDatabase.ImportAllAssets(ImportAssetOptions.ForceUpdate);` |
| Detect project path | `return Application.dataPath.Replace("/Assets","");` |

All bridge calls time out after **5 seconds** and fail gracefully — the CLI
never crashes just because Unity isn't running.

To use a non-default port:

```bash
python unity_cli.py overlay output/unity/NeonFugitive --running --port 7891
```

---

## Overlay rules

When copying a generated project into a target directory, the following
rules apply:

| Path | Rule |
|------|------|
| `Library/`, `Temp/`, `obj/`, `Logs/` | **Always skipped** (Unity-generated) |
| `UserSettings/`, `.git/`, `.vs/` | **Always skipped** |
| `Packages/manifest.json` | **Merged** — new deps added, existing ones preserved |
| Everything else | Copied only if content differs (byte-exact check) |
| `--force` flag | Copies all files regardless of content |

The merge logic for `manifest.json`:

```python
# New package -> added
# Existing package -> version unchanged (never downgraded)
# Keys outside "dependencies" -> preserved from destination
```

---

## Unity Editor discovery

Editors are found by scanning these directories:

**Windows**
```
C:\Program Files\Unity\Hub\Editor\
C:\Program Files (x86)\Unity\Hub\Editor\
```

**macOS**
```
/Applications/Unity/Hub/Editor/
```

**Linux**
```
~/.local/share/unityhub/
/opt/unity/editors/
```

Version matching for `--open` and `--unity-version`:

1. Exact prefix match (`6000.0` matches `6000.0.66f2`)
2. Major-number match (`6000` matches `6000.1.4f1`)
3. Most recently installed editor as fallback

---

## Options reference

### Global

```
python unity_cli.py --help
```

### Per-command

```
python unity_cli.py new          --help
python unity_cli.py from-schema  --help
python unity_cli.py overlay      --help
python unity_cli.py open         --help
python unity_cli.py refresh      --help
python unity_cli.py list-editors --help
```
