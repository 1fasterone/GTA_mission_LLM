# Unity Micro-Generators

The **micro layer** produces small, drop-in Unity artefacts — a character controller,
a camera rig, a physics body, a HUD — instead of a whole project. Each micro-generator
runs the same LLM→schema→files pipeline as the full Unity generator, just scoped to one
feature, so you get a focused output you can copy into any existing project.

- **Base class:** `src/micro_pipeline.py` (`MicroPipeline`)
- **Generators (15):** `src/micro/*.py` (`controller`, `camera`, `physics`, `hud`, `input`, `audio`, `ai`, `spawner`, `saveload`, `inventory`, `dialogue`, `menu`, `interactable`, `transition`, `health`)
- **Composition:** `unity-create --compose` assembles these into one wired project — see `src/unity_compose.py`. The named connections between micros are documented in [`WIRING_SEAMS.md`](WIRING_SEAMS.md).
- **What the LLM may author into a micro:** [`LLM_ENRICHMENT_STRATEGY.md`](LLM_ENRICHMENT_STRATEGY.md) — the name/role split that lets a model's invented vocabulary reach the emitted code without being able to break it.
- **Templates:** `templates/unity/micro/<kind>/`
- **Registry:** `src/micro/__init__.py` (`REGISTRY`)
- **CLI:** `python main.py unity-<kind> "<prompt>"` (list all with `python main.py unity-tools list`)
- **Output:** `output/unity/micro/<kind>/<Name>/` + a `<Name>.schema.json` sidecar

---

## 1. When to use micro vs the full project generator

| | **Full project generator** | **Micro-generator** |
|---|---|---|
| Entry point | `python unity_cli.py new "<prompt>"` | `python main.py unity-controller "<prompt>"` |
| Code | `src/unity_pipeline.py` + `src/unity_generator.py` | `src/micro_pipeline.py` + `src/micro/<kind>.py` |
| Output | A complete Unity project (manifest, ProjectSettings, scenes, prefabs, materials, 10–15 scripts, MCP bridge) | One feature folder: a `.cs` (or few) + optional asset + `README.md` |
| Scope | "Build me a game" | "Build me **this one system**" |
| Asset resolution (Step 4) | Maps requested packages → UPM IDs via RAG | Skipped — micros are self-contained |
| Render pipeline / packages | Chosen + injected | Not its concern (drop-in to host project) |

**Use the full generator when** the user wants a runnable project: a scene that loads,
a player, AI, UI, audio, and settings all wired together. It owns the manifest, the scenes,
and cross-system wiring.

**Use a micro-generator when** you want a single, portable feature to drop into an existing
project — e.g. "a swimming controller with an oxygen meter", "an RTS pan/zoom camera",
"a destructible crate with impact events". A micro produces just that feature's scripts +
an optional asset (input actions, UXML/USS, etc.) and a `README.md` explaining how to attach it.

Rule of thumb: **if the answer is a scene, use the full generator; if the answer is a
component you'd attach to a GameObject, use a micro.** Internally, the full generator owns
project-level concerns (the magenta-shader/UGUI-GUID/scene-wiring fixes live there); micros
own one MonoBehaviour and its immediate assets.

---

## 2. How the `extra_*` fields work

### The problem they solve

A micro's structural schema (e.g. `ControllerSchema` with `variant`, `move_speed`,
`abilities`) can only express what was *modelled in advance*. Early micros produced the
**same script regardless of the prompt** — the LLM's concept ("oxygen meter", "shift-to-boost")
was captured at Step 2 but had no field to land in, so it never reached the `.cs` file.

The fix: the LLM authors **typed C# fragments** alongside the structural fields. The template
emits the *baseline* for the chosen variant; the `extra_*` fields carry the *prompt-specific
behavior*. Because the fragments live **in the schema**, Step 6 (Jinja render) stays a pure
function of the schema — determinism is preserved while the LLM gains real structural influence.

### The fields (`src/micro/code_fields.py`)

Mix `CodeExtensibleSchema` into your schema to get all six for free:

| Field | Type | Renders as |
|---|---|---|
| `extra_usings` | `list[str]` | `using X;` lines at the top |
| `extra_fields` | `list[CodeField]` | `[SerializeField] private` members |
| `awake_extra` | `str` | statements appended inside `Awake()` |
| `update_extra` | `str` | statements appended inside the per-frame method |
| `fixed_update_extra` | `str` | a `FixedUpdate()` body (emitted only when non-empty) |
| `extra_methods` | `list[CodeMethod]` | helper methods appended to the class |

- **`CodeField`**: `{name, type="float", default?, header?, tooltip?}` — becomes
  `[Header("…")] [Tooltip("…")] [SerializeField] private <type> <name> = <default>;`
- **`CodeMethod`**: `{signature, body}` — the template adds the braces (don't include them
  in `body`).

### The flow

1. **Step 3** — the LLM fills the structural fields *and* the `extra_*` fields. Append
   `UNITY_EXTRA_INSTRUCTIONS` (from `code_fields.py`) to your `schema_prompt` so the LLM
   knows how to use them.
2. **Step 5** — `default_fix` calls `lint_extensible(schema, reserved_fields=…, template_usings=…)`.
   LLMs author imperfect C#, so the linter is deliberately defensive (never raises): it strips
   markdown fences, rejects Python-shaped fragments, removes stray `using` lines from bodies,
   auto-closes unbalanced braces, caps method length, and dedupes usings/fields/methods.
   `reserved_fields` drops any `extra_field` whose name collides with a field the template
   already emits; `template_usings` drops namespaces the template already imports.
3. **Step 6** — the template renders the fragments through the shared macros in
   `templates/unity/micro/_inject.j2`:

   ```jinja
   {% import "_inject.j2" as inject %}
   using UnityEngine;
   {{ inject.usings(s) }}

   public class {{ s.name }} : MonoBehaviour
   {
       {{ inject.fields(s) }}

       private void Awake()
       {
           // baseline …
           {{ inject.lifecycle(s.awake_extra) }}
       }
   {{ inject.fixed_update(s) }}
   {{ inject.methods(s) }}
   }
   ```

### Worked example

Prompt: *"underwater scuba explorer with shift-to-boost and an oxygen meter that depletes
over time"* (`--variant swimming`). The LLM produces (abridged):

```json
{
  "name": "ScubaExplorerController",
  "variant": "swimming",
  "extra_usings": ["UnityEngine.Events"],
  "extra_fields": [
    {"name": "maxOxygen", "type": "float", "default": "100f", "header": "Oxygen", "tooltip": "Breath capacity in seconds."},
    {"name": "oxygenDepletionRate", "type": "float", "default": "1.5f"},
    {"name": "boostMultiplier", "type": "float", "default": "2.2f", "header": "Boost"},
    {"name": "boostCostPerSecond", "type": "float", "default": "4f"},
    {"name": "OnOxygenEmpty", "type": "UnityEvent", "header": "Events"}
  ],
  "awake_extra": "_oxygen = maxOxygen;",
  "update_extra": "bool boosting = Keyboard.current != null && Keyboard.current.leftShiftKey.isPressed && _oxygen > 0f;\nif (boosting) { moveSpeed = sprintSpeed * boostMultiplier / 2f; }\nDeplete(boosting ? boostCostPerSecond : oxygenDepletionRate);",
  "extra_methods": [
    {"signature": "private void Deplete(float ratePerSecond)",
     "body": "_oxygen = Mathf.Max(0f, _oxygen - ratePerSecond * Time.deltaTime);\nif (_oxygen <= 0f && OnOxygenEmpty != null) OnOxygenEmpty.Invoke();"}
  ]
}
```

renders into the baseline `swimming` controller as:

```csharp
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.Events;

public class ScubaExplorerController : MonoBehaviour
{
    // baseline fields: moveSpeed, sprintSpeed, _cc, _velocity …
    [Header("Oxygen")] [Tooltip("Breath capacity in seconds.")] [SerializeField] private float maxOxygen = 100f;
    [SerializeField] private float oxygenDepletionRate = 1.5f;
    [Header("Boost")] [SerializeField] private float boostMultiplier = 2.2f;
    [Header("Events")] [SerializeField] private UnityEvent OnOxygenEmpty;

    private void Update()
    {
        // baseline swim movement …
        // ── prompt-specific ──
        bool boosting = Keyboard.current != null && Keyboard.current.leftShiftKey.isPressed && _oxygen > 0f;
        if (boosting) { moveSpeed = sprintSpeed * boostMultiplier / 2f; }
        Deplete(boosting ? boostCostPerSecond : oxygenDepletionRate);
    }

    // ── prompt-specific ──
    private void Deplete(float ratePerSecond)
    {
        _oxygen = Mathf.Max(0f, _oxygen - ratePerSecond * Time.deltaTime);
        if (_oxygen <= 0f && OnOxygenEmpty != null) OnOxygenEmpty.Invoke();
    }
}
```

> Reference your own `extra_fields` by name in the bodies — they become private members.
> Reference baseline fields directly (`moveSpeed`, `_cc`, `_rb`, `_velocity`, `_grounded`).
> If a prompt has no special behavior, leave every `extra_*` empty and you get the clean baseline.

---

## 2b. Generator catalog

| Kind | CLI | Variants |
|---|---|---|
| `controller` | `unity-controller` | first_person, third_person, top_down, 2d_platformer, 2d_topdown, vehicle_car, vehicle_bike, flying, swimming, pointer, multi_mode |
| `camera` | `unity-camera` | follow, orbit, first_person, cinemachine_3p, cinemachine_topdown, rts_pan_zoom, vehicle_chase, cinematic_dolly, board_framing |
| `physics` | `unity-physics` | rigidbody_2d, rigidbody_3d, kinematic_character, ragdoll, vehicle_wheels, soft_body_stub, trigger_zone, destructible_object, buoyancy |
| `hud` | `unity-hud` | health_bar, ammo_counter, minimap, crosshair, interaction_prompt, score_panel, pause_menu, dialogue_box, full_hud_bundle |
| `input` | `unity-input` | keyboard_mouse, gamepad, touch, dual_stick, racing_wheel, vr_xr, pointer |
| `audio` | `unity-audio` | 2d_simple, 3d_spatial, music_layers, crossfade_zones, categorical_mixer |

> `controller`'s `multi_mode` variant is a runtime-switchable "Control Surface
> Stack" — one player, several coexisting locomotion modes on one schema
> instance — rather than a normal one-mode-per-project variant. See
> [`CONTROL_SURFACE_STACK.md`](CONTROL_SURFACE_STACK.md) for the mode
> vocabulary, the architecture, and its named v1 limitations.
| `ai` | `unity-ai` | navmesh_patrol, chase_player, fsm_guard, behaviour_tree_lite, flocking, cover_shooter, flying_swarm |
| `spawner` | `unity-spawner` | wave_spawner, continuous_spawner, object_pool, trigger_spawner, point_spawner, radial_spawner |
| `saveload` | `unity-saveload` | json_file, playerprefs_kv, save_slots, checkpoint, autosave, encrypted_json |
| `inventory` | `unity-inventory` | slot_based, grid, weight_based, equipment, hotbar, unlimited |
| `dialogue` | `unity-dialogue` | linear, branching, typewriter, cutscene, barks, localized |
| `menu` | `unity-menu` | main_menu, pause_menu, settings_menu, game_over, level_select, confirm_dialog |
| `interactable` | `unity-interactable` | pickup, door, lever, button_switch, npc_talk, readable |
| `transition` | `unity-transition` | instant, fade, loading_bar, async_additive, trigger_door |
| `health` | `unity-health` | simple, regenerating, shielded, armor, lives |

### Spawner (`unity-spawner`)

Produces a drop-in `MonoBehaviour` that instantiates (or pools) a prefab over time,
on a trigger, or as a one-shot — with a live-instance cap and configurable spawn area.
Pairs naturally with the `ai` micro (spawn the enemy it controls).

- **Variants** — `wave_spawner` (discrete ramping waves), `continuous_spawner` (steady timer
  trickle), `object_pool` (pure pool broker), `trigger_spawner` (burst when the player enters a
  trigger), `point_spawner` / `radial_spawner` (one-shot at start).
- **Key fields** — `spawn_shape` (`point|circle|box|spawn_points`), `pooling`
  (`pooled|instantiate`), `trigger` (`timer|on_enter|manual|wave`), `max_alive`,
  `spawn_interval`, `per_wave_count`, `wave_count`, `spawn_radius`, `warmup_count`,
  `target_tag`. The three enum fields are clamped via `clamp_enum` (in `code_fields.py`), so a
  loose model that echoes the option menu (e.g. `"pooled | instantiate"`) can't crash validation.
- **Files** — `Scripts/<Name>.cs` always; `Scripts/<Name>Pool.cs` when `pooling == "pooled"`
  (forced for the `object_pool` variant); plus `README.md`.
- **Public API** — `Spawn()` is exposed so Timelines / UnityEvents / other scripts can fire a
  spawn manually.

```bash
python main.py unity-spawner "zombie wave every 20s in a ring around the crypt, ramping" \
  --variant wave_spawner
python main.py unity-spawner "pooled bullet emitter" --variant object_pool
```

### Save/Load (`unity-saveload`)

Produces a persistence layer: a `[Serializable]` data class plus a manager `MonoBehaviour`
exposing `Save()` / `Load()` / `HasSave()` / `DeleteSave()`. Self-contained — no external deps.

- **Variants** — `json_file` (JSON in `persistentDataPath`), `playerprefs_kv` (JSON blob in
  PlayerPrefs), `save_slots` (N slots; `Save(int)/Load(int)`), `checkpoint` (adds
  `SetCheckpoint()/Respawn()` against a player Transform), `autosave` (auto-load + timer + quit/pause
  saving), `encrypted_json` (XOR+Base64 obfuscated file — light, not real crypto).
- **Key fields** — `storage` (`file|playerprefs`, clamped via `clamp_enum`),
  `slot_naming` (`numbered|named`, clamped), `slot_count`, `autosave_interval`, `save_on_quit`,
  `save_on_pause`, `default_file_name`, `encryption_key`, and `data_fields` — a list of
  `CodeField` that become the members of the serializable save payload (defaulted to
  level/score/playerName when the model leaves it empty).
- **Files** — `Scripts/<Name>Data.cs`, `Scripts/<Name>.cs`, `README.md` (always 3).

```bash
python main.py unity-saveload "checkpoint system that respawns the player" --variant checkpoint
python main.py unity-saveload "three save slots for an RPG" --variant save_slots
```

### Inventory (`unity-inventory`)

Produces an item model + container manager: a `ItemDefinition` ScriptableObject plus a
`MonoBehaviour` with `Add/Remove/Count/Has`, a `Stacks` view, and an `OnChanged` event for UI.

- **Variants** — `slot_based` (fixed slot count), `grid` (width×height), `weight_based`
  (capacity by total weight), `equipment` (named slots via `Equip/Unequip`), `hotbar` (slots +
  `Select(i)`/`Selected`), `unlimited` (no cap).
- **Key fields** — `stacking` (`stackable|unique`, clamped via `clamp_enum`), `sort_mode`
  (`none|by_name|by_weight`, clamped → emits a `Sort` on change), `slot_count`, `grid_width`/
  `grid_height`, `max_weight`, `equipment_slots` (defaulted for the equipment variant),
  `starting_items`, `allow_drop`.
- **Files** — `Scripts/<Name>ItemDefinition.cs`, `Scripts/<Name>.cs`, `README.md` (always 3).
- **Note** — the `equipment` variant generates a slot-dictionary manager (no stacks); the other
  five share the stack-list manager. Subscribe to `OnChanged` to refresh any HUD bound to it.

```bash
python main.py unity-inventory "5x4 grid backpack with stacking" --variant grid
python main.py unity-inventory "equipment slots for head chest weapon" --variant equipment
```

### Dialogue (`unity-dialogue`)

Produces a conversation runner: a serializable `Node`/`Choice` model plus a `MonoBehaviour` with
the actual lines **baked in** (the LLM authors the script), driving `OnLine` / `OnChoices` /
`OnDialogueEnd` events for your UI.

- **Variants** — `linear` (sequence), `branching` (choices → node ids via `Choose(i)`),
  `typewriter` (per-char reveal), `cutscene` (auto-advance on a timer), `barks` (random one-offs
  via `PlayBark()`), `localized` (line text are loc keys; override `Resolve(string)`).
- **Key fields** — `advance` (`submit|any_key|auto`, clamped via `clamp_enum`), `text_render`
  (`instant|typewriter`, clamped), `text_speed`, `auto_advance_delay`, `start_node`, and `nodes`
  (a nested list of `{id, speaker, text, choices[]}` — the conversation itself; defaulted to a
  sample if the model leaves it empty).
- **Files** — `Scripts/<Name>Node.cs`, `Scripts/<Name>.cs`, `README.md` (always 3).
- **Note** — node text is emitted through Jinja's `tojson`, so quotes/newlines in LLM-authored
  lines become valid C# string literals. Subscribe to `OnLine` to feed a HUD `dialogue_box`.

```bash
python main.py unity-dialogue "guard challenges the player with branching replies" --variant branching
python main.py unity-dialogue "intro cutscene narration" --variant cutscene
```

### Menu (`unity-menu`)

A UI-flow controller (`Show`/`Hide`, button → action) for main/pause/settings/game-over/level-select/
confirm screens. uGUI **or** UI Toolkit backend (emits `.uxml`/`.uss` for the latter). Common items
(Play/Resume/Retry/Quit/Back) act automatically; everything else raises `OnItemSelected`. Pause and
settings variants set `Time.timeScale = 0` while open. Enums `backend`/`navigation` use `clamp_enum`.

```bash
python main.py unity-menu "main menu with play, settings, quit" --variant main_menu
python main.py unity-menu "pause overlay" --variant pause_menu --model meta-llama-3.1-8b-instruct
```

### Interactable (`unity-interactable`)

A "press [E]" world object: pickup / door / lever / button_switch / npc_talk / readable. Trigger or
raycast detection, raises `OnPrompt(text)` (wire to the HUD `interaction_prompt`) and `OnInteract`
(+`OnToggled(bool)` for doors/levers/switches). Pickups are auto one-shot. Enums `detection`/
`interact_key` clamp.

```bash
python main.py unity-interactable "a treasure chest you open with E" --variant pickup
python main.py unity-interactable "a wall lever that opens a gate" --variant lever
```

### Scene transition (`unity-transition`)

Loads a scene with style + trigger: instant / fade-to-black / loading-bar (async) / async-additive /
trigger_door. `manual` (`Go()`), `on_trigger` (player enters), or `on_timer`. Single or additive
load mode. Built on `UnityEngine.SceneManagement`. Enums `load_mode`/`trigger` clamp.

```bash
python main.py unity-transition "fade to the next level" --variant fade
python main.py unity-transition "a door that loads the boss arena" --variant trigger_door
```

### Health (`unity-health`)

A damage/heal/death component: `TakeDamage`/`Heal`, `CurrentHealth`/`MaxHealth`, and
`OnHealthChanged(current,max)` / `OnDamaged` / `OnHealed` / `OnDeath` events. Variants: `simple`,
`regenerating` (regen after a delay), `shielded` (shield absorbs first), `armor` (% damage
reduction), `lives` (respawn until lives run out). Enum `death_action`
(`destroy|disable|respawn|event_only`) clamps. Pairs with the HUD `health_bar` micro — the
composition layer auto-wires `OnHealthChanged → HUD.SetHealth`.

```bash
python main.py unity-health "player with regenerating shields" --variant shielded
python main.py unity-health "enemy with 3 lives" --variant lives
```

---

## 3. How to add a new micro-generator

Say you want a `loot` micro (`python main.py unity-loot "…"`).

**1. Schema + pipeline — `src/micro/loot.py`**

```python
from __future__ import annotations
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator

from ..micro_pipeline import MicroPipeline, to_pascal
from .code_fields import CodeExtensibleSchema, lint_extensible, UNITY_EXTRA_INSTRUCTIONS

LootVariant = Literal["coin", "chest", "weapon_drop", "random_table"]
VARIANTS = list(LootVariant.__args__)  # type: ignore[attr-defined]

class LootSchema(CodeExtensibleSchema):          # ← inherit the extra_* fields
    name:        str   = "LootItem"
    variant:     str   = "coin"
    value:       int   = 10
    respawns:    bool  = False
    description: str   = "A collectible loot item"
    meta:        dict  = Field(default_factory=dict)   # keep: pipeline stashes theme/concept here

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "coin"

CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity loot pickup for this idea:

"{user_prompt}"

Variant requested: {variant}
Cover pickup behavior, value, respawn, and any VFX/SFX cue. Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "value": 10,
  "respawns": false,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS               # ← teaches the LLM to fill extra_*

class LootPipeline(MicroPipeline):
    kind            = "loot"
    display_name    = "Unity Loot Item"
    schema_cls      = LootSchema
    variants        = VARIANTS
    default_variant = "coin"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "loot"                  # → templates/unity/micro/loot/

    _RESERVED_FIELD_NAMES = {"value", "respawns"}   # names the template already emits
    _TEMPLATE_USINGS      = {"UnityEngine"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "LootItem")
        if schema.value < 0:
            schema.value = 0
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)   # ← always lint extras
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts = out_dir / "Scripts"; scripts.mkdir(parents=True, exist_ok=True)
        (scripts / f"{schema.name}.cs").write_text(
            env.get_template("Loot.cs.j2").render(s=schema, variant=variant), encoding="utf-8")
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant), encoding="utf-8")
        return 2
```

**2. Templates — `templates/unity/micro/loot/`**

Create `Loot.cs.j2` (the MonoBehaviour) and `README.md.j2`. Wire the `extra_*` injection
points via the shared macro so the prompt-specific behavior renders consistently:

```jinja
{% import "_inject.j2" as inject %}
using UnityEngine;
{{ inject.usings(s) }}

/// <summary>{{ s.description }}</summary>
public class {{ s.name }} : MonoBehaviour
{
    [SerializeField] private int value = {{ s.value }};
    [SerializeField] private bool respawns = {{ "true" if s.respawns else "false" }};
    {{ inject.fields(s) }}

    private void Awake()
    {
        {{ inject.lifecycle(s.awake_extra) }}
    }

    private void OnTriggerEnter(Collider other)
    {
        // baseline pickup …
        {{ inject.lifecycle(s.update_extra) }}
    }
{{ inject.fixed_update(s) }}
{{ inject.methods(s) }}
}
```

`jinja()` searches the generator's own folder **and** the shared `micro/` root, so
`{% import "_inject.j2" %}` resolves automatically.

**3. Register — `src/micro/__init__.py`**

```python
from .loot import LootPipeline, LootSchema
# … add to __all__ …
REGISTRY = {
    # …existing…
    "loot": LootPipeline,
}
```

**4. CLI command — `main.py`**

```python
@app.command("unity-loot")
def unity_loot(
    prompt: Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:   Optional[Path] = typer.Option(None, "--file", "-f"),
    variant: Optional[str] = typer.Option(None, "--variant", help="coin|chest|weapon_drop|random_table"),
    theme:  Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:   Optional[int]  = typer.Option(None, "--seed"),
    out:    Optional[str]  = typer.Option(None, "--out"),
    config: str            = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Generate a focused Unity loot pickup."""
    _run_micro("loot", prompt, file, variant, theme, mashup_with, seed, out, config)
```

**5. Run it**

```bash
python main.py unity-tools list                 # confirm "unity-loot" shows up
python main.py unity-loot "a glowing chest that bursts into coins" --variant chest
# → output/unity/micro/loot/<Name>/Scripts/<Name>.cs  +  README.md  +  <Name>.schema.json
```

### Checklist for a new micro

- [ ] Schema extends `CodeExtensibleSchema` (so it gets the six `extra_*` fields).
- [ ] `variant` has a `field_validator` that clamps to a known value.
- [ ] Keep a `meta: dict` field — the base pipeline stashes theme + concept there for the Library page.
- [ ] `schema_prompt` ends with `+ UNITY_EXTRA_INSTRUCTIONS`.
- [ ] `default_fix` calls `lint_extensible(...)` with accurate `_RESERVED_FIELD_NAMES`
      (every field the template already declares) and `_TEMPLATE_USINGS`.
- [ ] The main `.cs.j2` imports `_inject.j2` and calls `inject.usings/fields/lifecycle/fixed_update/methods`.
- [ ] Registered in `REGISTRY` **and** exposed as a `unity-<kind>` CLI command.
- [ ] `write_files` returns an accurate file count and always writes a `README.md`.

> The `controller` generator predates the shared helpers and inlines its own `extra_*` loops
> and lint pass — prefer the `physics` generator (`src/micro/physics.py`) as the template for
> new ones: it uses `CodeExtensibleSchema`, `lint_extensible`, `UNITY_EXTRA_INSTRUCTIONS`, and
> the `_inject.j2` macros end-to-end.

### When NOT to follow this checklist: `terrain`

`src/micro/terrain.py` (`unity-terrain-gen`) is a deliberate exception to everything
above, not an oversight:

- It is **world/region-scale**, not per-entity — one bake per `WorldRegion`
  (`schema.world_spec.regions`), not one instance per prompt.
- Its artifact is **data**, not a `MonoBehaviour` — a heightmap/splatmap PNG pair, read
  by a fixed runtime builder script (`TerrainBuilder.cs.j2`) that is never
  LLM-authored.
- It is **not registered in `micro.REGISTRY`**, never goes through `enrich_subsystems`
  (there is no `extra_*` C# surface to enrich — see
  `src/unity_terraingen.py`'s module docstring), and is not covered by
  `src/micro_stress.py`'s generic per-kind harness for the same reason `model` isn't
  (`_NO_SCHEMA`): it has its own dedicated tests instead
  (`tests/test_unity_terraingen.py`, `tests/test_terrain_pipeline_files.py`).
- It still wires into `unity-create --compose` — see the `schema.world_spec` block in
  `src/unity_pipeline.py`, right after `composer.emit(...)` — just outside the
  `Subsystem`/`MicroComposer` model entirely, because terrain doesn't attach to a
  GameObject or participate in any `GameWiring` seam.

If a future micro is similarly world-scale or data-artifact-shaped rather than
per-entity-and-C#-shaped, follow `terrain`'s pattern instead of forcing it through
`MicroPipeline`.
