# Mechanic descriptor ownership audit (S3)

S3 validates structural connection and ownership. It does not migrate gameplay,
add public atom vocabulary, persist S4 bindings, or claim compile/runtime proof.

Run from the repository root:

```sh
python -m src.atoms.ownership_audit --write
python -m src.atoms.ownership_audit --check
python -m pytest -q tests/test_mechanic_ownership_audit.py
```

The derived report is
[mechanic_ownership_inventory.json](mechanic_ownership_inventory.json). It
inventories the current descriptor producer classes and traces a representative
set across schema atoms, entity attachment, Unity wiring, standalone companions,
and template-owned mechanics.

## Classifications

| State | Meaning |
| --- | --- |
| `connected_and_owned` | One provider, one owner, one generated host, and one invoked lifecycle/wiring seam are proven from source. |
| `connected_but_not_attached` | Source is emitted or lowerable, but generated attachment to a host/lifecycle is not proven. |
| `ambiguous_owner` | More than one candidate owner exists; the audit abstains and names every candidate plus the missing seam. |
| `missing_provider` | No declared provider exists; the existing debt entry supplies the later migration slice. |

Unknown routes are errors, not defaults. A provider with no owner candidate raises
an `OwnershipEvidenceError`; a changed or incomplete report fails validation.

## Representative result

- `UnitySchema.atoms` lowers into `GameSequencer.cs`, but no generated scene or
  prefab attaches `GameSequencer`; this is `connected_but_not_attached`.
- Behavior-map entity atoms resolve a behavior class, render `EntityBehavior`,
  attach through `EntitySpawner.AddComponent`, and enter its `Start` coroutine;
  this route is `connected_and_owned`.
- `WireInputToController` is selected by `MicroComposer`, lowered into
  `GameWiring`, attached on `_Systems`, and invoked from `Awake`; this route is
  `connected_and_owned`.
- `MICROGEN_ATOM_MAP["controller"]` can emit
  `ControllerDefaultBehavior.cs` through standalone `_after_write`, but composed
  generation calls `write_files` directly and no generated scene attaches that
  companion; this is `connected_but_not_attached`.
- The composed controller descriptor still resolves through `REGISTRY` to
  `ControllerPipeline`, renders selected template source, and attaches to the
  generated Player host; template ownership remains unchanged.
- Unity `Shoot` still has no atom lowerer and remains `missing_provider`, with S5
  as its reviewed migration slice. The controller/projectile templates remain the
  runtime owners until that later slice supplies independent evidence.

## Authority boundary

The report is derived from Python AST/source locations, live registries, backend
dispatch, and generated templates. It does not inspect prompt prose and invokes no
LLM. Controller, board, rules, and projectile ownership remains template-owned and
runtime acceptance remains `unverified`.
