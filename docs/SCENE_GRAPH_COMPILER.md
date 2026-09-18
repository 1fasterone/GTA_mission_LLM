# Scene-graph compiler (plan)

Status: **Phase 0 built** (`src/ident/`); Tracks A-C planned. This document is
the roadmap for three coupled pieces of work, and the tracker for which slice
has landed:

1. a deterministic **scene-graph compiler** — micro-types extended to a full
   scene DAG;
2. **deterministic asset hashing** shared by the Unity, Blender and GTA5
   backends;
3. **graph-first layout generation** — solve topology, then realize geometry.

They are one project, in that order: the hashing substrate supplies node and
asset identity, and the DAG is what the layout solver consumes. Everything here
lands in Steps 4-7 and therefore stays **LLM-free and deterministic** — no step
below may call the Pipeline LLM.

---

## Where the repo stood before Phase 0

| goal | today | gap |
| --- | --- | --- |
| scene DAG | no hierarchy anywhere. `EntitySpec` (`src/unity_schema.py`) is a flat list of <=40 groups with baked `instance_positions`. The only real graphs are `WiringMap.dependencies` (LLM-authored, advisory), `BTNode.children`, `DialogGraph`, and the hard-coded `SEAM N` blocks in `src/unity_compose.py` | no node identity, no parenting, no acyclicity gate, no single inspectable artifact |
| asset hashing | four digests with four canonicalizations: md5 (`src/blender/cache.py`, `unity_generator._make_guid`), sha1 (`src/unity_entity_asset_resolver.py`, `src/unity_sceneinput.py`), sha256 (`BlenderCache.fingerprint`, `src/unity_terraingen.py`), blake2b (`src/vstack/canon.py`) | no cross-backend asset identity; GTA5 has none at all; raw floats enter some hashed payloads |
| graph-first layout | `compute_layout()` (`src/unity_layout.py`) goes from schema text straight to circle packing; `_regions_for()` *describes* what packing already did | geometry-first, no coordinate-free solve stage, and layout is model-blind (it runs before asset resolution) |

**The substrate already exists and is unused.** `src/vstack/canon.py` is the
correct hashing kernel — blake2b, domain-separated `chash(tag, payload)`,
float-*rejecting*, `Decimal`-quantized, sorted keys, with no paths or wall-clock
in any digest. It is imported by exactly one file, `tests/test_e2e_0001.py`.
Phase 0 is promoting it, not writing it.

---

## Phase 0 — one identity substrate (`src/ident/`)

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

## Track A — deterministic asset hashing (Unity / Blender / GTA5)

### A1 — `AssetIdentity` (`src/ident/asset.py`)

`id = chash("asset", payload)` over:

| field | meaning |
| --- | --- |
| `bin_hash` | blake2b of the file's bytes, or `None` |
| `recipe_hash` | how the asset was produced (see A2) |
| `engine_owned` | `True` for GTA5 stock assets — an explicit state, never a zero hash |
| `unit_scale_q` | quantized via `canon.q` |
| `bounds_q` | quantized, from `src/unity_model_measure.py` |
| `source_tier` | `A` \| `S` \| `1` \| `2` \| `fallback` |

### A2 — one adapter per producer

- **Unity catalog / Sketchfab** — `src/unity_entity_asset_resolver.py`, tiers
  A/S/1/2.
- **Blender** — wrap the existing `BlenderCache.fingerprint` /
  `geo_fingerprint` as `recipe_hash`. Rename nothing; those fingerprints are
  already correct and already cache-keyed.
- **GTA5** — `src/resolver.py` + `src/gta_allowlists.py`. The lookup id *is* the
  recipe; `bin_hash=None`, `engine_owned=True`.

### A3 — `assets.lock.json`

One per build, sorted by asset id: tier, source, `bin_hash`, measured bounds,
and which entities bound it. This becomes the machine-readable record;
`schema.meta.entity_model_paths` and `schema.meta.authored_models` stay as views
over it.

### A4 — re-key the machine-bound keys

Two digests hash an **absolute path**, so their result differs across machines
and worktrees. Move both to asset ids (`frozen.py` marks them `migrate`):

- `src/unity_sceneinput.py` `_cache_key` — the GLB-to-FBX conversion cache,
  keyed on resolved path + size + mtime;
- `src/unity_entity_asset_resolver.py` authored-file disambiguation tag — sha1
  of the resolved absolute path, which lands in an **emitted** Resources file
  name whenever two authored files share a stem. That is a cross-machine output
  difference, not just a cache miss.

Model measurement (cached into the catalog entry) and the semantic match cache
(`catalog_content_hash`, over catalog-relative paths) are already
machine-stable and stay as they are.

### A5 — gate (`tests/test_asset_identity.py`)

- the same file at two different paths gives the same id;
- one flipped byte gives a different id;
- a raw float in a payload raises `TypeError`;
- a GTA5 engine-owned asset gets no fabricated `bin_hash`;
- two processes under differing `PYTHONHASHSEED` produce identical ids.

---

## Track B — the scene DAG

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
| `templates/unity/EntitySpawner.cs.j2` | emits real Unity parenting instead of a flat spawn list |
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

### C1 — split `compute_layout()` into three phases

| phase | input | output | coordinates? |
| --- | --- | --- | --- |
| 1 relate | Track B graph + route zones | constraint graph (`near`, `on_surface`, ordinal along route) | none |
| 2 solve | constraint graph | a region assignment per node (tier, ordinal, cell) | none |
| 3 realize | regions + **measured asset bounds** | `instance_positions` / `instance_yaws` | yes |

### C2 — the primitives become realizers

`_layout_arena_plane`, `_layout_vertical_corridor`, `_layout_bounded_volume`,
`_layout_vertical_surface` and `_layout_ui_board` consume a solved assignment
instead of re-deriving intent from schema text. `select_archetype()`
(`src/unity_layout_archetype.py`) is untouched — it already *is* the axis
derivation phase 1 wants.

### C3 — generative regions

Promote `WorldRegion` from descriptive to generative: solved in phase 2, packed
into in phase 3. This inverts today's `_regions_for()`.

### C4 — fix the model-blind clump

Insert a **measure-only pre-pass** between phases 2 and 3, cache-hit by Track A
asset id, so phase 3 packs with real footprints. Moving full asset resolution
ahead of layout is the riskier change and is not needed for this.

### C5 — reseed

Every `random.Random(f"{name}:{count}:{spacing:.3f}")` becomes
`random.Random(chash("layout.scatter", node_id))` — stable under renames, which
the current string seed is not. (Registers the `layout.scatter` tag.)

### C6 — gate

One baseline per archetype, extending `tests/fixtures/arenaplane_baseline.json`;
plus a test that phase 3 is a pure function of (graph hash, asset bounds) and
reads no schema prose.

---

## PR slicing

No slice is complete until it is PR'd.

| # | scope | risk | landed |
| --- | --- | --- | --- |
| 1 | Phase 0: `src/ident/`, tags, frozen list, lint test | low | [x] |
| 2 | A1-A2: `AssetIdentity` + three adapters | low | [ ] |
| 3 | A3 `assets.lock.json` + A5 gate | low | [ ] |
| 4 | A4 cache re-keying | med (one-time cache rebuild) | [ ] |
| 5 | B1-B4 + B6: IR, derivation, strict gate, artifacts, CLI | med | [ ] |
| 6 | B5: playability + reachability on the graph | med | [ ] |
| 7 | B5: `EntitySpawner` real parenting | **high** (changes emitted scenes) | [ ] |
| 8 | C1-C2: phase split, output byte-identical | med | [ ] |
| 9 | C3-C4: generative regions + measure pre-pass | **high** (baselines churn) | [ ] |
| 10 | C5 reseed + baseline regeneration | med, mechanical | [ ] |

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

- `docs/LAYOUT_ARCHETYPES.md` — the five spatial primitives Track C rebuilds on
- `docs/WIRING_SEAMS.md` — the seams that become `binds` edges
- `docs/model-pipeline-architecture.md` — the asset chain Track A identifies
- `docs/COMPILER_AUDIT.md` — the standing gap list
