# Slice 6 — Hybrid spatial layout engine

Status: **6.1 implemented as a frozen image-space evidence pass; 6.2
implemented as a deterministic closed schema-role pass; 6.3 implemented as a
unit-free hybrid evidence-fusion pass.** Unity world-layout realization is
intentionally not part of 6.3 yet.

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

### 6.3 — Hybrid evidence fusion (deterministic) · implemented

Join 6.1 evidence to 6.2 roles using the existing closed visual binding and
schema-entity binding contracts. The fusion pass must:

1. match only declared entities;
2. convert image X/Y/scale ratios into normalized composition constraints;
3. preserve concept/schema constraints when image evidence conflicts;
4. use named defaults only when evidence is absent, with a warning;
5. abstain on ambiguous entity matches rather than silently swapping models.

The output is a solved, unit-free region/constraint IR. It is not yet a list
of Unity transforms.

The implementation is `src/unity_spatial_fusion.py`. It verifies the hashes of
the closed Slice-3 IR, 6.1 evidence and manifest, schema, and 6.2 role table
and manifest before fusing them. Declared entities with no unique image
anchor receive a named role default plus a warning; visual labels that are not
declared entity matches remain evidence-only. Ambiguous declared-entity
matches abstain loudly. Schema/archetype surface constraints win over a
conflicting image surface, while the original image observation remains
recorded for audit.

Artifacts are hash-linked as:

```text
<project>.spatial-fusion.json
<project>.spatial-fusion.manifest.json
```

Run it with:

```text
python main.py unity-vizconcept-spatial-fuse \
  --closed-ir rallyboat.v5.closed-image-ir.json \
  --evidence rallyboat.v5.image-spatial-evidence.json \
  --evidence-manifest rallyboat.v5.image-spatial.json \
  --schema RallyBoat.schema.json \
  --roles RallyBoat.spatial-roles.json \
  --roles-manifest RallyBoat.spatial-roles.manifest.json \
  --output RallyBoat.spatial-fusion.json
```

Focused coverage is in `tests/test_unity_spatial_fusion.py`. The output is a
deterministic composition/constraint sidecar only: 6.4 owns relation-graph
construction and 6.5 owns world-space realization.

### 6.4 — Scene and relation graph construction

Reuse the graph-first work described in `docs/SCENE_GRAPH_COMPILER.md`:
entities and scenes are nodes; gameplay and spatial relationships are closed
edges. Persist the relation stage before coordinates are realized. Do not
create parallel `AdjacencyGraph.json` and `AnchorGraph.json` dialects; the
scene graph and its relation view are the canonical artifacts.

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

### 6.7 — Spatial freeze

Freeze the complete deterministic inputs and outputs together:

- image evidence hash and manifest;
- closed Concept/inventory lineage;
- schema-role and relation hashes;
- frozen asset-binding/catalog hashes;
- solved layout hash;
- measured footprints and transforms;
- spatial validation report.

The freeze is a build sidecar, not a new schema authority. Re-running with
the same frozen inputs must reproduce the same scene graph and transforms;
changing the board, Concept, asset catalog, or config creates a new lineage
instead of mutating an old freeze.

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
