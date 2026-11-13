# Mechanic descriptor ownership audit (S3, R2, R3)

S3 validates structural connection and ownership. It does not migrate gameplay,
add public atom vocabulary, or claim compile/runtime proof. R2 (S4) attached or
retired the two unattached routes, and persists per-build typed atom-route
bindings (see "Typed atom bindings" in `ATOM_BACKEND_SUPPORT.md`).

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
| `retired` | The route was removed on purpose. No provider emits it, and `retired_by` names the source that proves it (R2). |

Unknown routes are errors, not defaults. A provider with no owner candidate raises
an `OwnershipEvidenceError`; a changed or incomplete report fails validation.
Since R2, `connected_but_not_attached` also fails validation: an emitted route
must be attached to a generated host or retired. Since R3, `missing_provider`
fails validation too: every representative route has a provider.

## Representative result

- `UnitySchema.atoms` lowers into `GameSequencer.cs`, which every gameplay scene
  hosts on its own `_GameSequencer` object (R2). The generator places that host
  from the typed binding it persists in `schema.meta.atom_bindings`
  (`src/atom_bindings.py`); this route is `connected_and_owned`.
- Behavior-map entity atoms resolve a behavior class, render `EntityBehavior`,
  attach through `EntitySpawner.AddComponent`, and enter its `Start` coroutine;
  this route is `connected_and_owned`.
- `WireInputToController` is selected by `MicroComposer`, lowered into
  `GameWiring`, attached on `_Systems`, and invoked from `Awake`; this route is
  `connected_and_owned`.
- `MICROGEN_ATOM_MAP["controller"]` used to emit `ControllerDefaultBehavior.cs`
  through standalone `_after_write`, which no generated scene ever attached. R2
  retired every Unity companion: `UnityMicroPipeline._after_write` makes no
  call, and the audit checks that structurally for every registered Unity micro.
  A hook that writes a companion again reopens the route and fails. This route
  is `retired`; `MICROGEN_ATOM_MAP` stays descriptive data.
- The composed controller descriptor still resolves through `REGISTRY` to
  `ControllerPipeline`, renders selected template source, and attaches to the
  generated Player host; template ownership remains unchanged.
- Unity `Shoot` lowers to `FiringProviders.FirePlayer()`, which calls
  `IFiringProvider.TryFire` on the Player: the same gate the controller's
  `Update` fire input calls (R3, S5). The audit proves each link from source
  (the lowered C#, the contract template, the controller's single `TryShoot()`
  call site, the micro that emits the contract, the Player tag) and reads the
  owner from the template class that implements the contract. Its mode is
  `shared_firing_provider` and the route is `connected_and_owned` by
  `ControllerPipeline`. A lowerer that bypasses the provider loses its seam and
  fails validation. A second implementer makes the owner ambiguous rather than
  picked. The controller/projectile templates remain the runtime owners.

## Authority boundary

The report is derived from Python AST/source locations, live registries, backend
dispatch, and generated templates. It does not inspect prompt prose and invokes no
LLM. Controller, board, rules, and projectile ownership remains template-owned and
runtime acceptance remains `unverified`.
