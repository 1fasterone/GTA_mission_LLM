# Schema Extensions — the LLM's sandboxed field-invention namespace

See `CLAUDE.md` "Schema Extensions" for the doctrine. This doc is the
implementation reference and the phased-rollout tracker (mirrors
`docs/CONTROL_SURFACE_STACK.md`'s phasing style).

## What it is

Every extensible schema exposes three fields the Pipeline LLM may fill with
genuinely new content — the ONLY place it's allowed to invent a field outside
the fixed schema surface:

| Field | Shape | Becomes |
|-------|-------|---------|
| `extensions_fields`   | `list[CodeField]`  | new `[SerializeField]` declarations |
| `extensions_methods`  | `list[CodeMethod]` | new helper methods |
| `extensions_metadata` | `dict`             | non-code notes — never rendered into the .cs |

This is a second, DIFFERENT channel from the existing `extra_fields`/
`extra_methods`/etc. ("prompt-specific extras") — extras are the established,
battle-tested authoring surface for prompt-specific behavior and are run
through `lint_extensible`'s full repair pipeline (`lint_fields`, `lint_method`,
`lint_code_fragment`) before they reach a template. Extensions exist for the
rarer case where even that isn't enough, and are deliberately NOT
repair-hardened — see the safe-block contract below.

## The safe-block contract (`apply_extensions_safe_block`, `src/micro/code_fields.py`)

Unlike `lint_extensible`:
- **no pruning** of malformed content
- **no clamping/repair** of default values or method bodies — a default
  that PARSES ships exactly as authored, no matter how questionable
  (`sanitize_default` is never called here; see
  `test_no_clamping_unlike_lint_fields`)
- **no silent auto-rename** to dodge a name collision

Two things it DOES do, both a **loud abstain** — flagged, recorded, never a
silent drop or a guessed rewrite:

1. **Collision detection.** An unexcluded collision would silently corrupt
   the build (CS0102 duplicate member) rather than just fail to compile the
   author's own intent. A name colliding with the generator's reserved-field
   set, another schema field, a sibling `extra_field`/`extra_method`, or
   another entry in `extensions_fields`/`extensions_methods` itself excludes
   the **whole entry** from what renders.
2. **Default-syntax rejection.** A default that cannot possibly be valid C#
   — proven structurally via the same `_initializer_is_structurally_safe`
   check `sanitize_default` itself falls back on for `extra_fields`, not
   guessed — has no compilable intent left to preserve by shipping it
   verbatim: doing so would take the ENTIRE compose down at the final
   Roslyn syntax gate over one sandboxed field (`CSharpSyntaxError` aborts
   the whole project, not just this micro). Only the **default** is cleared;
   the field's name/type/header/tooltip survive untouched, exactly the way
   collision handling leaves everything but the colliding entry alone. This
   is strictly narrower than `sanitize_default`: it only ever REJECTS a
   default that cannot parse, and never rewrites one that can (`"2i"` on an
   `int` field still ships verbatim — repairing a parseable-but-wrong
   literal is `lint_fields`' job, not this one's). Live incident that
   motivated this: DropZone's `SaveSystem.cs`, an `extensions_fields` entry
   whose default was the raw string `"new Vector3({x: 0f, y: 0f, z: 0f})"`
   — a JSON object literal where positional constructor args belonged.

Both cases are recorded by name in `schema.meta["extensions_conflicts"]` —
never just dropped quietly.

`extensions_metadata` gets no processing at all — it's non-code, so there's
nothing to sandbox against.

## Migration path

When an extension field proves useful across runs, promote it out of the
sandbox in one pass: move it from `extensions_*` to a real named schema
field, and update the template, the fixer, and the prompt guide together —
the same "grows in step, together" discipline `docs/CONTROL_SURFACE_STACK.md`
uses for extending mode vocabularies.

## Rollout status

The three fields live on the shared `CodeExtensibleSchema` mixin
(`src/micro/code_fields.py`), so every schema that inherits it gets them for
free. `ControllerSchema` hand-rolls the same three fields directly (it
predates the mixin and hand-rolls `extra_*` too).

**Fully wired (schema + fixer safe-block + prompt guide + template render),
19 generators:**
`controller` (`Controller.cs.j2`, `PointerController.cs.j2` — NOT
`ControllerMultiMode.cs.j2`, which renders neither `extra_*` nor
`extensions_*` today), `ai`, `audio`, `behavior_tree`, `board`, `camera`,
`dialogue`, `health`, `hud`, `interactable`, `menu`, `npc_dialog`,
`objectives`, `physics`, `rules`, `saveload`, `spawner`, `transition`.

For these, `UNITY_EXTRA_INSTRUCTIONS` (the shared prompt-fragment constant
every one of them except `controller` interpolates into its `SCHEMA_PROMPT`)
carries the "if extra_\* isn't enough, put it in extensions" guidance —
`controller.py` carries its own copy since it doesn't use that constant.

**Schema-level only (inherits the fields via `CodeExtensibleSchema`, but its
generator does not call `apply_extensions_safe_block` and its template does
not render `extensions_fields`/`extensions_methods` — filling them today is a
no-op, not a build break, since nothing reads them yet):**
`animator`, `lighting`, `shader`, `statemachine`, `trigger`, `vfx` — these
don't currently call `lint_extensible` either (unclear why; not investigated
as part of this pass), so wiring them means adding the fixer call once that's
understood, then the template include.

**Not touched at all:**
- `src/micro/gta5/*.py` (spawner/waves/hud/menu/saveload) — inherit
  `CodeExtensibleSchema` but their generators don't call `lint_extensible`
  either; same gap as the six above, on the GTA5-flavored micro cohort.
- The top-level GTA5 mission pipeline (`pipeline.py`, `mission_schema.py`)
  has its own PARALLEL extensibility system (`GtaCodeExtensibleSchema`,
  `lint_gta_extensible` in `code_fields.py`) with different lifecycle names
  (`on_start_extra`/`on_tick_extra`/`on_abort_extra` instead of Unity's
  Awake/Update/FixedUpdate). Schema Extensions has no presence there yet —
  it would need its own `extensions_*` fields on `GtaCodeExtensibleSchema`
  and its own safe-block wiring, not a reuse of the Unity one.
- `terrain.py` and the custom-script path in `unity_generator.py`
  (`lint_extensible` call on `schema.scripts` entries) — a fully
  LLM-authored custom script is already the LLM's free space; Schema
  Extensions' narrower, collision-checked sandbox adds little there.

## Testing

`tests/test_schema_extensions.py` covers `apply_extensions_safe_block` in
isolation (no pruning, no clamping/rewriting of a PARSEABLE default, no
renaming, collision → loud abstain in `schema.meta`, an unparseable default →
loud abstain that clears only the default and keeps the field) plus
end-to-end runs through `controller` (hand-rolled) and `health`
(`CodeExtensibleSchema`-based) proving the fixer wiring and Jinja rendering
both fire, including a real C# syntax-gate pass.
