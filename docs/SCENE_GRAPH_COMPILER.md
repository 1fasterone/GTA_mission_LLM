# Scene-graph compiler (plan)

Status: **Phase 0 built** (`src/ident/`); Tracks A-C planned, re-scoped after
checking every premise against the code (below). This document is the roadmap
for three coupled pieces of work, and the tracker for which slice has landed:

1. a deterministic **scene-graph compiler** — a scene DAG derived from the
   schema;
2. **one asset identity** shared by the Unity, Blender and GTA5 backends;
3. **graph-first layout generation** — solve relations, then realize geometry.

The hashing substrate supplies node and asset identity, and the DAG is what the
layout solver consumes. Everything here lands in Steps 4-7 and therefore stays
**LLM-free and deterministic** — no step below may call the Pipeline LLM.

---

## Premises checked against the code

The three goals arrived with framing attached. Each claim was checked against
the code; where the framing was wrong, the plan follows the code.

| stated premise | what the code shows | verdict |
| --- | --- | --- |
| "micro-types → extend to a full scene DAG" | micro-types (`src/micro/__init__.py` `REGISTRY`, 31 kinds) are **component** generators. Each composed unit attaches to one of five fixed targets (`ComposedUnit.target`: player / camera / ui / systems / world, `src/unity_compose.py`). There are no nodes or hierarchy in them to extend. Scene objects come from `EntitySpec` + `scene_specs` | **wrong framing.** The DAG's nodes come from entities and scenes; micros are components ON nodes; SEAMs are edges between components |
| asset hashing is not yet deterministic across Unity / Blender / GTA5 | **GTA5** has no asset files: `ModelResolver.resolve` (`src/resolver.py`) returns a lowercase model name validated by `^[a-z][a-z0-9_]+$` — the name already is a deterministic id. **Blender** FBX is already byte-deterministic (constant `CreationTime`, `src/blender/normalize.py`) and already content-addressed (`BlenderCache.fingerprint` / `geo_fingerprint`). **Unity** picks are already deterministic (`_stable_seed` uses hashlib, not `hash()`) | **mostly wrong.** Determinism already holds per machine. Two digests hash an absolute path (see A4); only one of them changes emitted output, and only when two authored files share a stem. What is genuinely missing is a single id FORMAT — which only matters once something consumes it (Track B `asset_ref`) |
| layout must become deterministic | string-seeded `random.Random(f"{entity.name}:{n}:{eff_spacing:.3f}")` hashes the string with sha512, independent of `PYTHONHASHSEED` (verified); `compute_layout` is documented idempotent; `tests/test_unity_layout_archetype.py` locks ArenaPlane positions byte-for-byte against `tests/fixtures/arenaplane_baseline.json` | **already true** |
| layout is geometry-first — "start with graphs, not geometry" | two abstract stages already run BEFORE any coordinate: `select_archetype()` (axes → primitive) and `spatial_relationship_of()` (`src/unity_layout_archetype.py`), which classifies every group's relation to the primary route — `route_gate`, `edge_distributed`, `boundary_follow`, `formation`, `scenery_distributed` — and dispatches a packer per relation | **partly wrong.** A relation stage exists, but it is a STAR: every group relates only to one primary route. There are no group-to-group relations, and the classification is not persisted |

---

## Where the repo stood before Phase 0

| goal | before | gap |
| --- | --- | --- |
| scene DAG | the IR is flat: `EntitySpec` (`src/unity_schema.py`) is a list of <=40 groups with baked `instance_positions`. A runtime hierarchy DOES exist, fixed at two levels and implicit in `templates/unity/EntitySpawner.cs.j2`: spawner → one container per group → instances (plus `Waypoints` containers). Other graphs: `WiringMap.dependencies` (LLM-authored, advisory), `BTNode.children`, `DialogGraph`, the hard-coded `SEAM N` blocks in `src/unity_compose.py` | no node identity, no depth beyond group → instance (a prop cannot sit ON another group), no acyclicity gate, no single inspectable artifact |
| asset identity | four digests with four canonicalizations: md5 (`src/blender/cache.py`, `unity_generator._make_guid`), sha1 (`src/unity_entity_asset_resolver.py`, `src/unity_sceneinput.py`), sha256 (`BlenderCache.fingerprint`, `src/unity_terraingen.py`), blake2b (`src/vstack/canon.py`) — each deterministic on its own | no shared id format; two absolute-path digests (A4) |
| graph-first layout | `select_archetype()` + `spatial_relationship_of()` run before packing; `_regions_for()` *describes* what packing did; Step 6 re-runs `compute_layout` with measured catalog footprints (`unity_generator.py`, "Model-aware re-layout") | relations are a star around one route; regions are descriptive, not generative; Step 5 consumers see the pre-re-layout bake (C4) |

**The substrate already existed and was unused.** `src/vstack/canon.py` was the
correct hashing kernel — blake2b, domain-separated `chash(tag, payload)`,
float-*rejecting*, `Decimal`-quantized, sorted keys, with no paths or wall-clock
in any digest. It was imported by exactly one file, `tests/test_e2e_0001.py`.
Phase 0 promoted it rather than writing a new one.

---

## Phase 0 — one identity substrate (`src/ident/`) · built

1. Move `src/vstack/canon.py` to `src/ident/canon.py`; `vstack` re-imports from
   the new home so `tests/test_e2e_0001.py` stays green untouched.
2. Add `fhash(path)` (blake2b over a file's **exact bytes**) and `HashRef`
   (tag + hex + 8-char display form).
3. `src/ident/tags.py` — the closed domain-tag set: `asset`, `asset.bin`,
   `asset.recipe`, `node`, `scene`, `layout`, `wiring`, `build`, plus the vstack
   reference kernel's own `v1.*`-`v5.*` spec tags as a separate namespace.
   `chash()` refuses an unregistered tag. Same doctrine as atoms: a tag is never
   invented inline at a call site; a new one is added to the registry in the
   same PR as its first caller.
4. `src/ident/frozen.py` — the **inventory of every digest outside
   `src/ident/`**, one row per enclosing function: how many `hashlib` calls it
   holds, a disposition, and why. Two dispositions:
   - `keep` — must NOT be unified. Contracts: `src/unity_package_cache.py`'s
     sha1 is the npm registry's own digest; `unity_generator._make_guid` /
     `_make_uuid` md5 is a compatibility contract with every already-generated
     Unity project (pinned as `GUID_SCHEME = 1` with a known-output test).
     Output seeds: the audio / sprite / Blender / terrain / asset-rotation seed
     derivations — changing their digest changes every generated artifact.
     Wrapped: the `BlenderCache` fingerprints, which Track A wraps rather than
     replaces.
   - `migrate` — retired onto `src/ident/` by a named slice (below).
5. Lint test (`src/ident/lint.py`, AST like `src/naming_lint.py`): the actual
   `hashlib` call count per enclosing function in `src/` and `cli/` must equal
   the inventory exactly — a new digest anywhere fails, and so does a stale row.
   Covers every `hashlib` callable, including aliased and `from` imports, not
   just md5/sha1.

---

## Track A — one asset identity (Unity / Blender / GTA5)

Per-machine determinism already holds (see Premises). Track A is therefore NOT
a determinism fix, except A4. A1-A3 exist to give Track B's `asset_ref` one id
format, so they are worth building only together with Track B; A4 stands on
its own.

### A1 — `AssetIdentity` (`src/ident/asset.py`)

The id must be derivable BEFORE measurement, because measured bounds are
looked up by it — so bounds, scale and resolver tier are resolution facts
recorded next to the id (A3), never inside it.

| producer | id derived from | why |
| --- | --- | --- |
| a found file (Kenney / catalog, Sketchfab download, authored layout model) | `fhash` of its bytes | the bytes are the input; the same file at two paths is one asset |
| a Blender-produced model | the wrapped `BlenderCache.fingerprint` (recipe) | the recipe is the identity; its FBX bytes follow from it deterministically |
| a GTA5 model | the canonical model name | engine-owned: no bytes exist in this tree — an explicit state, never a zero hash |

### A2 — one adapter per producer

- **Unity** — `ResolvedModel` (`src/unity_model_resolver.py`) from
  `src/unity_entity_asset_resolver.py`. Non-file routes in `entity_model_paths`
  (`fx:`, `skip:`, `ui:`, schema procedural meshes) are routes, not assets.
- **Blender** — wrap `BlenderCache.fingerprint` as the recipe. Rename nothing.
- **GTA5** — `ModelResolver.resolve` output. `UNRESOLVED` is an abstain, never
  an identity.

### A3 — `assets.lock.json`

One per build, sorted by asset id: tier, source, measured bounds, and which
entities bound it. `schema.meta.entity_model_paths` and
`schema.meta.authored_models` stay as views over it.

### A4 — re-key the machine-bound keys

Two digests hashed an **absolute path**. Both now derive from content via
`src/ident` (built; `frozen.py` has no open `migrate` rows):

- `src/unity_entity_asset_resolver.py` `_register_authored` — sha1 of the
  resolved absolute path lands in an **emitted** Resources file name whenever
  two authored files share a stem. A real cross-machine output difference,
  though only in that collision case.
- `src/unity_sceneinput.py` `_cache_key` — the GLB-to-FBX conversion cache,
  keyed on absolute path + size + mtime. A cache miss across machines, not
  wrong output.

Model measurement (cached into the catalog entry) and the semantic match cache
(`catalog_content_hash`, over catalog-relative paths) are already
machine-stable and stay as they are.

### A5 — gate (`tests/test_asset_identity.py`)

- the same file at two different paths gives the same id;
- one flipped byte gives a different id;
- a GTA5 model gets no fabricated byte hash;
- two processes under differing `PYTHONHASHSEED` produce identical ids.

---

## Track B — the scene DAG

Nodes are entities and scenes, not micro-types (see Premises). Micros attach to
nodes as components; SEAMs are `binds` edges between those components.

### B1 — IR (`src/scene_graph.py`)

```
Node{ id, kind, name, parent, transform_q, asset_ref, tag, source }
Edge{ src, dst, rel }
```

`rel` is a **closed** vocabulary: `parent_of`, `spawns`, `targets`, `triggers`,
`requires`, `near`, `on_surface`, `binds`, `reaches`. Closed exactly like the
atom vocabulary — inventing a rel is a hard error, and extending the set is a
one-pass change to graph, validators and consumers together.

### B2 — derived, never authored

`build_scene_graph(schema)` is a Step 5 pass, LLM-free, slotted after
`resolve_objective_targets` and **before** `compute_layout`
(`src/unity_pipeline.py`). Sources, in precedence order:

1. the authored scene (`src/unity_scenebind.py`) — the author's hand outranks
   everything, as it already does for assets and transforms;
2. `scene_specs`;
3. `entities`;
4. `missions` / objectives (`targets`, `requires`);
5. SEAM bindings from `src/unity_compose.py`;
6. `behavior_trees` / dialog graphs.

### B3 — Merkle node ids

A node's id hashes its own canonical payload **plus its parent's id**. An
unchanged subtree keeps its id, which buys incremental rebuild and a stable
per-node GUID.

### B4 — strict validation, loud abstain

`[graph.strict]` errors with explicit remedies, the same shape as
`LayoutStrictError`'s `[layout.strict]`:

- acyclic on `parent_of` and `requires`;
- a single root;
- every `asset_ref` present in `assets.lock.json`;
- every edge endpoint resolvable;
- every entity has a scene ancestor.

### B5 — migrate consumers one at a time

Each moves behind a read-only adapter, so the existing flat path stays
byte-identical until the flip:

| consumer | after |
| --- | --- |
| `src/unity_playability.py` | its checks become graph queries; `entity_scene_coverage` is literally "has a scene ancestor" |
| `src/unity_reachability.py` | occupancy grid built from graph nodes, not `schema.entities` |
| `templates/unity/EntitySpawner.cs.j2` | already parents instances under a per-group container; gains variable depth from `parent_of` edges (a group placed ON another group) |
| `src/unity_compose.py` | SEAM results recorded as `binds` edges; the wiring report becomes a graph diff |

### B6 — artifacts

A `scene_graph.json` sidecar plus `schema.meta["scene_graph"] = {hash, nodes}` —
`meta` is the existing pattern (`cover_points`, `authored_models`), so no new
schema fields are invented. CLI:

```
python main.py unity-scenegraph <schema.json> [--dot]
```

### B7 — gate

A golden graph per archetype; determinism across two processes; and a test
asserting `src/scene_graph.py` imports no LLM client — the "Steps 4-7 are
LLM-free" rule made executable rather than documented.

---

## Track C — graph-first layout

Builds on the relation stage that already exists; it does not replace it.

### C1 — persist and generalize the relation stage

| phase | input | output | coordinates? |
| --- | --- | --- | --- |
| 1 relate | `spatial_relationship_of()` (today's star around the primary route) + Track B `near` / `on_surface` edges | a persisted relation graph, including group-to-group relations | none |
| 2 solve | relation graph | a region assignment per node (tier, ordinal, cell) | none |
| 3 realize | regions + measured footprints | `instance_positions` / `instance_yaws` | yes |

### C2 — the primitives become realizers

`_layout_arena_plane`, `_layout_vertical_corridor`, `_layout_bounded_volume`,
`_layout_vertical_surface` and `_layout_ui_board` consume a solved assignment
instead of re-deriving intent. `select_archetype()` and
`spatial_relationship_of()` (`src/unity_layout_archetype.py`) stay — they
already are phase 1's classifiers.

### C3 — generative regions

Promote `WorldRegion` from descriptive to generative: solved in phase 2, packed
into in phase 3. This inverts today's `_regions_for()`.

### C4 — Step 5 layout consumers go stale after the Step 6 re-layout

The model-blind clump is **already fixed**: `unity_generator.py` re-runs
`compute_layout` with measured catalog footprints after model resolution. But
that re-layout runs inside `generator.write()`, AFTER Step 5 consumers of the
first bake have already been used:

- `compute_cover_points` (`src/unity_pipeline.py`, Step 5d) bakes cover points
  from the Step 5 obstacle positions;
- `subsystems_for(..., cover_points=schema.meta["cover_points"])` hands them to
  the `cover_shooter` AI micro before `generator.write()` runs;
- the re-layout then moves scatter / grid / ring / line groups that have
  measured bounds — the obstacles included.

So whenever catalog bounds exist for an obstacle group, the AI ships cover
coordinates next to where the obstacles USED to be. Fix: one layout, sized
once — either measure footprints before Step 5 (the lookup needs only the
catalog pick, not a copied binary) or recompute cover points after the
re-layout, before AI codegen.

### C5 — gate

One baseline per archetype, extending `tests/fixtures/arenaplane_baseline.json`;
plus a test that phase 3 is a pure function of (relation graph, footprints)
and reads no schema prose.

(Dropped: the earlier "reseed layout RNG by node id" slice. The string seed is
already deterministic across processes, and a node id that hashes the name
would be no more stable under a rename.)

---

## PR slicing

No slice is complete until it is PR'd.

| # | scope | depends on | risk | landed |
| --- | --- | --- | --- | --- |
| 1 | Phase 0: `src/ident/`, tags, frozen list, lint test | — | low | [x] |
| 2 | C4: stale cover points after the Step 6 re-layout | — | med | [ ] |
| 3 | A4: re-key the two absolute-path digests | 1 | low | [x] |
| 4 | A1-A2 + A5: `AssetIdentity` + three adapters | 1, only with 6 | low | [ ] |
| 5 | A3: `assets.lock.json` | 4 | low | [ ] |
| 6 | B1-B4 + B6: IR, derivation, strict gate, artifacts, CLI | 5 | med | [ ] |
| 7 | B5: playability + reachability on the graph | 6 | med | [ ] |
| 8 | B5: variable-depth `EntitySpawner` parenting | 6 | **high** (changes emitted scenes) | [ ] |
| 9 | C1-C2: persisted relation stage + realizer split, output byte-identical | 6 | med | [ ] |
| 10 | C3: generative regions | 9 | **high** (baselines churn) | [ ] |

Slices 2 and 3 fix real defects and stand alone. Slices 4-10 are one
investment: they pay off only if the scene DAG is built.

Baseline regeneration always lands in its own commit with the diff explained,
never mixed with logic changes.

---

## Hazards

1. **Do not unify `_make_guid`.** md5-derived Unity GUIDs are a contract with
   every already-generated project. Version the scheme instead.
2. **Never rewrite asset bytes to stabilize a hash.** Rewriting FBX bytes was
   the root cause of every past Unity `ImportFBX` failure; `normalize_fbx` is a
   deliberate no-op. Hash bytes as found.
3. `src/unity_package_cache.py`'s sha1 is the npm registry's digest. Excluded by
   design, recorded in `src/ident/frozen.py`.
4. No builtin `hash()`, and no dict/set iteration order, inside any hashed
   payload. `canon.py` already enforces the float half of this.
5. **The graph must stay derived.** An LLM-authored scene graph would blur
   held-out natural-language briefs into production structured input and
   dissolve the Step 3 evals.

---

## See also

- `docs/LAYOUT_ARCHETYPES.md` — the five spatial primitives Track C builds on
- `docs/WIRING_SEAMS.md` — the seams that become `binds` edges
- `docs/model-pipeline-architecture.md` — the asset chain Track A identifies
- `docs/COMPILER_AUDIT.md` — the standing gap list
