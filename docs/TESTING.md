# Beta Testing Checklist

This is the canonical "did the output actually work?" checklist. It has two
halves: an **automated** smoke suite that the maintainer runs before sending
out a build, and a **manual** in-game / in-Hub checklist that each tester
runs against every generation they exercise.

---

## A. Automated (maintainer runs before shipping)

```powershell
# Regression
python -m pytest -q

# Output smoke against everything currently in output/ and output/unity/
python -m pytest -m smoke
```

The smoke suite (`tests/test_outputs_smoke.py`) gates:

| Check                                             | What it catches                                  |
|---------------------------------------------------|--------------------------------------------------|
| GTA `.3.cs` has auto-generated header             | Template wasn't applied                          |
| GTA `.3.cs` has `public class X : Script`         | SHVDN3 entry point missing                       |
| GTA `.3.cs` has required `using GTA.*;` imports   | Won't compile in SHVDN3                          |
| GTA: no `UNRESOLVED` token                        | Model resolution silently failed                 |
| GTA / Unity: no unrendered Jinja tags (`{{` / `{%`) | Template rendering bug                           |
| Schema round-trip                                 | Model/schema drift between code and saved JSON   |
| Unity: `Assets/`, `Packages/manifest.json`, `ProjectSettings/` | Project won't open in Hub             |
| Unity: every manifest dep matches `^com\.…`       | Malformed package id                              |
| Unity: at least one `.unity` scene file           | Project opens but is empty                       |
| Unity: `ProjectVersion.txt` declares an editor    | Hub won't know which editor to use               |
| Unity: 3D project scripts don't reference 2D physics types (`Collider2D`/`Rigidbody2D`/`Physics2D`), and vice-versa | URP CS1069 "type has been forwarded to assembly Physics2DModule." Silently breaks the entire scripts assembly; cascades into "duplicate fileID" + "prefab parser failure" errors on first open. Real-world catch from private beta. |
| Unity: no manifest dep with version `"1.0.0"` (for `com.unity.*` packages) | Old generator fallback when no version was known; no current Unity official package ships at exactly 1.0.0, so it 404s on the registry and Unity refuses the entire manifest. Real-world catch from private beta. |

If anything fails, **do not ship** until either the broken output is
regenerated or removed from `output/`. Stale outputs from old template
versions are a normal cause — regenerate them through the UI.

---

## A2. Headless compile gate (Unity batchmode)

The smoke suite proves a project is *structurally* sound; it does **not** prove
the generated C# actually compiles. `unity_cli.py verify` closes that loop: it
launches the real Unity Editor in `-batchmode`, compiles the project, and
reports every C# error mapped back to the template that produced it.

```powershell
# Generate from a prompt, then compile-check it
python unity_cli.py verify "A 2D platformer with Cinemachine"

# Compile-check an already-generated project (skips the LLM)
python unity_cli.py verify --source output/unity/NeonFugitive

# Walk the representative corpus (one prompt per line)
Get-Content tests/unity_corpus.txt | ForEach-Object { python unity_cli.py verify $_ }
```

Exit code: `0` clean, `1` compile errors, `2` harness failure (no editor /
Unity couldn't launch). Artifacts left in the project root: `compile_report.json`
(structured, written by `Assets/CompileCheck/CompileCheck.cs`) and `compile.log`
(the raw editor log — the authoritative source the harness greps for `error CS`).

How it works: `verify` drops an **isolated** editor assembly
(`CompileCheck.Editor.asmdef`, no reference to `Assembly-CSharp`) so it compiles
and runs even when the project's own scripts don't. When a *package* fails to
compile, Unity skips `-executeMethod` entirely and writes no JSON — which is why
the log grep, not the JSON, is the source of truth.

A pytest wrapper exists but is opt-in (slow): `pytest -m unity_compile`. It
skips cleanly when no editor is installed or no project is on disk.

### Known compile error: missing built-in modules under Unity 6 (`CS1069 … forwarded to UnityEngine.XxxModule`)
- Symptom (first caught by `verify` on Unity 6000.1): a wall of `CS1069` for
  `Physics`, `Physics2D`, `AudioClip`/`AudioSource`, `Texture2D.EncodeToPNG`,
  `ScreenCapture`, … — "type has been forwarded to assembly
  UnityEngine.XxxModule." Appears in `Library/PackageCache/com.coplaydev.unity-mcp/…`
  (the MCP bridge) **and** in generated gameplay scripts like `AudioManager`.
- Cause: Unity 6 omits built-in engine modules from a freshly-written manifest
  unless they are listed. A 3D project masks it by pulling many modules
  transitively through its packages (navigation → physics, …); a lean 2D
  project pulls almost none, so the gaps surface as a cascade (fix one module,
  the recompile reveals the next).
- **Fixed** in `build_manifest_deps`: every generated manifest now ships the
  standard built-in module set Unity's own default template writes (see
  `BUILTIN_MODULES`) — independent of the MCP bridge flag, since gameplay needs
  them too. Guarded by `test_unity_build_manifest_deps_injects_mcp_bridge`.
  Regenerate any pre-fix `output/unity/` project (or run `verify --source`)
  to clear it.

---

## B. Manual — GTA5 mission (per output, ~10 minutes)

Setup once on the test machine:
- Install [ScriptHookV](https://www.dev-c.com/gtav/scripthookv/) into the GTAV
  install root.
- Install ScriptHookVDotNet (drops `ScriptHookVDotNet.asi` and
  `ScriptHookVDotNet3.dll` into the GTAV install root).
- Create `Grand Theft Auto V/scripts/` if it doesn't exist.

Per generation:

- [ ] Drop the generated `<Name>.3.cs` into `Grand Theft Auto V/scripts/`.
- [ ] Launch GTAV. `ScriptHookVDotNet.log` shows the script compiled with no
      errors (warnings about unused imports are fine).
- [ ] In-game, the mission's start blip appears at the location declared in
      the schema's `location_name`.
- [ ] Walking up to the start blip prompts the mission to begin (or it
      auto-starts if the schema says so).
- [ ] Step 1's marker/objective text appears.
- [ ] All declared **peds** spawn (no `Tried to spawn X — model not found`
      lines in the SHVDN log).
- [ ] All declared **vehicles** spawn.
- [ ] All declared **objects** / pickups spawn.
- [ ] Each mission step completes when expected (markers advance, peds die
      on KILL_ALL, deliver completes on the deliver step, etc.).
- [ ] Mission-passed audio plays and the success message appears.
- [ ] Cleanup: no leftover peds/vehicles/blips after mission ends.

### Per-issue report template

> **Project / mission:** `<name>` (from Library)
> **Generated:** `<date>`
> **Backend:** LM Studio / Ollama
> **Theme / seed:** `<value>`
> **Step that broke:** `<step number + type>`
> **Symptom:** what you saw vs. what you expected
> **SHVDN log excerpt:** ```paste```
> **Schema concept (from Library "Show more"):** ```paste```

---

## C. Manual — Unity project (per output, ~10 minutes)

Setup once:
- Install Unity Hub.
- Install the editor version declared in the project's
  `ProjectSettings/ProjectVersion.txt` (current default: 6000.0.66f2).

Per generation:

- [ ] Add the project to Hub (`Add → Add project from disk`, point at the
      generated folder under `output/unity/<Name>/`).
- [ ] Hub recognizes the editor version (no "select editor version" prompt).
- [ ] Open. Unity imports without **errors** in the Console (yellow warnings
      about missing assets / .meta regen are fine on first import).
- [ ] `Window → Package Manager` shows every dependency from
      `Packages/manifest.json` resolved (no red error icons).
- [ ] Scenes under `Assets/Scenes/` open without missing-script warnings.
- [ ] `Main.unity` is the first scene in `File → Build Settings → Scenes In
      Build`.
- [ ] Sample script(s) under `Assets/Scripts/` compile (Console shows no
      compile errors on save).
- [ ] No missing references when selecting the player or any prefab.
- [ ] Play mode enters without runtime errors in the Console.
- [ ] (Optional) Build a development player to confirm the project can be
      built end-to-end.

### Per-issue report template

Same shape as the GTA template — substitute SHVDN log with the Unity Console
output (right-click → Copy on the failing entry).

---

## Tester-environment issues (not generator bugs)

Things that look like generator output bugs but are actually local Unity /
machine state. If you see these, the project is fine — your install is not.

### `Shader compiler initialization error: Failed to launch UnityShaderCompiler.exe`
- Unity can't spawn its own shader compiler process. Has nothing to do with
  the generated project.
- **Common causes** (in order of likelihood):
  1. Antivirus / Defender quarantined `UnityShaderCompiler.exe`. Add the
     Unity install root (`<editor>/Editor/Data/Tools/`) to AV exclusions.
  2. Unity install on a non-system drive (e.g. `D:\unity\…`) with a
     permissions or handle limit. Verify the editor can be launched standalone
     once before generating projects.
  3. Editor install corrupted. Open Unity Hub → Installs → … → Repair (or
     reinstall the exact editor version listed in `ProjectVersion.txt`).
  4. Disk full on the install volume.
- **Diagnostic**: `D:\unity\6000.0.66f2\Editor\Unity.exe -batchmode -quit -nographics`
  — if Unity can't launch this way either, it's an install / OS problem.

### `Package [com.unity.X@1.0.0] cannot be found`
- This was a real generator bug — the manifest fell back to the literal
  string `"1.0.0"` when no version was known. **Fixed** in iteration 4.
- If you see it on a freshly generated project (not an older `output/unity/`
  one), regenerate with the current build. The smoke test
  `test_unity_manifest_no_bogus_fallback_versions` catches this pre-ship.
- If you see it on an older output, regenerate that project.

### Known compile error: `CS0246: 'GameManager' could not be found` (singleton name coupling)
- Symptom (caught by the `verify` corpus sweep on an isometric + a narrative
  project): a referencing script (`UIManager.cs`, `Collectible.cs`, or
  `ProceduralMesh.cs`) fails with `CS0246` for `GameManager`, `MeshBuilder`, or
  `HealthSystem`.
- Cause: those templates hard-reference the singleton class by name, but the
  class name is `{{ script.name }}` — whatever the LLM called the script. When
  the LLM names a `game_manager` script thematically (e.g. "BaseBuilder"), the
  reference doesn't resolve. The boilerplate-injection in `_check_consistency`
  only fires when the type is **absent**, so an LLM-supplied-but-misnamed script
  slips through.
- **Fixed** in `_programmatic_fix` (`src/unity_pipeline.py`): script types in
  `_CANONICAL_SCRIPT_NAMES` (`game_manager`→`GameManager`, `mesh_builder`→
  `MeshBuilder`, `health`→`HealthSystem`) are force-renamed to their canonical
  class name and de-duplicated. Guarded by `test_unity_canonical_singleton_names`.

### Known compile error: `CS0246: 'HealthSystem' could not be found` (collectible without enemy)
- Symptom: a project with collectibles but no enemies (e.g. a base-builder)
  fails — `Collectible.cs` calls `GetComponent<HealthSystem>()` for heal pickups.
- Cause: `HealthSystem` was only injected when an **enemy** existed.
- **Fixed** in `_check_consistency`: `HealthSystem` is now injected when an enemy
  **or** a collectible script is present. Guarded by
  `test_unity_collectible_forces_healthsystem`.

### Known compile error: `CS1519`/`CS1022` in an auto-generated stub script
- Symptom: a script of an LLM-invented type (e.g. `TerrainGenerator`) the
  generator has no template for fails with `Invalid token '{'` / `end-of-file
  expected` at lines 6 & 8.
- Cause: the fallback stub emitted literal `{{` / `}}` (Python-format escaping
  that leaked into a plain string), which is not valid C#.
- **Fixed** in `UnityProjectGenerator._generate_script` (single braces now).
  Guarded by `test_unity_unmapped_script_stub_is_valid_csharp`.

### `verify` / pipeline crashes with `UnicodeEncodeError: 'charmap' codec can't encode …`
- Symptom: a run redirected to a file on Windows dies printing a Rich glyph
  (`→`, `✓`, box-drawing). The default console code page is cp1252.
- **Fixed**: `unity_cli.py` reconfigures stdout/stderr to UTF-8 at startup, and
  the `verify` table/panels use ASCII status words (`PASS`/`FAIL`). If you add a
  new CLI that prints Rich output, copy the `reconfigure` guard.

### Unity prefab "duplicate fileID" + "parser failure" errors at first open
- Almost always a cascade from an upstream script compile error.
  Check the Console for **earlier** CS-prefixed errors (CS1069, CS0103,
  etc.) and fix those first. The prefab errors clear on the next reload.

## What to do with failures

1. **Smoke test failure:** open the failing test name; the assertion message
   identifies the file and reason. Most "missing X" failures are stale
   outputs from before a template change — either regenerate or move the
   stale folder out of `output/`.
2. **Manual checklist failure:** file the report using the template. The
   schema/concept text from the Library row is critical context — paste it
   in.
3. **Backend-side failure (RAG empty, model not loaded, etc.):** check the
   Errors & Fixes page in the UI; the catalog covers the common ones with
   actionable steps.
