# Slice 6 — Hybrid spatial layout engine

Status: **6.1 implemented as a frozen image-space evidence pass; 6.2
implemented as a deterministic closed schema-role pass; 6.3 implemented as a
unit-free hybrid fusion sidecar; 6.4 implemented as a coordinate-free
scene/relation graph sidecar; 6.5 implemented as the validated world-layout
consumer; 6.6 implemented as the deterministic spatial validation report and
wired into the main Unity generation pipeline.** Slice 6.7 spatial freeze
remains the next phase. The Unity world-layout consumer is intentionally not
part of 6.1 or 6.2.

Slice 6 solves the scattered-scene problem by keeping three authorities
separate:

| authority | owns | cannot own |
| --- | --- | --- |
| canonical board image | visible composition, normalized boxes, visual depth, visible surfaces, image relations | gameplay meaning, new entities, metres, colliders |
| Concept + schema | declared roles, objectives, route/interaction semantics, required entities | exact marketplace asset, pixel coordinates |
| frozen asset bindings | selected model bytes, provenance, measured footprint | gameplay roles, image interpretation |
| deterministic layout compiler | world anchors, adjacency realization, transforms, scene graph, validation | inventing semantic entities or calling an LLM |

The image is therefore a constraint on appearance, not a second semantic
schema. A fixed Concept, closed image IR, frozen visual evidence, frozen asset
bindings, catalog snapshot, and config must produce the same later layout.

## Revised sub-slices

### 6.1 — Image-space evidence extraction (implemented)

This is a Step-3 annotation boundary, not a world-coordinate generator.

Inputs:

- the canonical board image, hash-checked against Slice 3;
- the closed Slice-3 image IR, used as the exact label whitelist.

The Concept is linked transitively by the closed IR, but its prose is not sent
to this image annotation call. Frozen assets and schema roles are deliberately
not consumed here; they belong to later fusion. This keeps visual evidence
from turning into hidden gameplay input.

One constrained vision call writes:

- one normalized `[0,1]` bounding box for every closed visible label;
- optional normalized horizon and vanishing-point estimates;
- visual layer (`foreground`, `midground`, `background`, or `unknown`);
- visible surface (`ground`, `water`, `sky`, `air`, `indoor`, or `unknown`);
- facing and occlusion hints;
- pairwise visual relations (`left_of`, `above`, `near`, and the other closed
  evidence relations);
- unnamed visual clusters and confidence values;
- explicit uncertainties.

Deterministic validation rejects missing/extra labels, out-of-bounds boxes,
unknown enum values, contradictory relations, duplicate clusters, and malformed
confidence values. The validated result is frozen as:

```text
image_spatial_evidence.json
image_spatial_evidence.raw.txt
image_spatial.json             # provenance manifest
```

CLI:

```text
python main.py unity-vizconcept-spatial \
  --board-image rallyboatboard.png \
  --closed-ir rallyboat.v5.closed-image-ir.json \
  --output rallyboat.v5.image-spatial-evidence.json
```

The artifact is visual evidence only. It must not be read as Unity metres,
world-space Y, route order, reachability, collider size, or a parent/child
relationship.

### 6.2 — Concept/schema spatial roles (deterministic) · implemented

Derive roles from the existing schema, mission objectives, route/path fields,
locomotion, medium, and the Concept lineage already present in `schema.meta`.
Do not add an LLM call and do not add production schema fields. Produce a
closed role table such as `player_path`, `checkpoint`, `hazard`,
`navigation_marker`, `scenery`, `airborne_hazard`, or `ui_only`, with an
explicit abstention when a role cannot be proven from declared content.

The implementation is `src/unity_spatial_roles.py` and emits one row for every
declared `EntitySpec`, including the closed `abstain` role. It also emits
objective target bindings and named adjacency requirements, but never creates
an entity for an unbound `target_ref` and never emits world coordinates. The
schema is not mutated. Concept prose is only a lineage/evidence source after
the entity is already declared by the schema; it is not an inventory.

Artifacts are hash-linked as:

```text
<schema>.spatial-roles.json
<schema>.spatial-roles.manifest.json
```

Run it with:

```text
python main.py unity-vizconcept-spatial-roles \
  --schema rallyboat.schema.json \
  --output rallyboat.spatial-roles.json
```

The manifest records the exact schema hash, semantic schema hash, Concept and
brief lineage hashes, the closed role vocabulary, and any explicit
abstentions/warnings. Focused coverage is in
`tests/test_unity_spatial_roles.py`.

This stage owns gameplay adjacency requirements. For example, a checkpoint
objective may require a declared checkpoint entity to lie on the player route;
it may not manufacture one because the image contains an arch.

### 6.3 — Hybrid evidence fusion (deterministic)

Join 6.1 evidence to 6.2 roles using the existing closed visual binding and
schema-entity binding contracts. The fusion pass must:

1. match only declared entities;
2. convert image X/Y/scale ratios into normalized composition constraints;
3. preserve concept/schema constraints when image evidence conflicts;
4. use named defaults only when evidence is absent, with a warning;
5. abstain on ambiguous entity matches rather than silently swapping models.

The output is a solved, unit-free region/constraint IR. It is not yet a list
of Unity transforms.

The local implementation is `src/unity_spatial_fusion.py`. It validates the
closed Slice-3 inventory/entity matches, revalidates Slice-6.1 evidence,
requires the Slice-6.2 schema hash and complete role coverage, and emits the
canonical `hybrid-spatial-fusion/v1` artifact with a
`hybrid-spatial-fusion-manifest/v1` provenance record. Each bound reference
carries normalized composition, layer, surface, facing, and confidence
alongside the schema role and gameplay adjacency requirements. Image relations
are retained only as composition constraints. Missing evidence receives named
role defaults with warnings; ambiguous schema matches are rejected rather than
silently rebound.

CLI:

```text
python main.py unity-vizconcept-spatial-fuse \
  --schema rallyboat.schema.json \
  --closed-ir closed_image_ir.json \
  --evidence image_spatial_evidence.json \
  --evidence-manifest image_spatial.json \
  --roles rallyboat.spatial-roles.json \
  --roles-manifest rallyboat.spatial-roles.manifest.json \
  --output rallyboat.spatial-fusion.json
```

Focused coverage is in `tests/test_unity_spatial_fusion.py`.

### 6.4 — Scene and relation graph construction

Reuse the graph-first work described in `docs/SCENE_GRAPH_COMPILER.md`:
entities and scenes are nodes; gameplay and spatial relationships are closed
edges. Persist the relation stage before coordinates are realized. Do not
create parallel `AdjacencyGraph.json` and `AnchorGraph.json` dialects; the
scene graph and its relation view are the canonical artifacts.

The local implementation is `src/scene_graph.py` and is deliberately
coordinate-free. It emits one `scene-relation-graph/v1` artifact containing
project, scene, entity, mission, and objective nodes plus the closed
`parent_of`, `spawns`, `targets`, `triggers`, `requires`, `near`,
`on_surface`, `binds`, and `reaches` relations. Objective target edges resolve
only to declared entities; unresolved targets are named warnings and never
become invented nodes. Parent and requirement cycles, duplicate endpoints,
missing scene ancestors, and unresolved edge endpoints are strict errors.
The graph hash uses the shared `src/ident` substrate, and the writer emits a
hash-linked manifest plus an optional DOT inspection view.

CLI:

```text
python main.py unity-scenegraph rallyboat.schema.json \
  --spatial-roles rallyboat.spatial-roles.json --output rallyboat.scene-graph.json \
  --dot
```

Focused coverage is in `tests/test_scene_graph.py`. The main Unity pipeline
now consumes the in-memory graph before generation and persists the final
graph beside the generated project.

### 6.5 — World realization and Unity placement

Extend `src/unity_layout.py` and its existing archetype realizers. The solved
region/constraint IR selects placement behavior, while measured asset
footprints from the frozen binding select clearance and scale. Continue to
emit the existing `EntitySpec.origin_offset`, `group_radius`,
`instance_positions`, and `instance_yaws` contracts so
`templates/unity/EntitySpawner.cs.j2` remains the runtime consumer.

Authored Scene Compiler transforms remain Tier A and are never repacked by the
hybrid solver. Water, ground, air, and vertical-surface rules remain owned by
the existing layout archetypes; 6.5 supplies constraints to them rather than
duplicating their geometry rules.

The 6.5 implementation is `src/unity_spatial_realization.py`. It
validates optional measured-footprint input and an optional 6.4 relation graph,
checks their schema hashes, calls the existing deterministic
`compute_layout()`/archetype path, verifies that authored Scene Compiler
transforms survive, and emits a hash-linked world-layout artifact. The output
records the existing placement contracts (`origin_offset`, `group_radius`,
`elevation`, `instance_positions`, `instance_yaws`, and `path_points`) rather
than inventing a parallel transform schema. It consumes image constraints when
the optional 6.3 artifacts are supplied; those normalized composition
constraints never become gameplay semantics.

CLI:

```text
python main.py unity-spatial-realize rallyboat.schema.json \
  --relation-graph rallyboat.scene-graph.json \
  --footprints rallyboat.footprints.json \
  --output rallyboat.world-layout.json \
  --manifest rallyboat.world-layout.manifest.json
```

The 6.5 consumer now accepts `--spatial-fusion`. After the existing
archetype realizes world geometry, normalized composition centers are mapped
into the realized envelope only for non-authored `scenery` groups. Player,
route, checkpoint, hazard, navigation, preplaced, and authored Scene Compiler
groups remain protected by schema/archetype authority. Candidate moves that
would overlap a realized group are named abstentions. This keeps image
composition useful without allowing it to rewrite gameplay placement.

For `game_type="2d_topdown"`, the same validated fusion rows now carry their
normalized `bbox` rectangles into the deterministic board primitive before
keyword anchors are considered. The rectangle center maps to the fixed board
envelope and its compact extent reserves a bounded group footprint. Authored
Scene Compiler transforms still remain Tier A, while the legacy semantic
anchors remain the fallback when no closed image rectangle exists.

Focused coverage is in `tests/test_unity_spatial_realization.py` and
`tests/test_unity_spatial_pipeline.py`.

### 6.6 — Spatial validation

Run before generation and again after model-aware footprint realization. Check:

- every declared gameplay entity has one resolved spatial role;
- every fused image reference binds to one declared schema entity;
- group and instance footprints do not overlap beyond the configured margin;
- surface/layer restrictions are satisfied;
- authored route/checkpoint/adjacency constraints are satisfied;
- entities are inside terrain/volume bounds;
- required player-to-objective paths remain reachable where the archetype can
  prove reachability;
- no stale Step-5 consumer uses positions from an earlier realization.

Failures are named `[spatial.strict]` rejections with a remedy. Visual
uncertainty remains a warning unless it makes a required binding ambiguous.

The 6.6 implementation is `src/unity_spatial_validation.py`. It
validates schema/role/fusion/world-layout hashes, entity-role coverage, group
and within-group footprint overlap, realized world bounds, required route
adjacency, airborne elevation restrictions, and 2-D reachability where the
archetype exposes that proof domain. It writes a hash-linked
`spatial-validation/v1` report and never emits or mutates world coordinates.

CLI:

```text
python main.py unity-spatial-validate \
  --schema rallyboat.schema.json \
  --world-layout rallyboat.world-layout.json \
  --spatial-roles rallyboat.spatial-roles.json \
  --spatial-fusion rallyboat.spatial-fusion.json \
  --output rallyboat.spatial-validation.json
```

The main Unity pipeline runs the validator after the final schema-side
coverage pass and freezes it again after generator model-aware relayout. The
generated project receives `<Project>.spatial-roles.json`,
`<Project>.scene-graph.json`, optional `<Project>.spatial-fusion.json`,
`<Project>.world-layout.json`, and `<Project>.spatial-validation.json`, each
with a neighboring manifest. `unity-vizconcept-compile` accepts
`--image-evidence` to activate the 6.3 path.

Focused coverage is in `tests/test_unity_spatial_validation.py` and
`tests/test_unity_spatial_pipeline.py`.

### 6.6a — Visual-comparison gate

The PlayMode screenshot is judged by `src/unity_screenshot_diff.py`, not by an
image model. It consumes only bound normalized rectangles from the closed
Slice-6.3 spatial-fusion sidecar and gates three deterministic signals:

- mean landmark centroid error, maximum `0.12` in normalized image space;
- foreground occupancy IoU, minimum `0.45`;
- coarse RGB palette cosine similarity, minimum `0.75`.

The Unity capture fixture temporarily disables recognizable Canvas/UI and
atmosphere/volume behaviours, fog, the UI layer, and camera post-processing;
all prior state is restored after capture. This keeps HUD and atmosphere
residue out of the scene-only comparison without changing gameplay checks.

Run the gate with:

```text
python unity_cli.py playtest output/unity/Project \
  --reference-image reference.png \
  --spatial-fusion Project.spatial-fusion.json
```

`e2e-verify --reference-image` enables its PlayMode phase automatically. If
`--spatial-fusion` is omitted, the isolated project must contain exactly one
`*.spatial-fusion.json` sidecar; otherwise the comparison fails loudly.

### 6.7 — Spatial freeze

`src/unity_spatial_freeze.py` implements the accepted-build gate. It refuses
to overwrite an existing freeze and replays Slice 6.6 before writing the
complete deterministic inputs and outputs together:

- image evidence hash and manifest;
- closed Concept/inventory lineage;
- schema-role and relation hashes;
- frozen asset-binding/catalog hashes;
- solved layout hash;
- measured footprints and transforms;
- spatial validation report.

CLI:

```text
python main.py unity-spatial-freeze \
  --project-root output/unity/Build/Game \
  --source-image F:/Downloads/plane.png \
  --reasoning-manifest output/visual_reasoning/run/reasoning.json \
  --inventory output/visual_reasoning/run/visual_inventory.json \
  --concept output/visual_reasoning/run/Concept.txt \
  --closed-ir output/visual_reasoning/run/closed_image_ir.json \
  --reconciliation-manifest output/visual_reasoning/run/reconciliation.json \
  --image-evidence output/visual_reasoning/run/image_spatial_evidence.json \
  --image-evidence-manifest output/visual_reasoning/run/image_spatial_evidence.manifest.json \
  --asset-fulfilment output/visual_reasoning/run/asset_fulfilment.json \
  --asset-manifest output/visual_reasoning/run/asset_fulfilment.manifest.json \
  --compile-manifest output/unity/Build/visual_compile.manifest.json
```

The freeze is a build sidecar, not a new schema authority. It contains a
content-hash lineage plus a transform snapshot, and it will not accept a
rejected asset result, stale neighboring manifest, or non-passing 6.6 report.
Changing the board, Concept, asset catalog, or config therefore produces a
new lineage instead of mutating an old freeze.

## Why this ordering produces the best output

Image-only layout would preserve composition but could place a visual prop on
the route as if it were a gameplay objective. Concept-only layout would stay
playable but lose the board's visual arrangement. Hybrid fusion gives the
image control over relative appearance while the declared schema controls
meaning, route legality, and playability. The deterministic solver is the
only stage allowed to turn those constraints into Unity geometry.

## Slice completion rule

No Slice-6 sub-slice is complete until its artifacts are hash-linked, focused
tests pass, and the change is opened as a GitHub Pull Request. Open a GitHub
Pull Request immediately; no feature is considered complete until it is PR'd.
