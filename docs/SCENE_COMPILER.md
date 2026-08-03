# Authored scenes — the Scene Compiler front door

Arrange models by hand in the **Scene Compiler** (the browser tool in
`Documents/Blender/`), then hand that layout to the pipeline. The models you
placed and the transforms you gave them win over Sketchfab and Kenney; every
other decision — scripts, micros, objectives, controllers, audio, the rest of
the cast — is still generated.

```bash
python main.py unity-create --file prompts/monaco.txt --scene "C:/Users/Owner/Documents/Blender/layouts/MONACO.json"
```

That is the whole workflow. `--scene` accepts a raw layout or a normalized
scene file; everything below is detail.

## What the layout decides

| Scene Compiler | Becomes | Who normally decided it |
|---|---|---|
| `items[].file` | the entity's model (**Tier A**) | Sketchfab / Kenney catalog |
| `items[].position` | `EntitySpec.instance_positions` | `unity_layout.py` packing |
| `items[].yaw_deg` | `EntitySpec.instance_yaws` | model default rotation |
| `items[].scale` × `base_scale` | `EntitySpec.scale` | schema / catalog |
| N copies of one file | one entity with `count = N` | the LLM's authored count |

No schema field was invented for this: authored placement rides the same baked
arrays `unity_racetrack.py`'s start grid uses.

## The chain

```
layouts/<name>.json                       (Scene Compiler)
  └─ unity_sceneinput.py    parse, three.js→Unity, group, GLB→FBX
       └─ scene file        canonical, reviewable, deterministic
            └─ unity_scenebind.py  bind to entities, bake transforms   [Step 5]
                 ├─ unity_layout.py     sees them as PREPLACED — packs around
                 └─ unity_entity_asset_resolver.py  Tier A             [Step 6]
                      └─ Assets/Resources/Models/SceneCompiler/Authored/
```

`unity_scenebind.apply_scene` runs **after** path normalization (so your
coordinates land in world space untransformed) and **before** the layout pass
(so generated groups pack *around* what you placed instead of on top of it).

## Binding: which model answers to which entity

Each authored group is matched to at most one generated entity by keyword
overlap against the model's own filename:

| Signal | Weight |
|---|---|
| entity `name` | 3.0 |
| `model_hint` | 2.0 |
| `sketchfab_query` | 1.0 |
| `kind` | 1.0 |

A bind needs **≥ 2.0**, so `kind` alone never binds — otherwise one authored
car would claim every `kind="vehicle"` group in the schema. Tokens also match
by containment when both are ≥ 4 characters, so `PlayerBike` binds
`victorian_monobike.fbx`.

Assignment is greedy over (score, group order, entity order) — a total order,
so the same layout + schema always binds identically.

### Starting grids: one group can feed several entities

"One group, one entity" is wrong for the commonest authored shape there is —
several copies of the same machine on a grid, against a schema that names each
racer separately. A multi-instance group therefore also feeds any **other**
entity that scored for it, is still unbound, and is a **singleton**
(`count == 1`) in the schema. The primary keeps the first slots in layout
order (pole position); each spill entity takes one of the trailing ones.

Group entities (`count > 1`) never take spill — a 20-barrel pile is one pile,
not 19 plus a loose barrel.

> Why it exists: a live Monaco run handed all three authored monobikes to
> `PlayerMonobike`, giving it `count = 3`. That silently disqualifies it from
> the generator's player-merge, which requires `count == 1` — so the player
> drove an invisible capsule while three bikes sat on the grid. With spill,
> the player keeps pole position and each named rival gets one bike.

**Unmatched groups are injected** as their own entity, so a model you placed
always reaches the scene even when the LLM wrote no character for it.
**Uncovered entities keep resolving through the normal catalog tiers** — the
layout wins where it speaks and stays silent elsewhere.

If a bound entity is a single `tag: "Player"` object, the generator's existing
player-merge makes your authored transform the **player spawn point**.

## `base_scale` — read this before your first build

The Scene Compiler silently multiplies a model by `0.01` when its loaded
bounding sphere exceeds 100 units ("looked centimeter-scaled"). That
correction is part of what you *see* but was never part of `scale`, so a
rebuild elsewhere has no way to know it happened.

The tool now records it per item as `base_scale`, and the pipeline multiplies
it into the group scale. **Layouts saved before that change carry no
`base_scale`**; they build with a loud warning naming every affected file and
assume `1.0`.

> Measured on the real library: `monacoGP.fbx` and `victorian_monobike.glb`
> both come back `base_scale: 0.01`. A pre-change layout of those two builds a
> world 100× too large.

**Remedy: open the layout in the Scene Compiler and press Save once.**

## GLB / glTF

The Unity side of this repo is FBX end to end. GLB items are converted by
headless Blender — the same converter Sketchfab downloads use — into
`output/blender/scene_compiler/<hash>/`, keyed by source path + size + mtime,
so a rebuild does no Blender work.

The converter's unit-guess rescale is **disabled** for authored models
(`category="as_authored"`): you already sized this model by eye, and a
downstream "correction" would relocate everything around it. Conversion
failure is a hard error with a remedy, never a silent fallback.

## Deliberate abstentions

| Case | Behavior |
|---|---|
| `game_type` is `2D` / `2d_topdown` | `EntitySpawner` builds its own course at spawn time and ignores `instance_positions`. Models still bind; **placement cannot be honored** and you are told so. |
| entity has `placement: "path"` | It owns its geometry (an authored polyline). Model binds; the path stands. |
| a sprite in a 3D project | Skipped loudly — 3D scenes take models only. |
| authored source file missing at Step 6 | Warns and falls through to the catalog tiers rather than spawning nothing. |
| a layout item's file is missing | Hard error listing every one, with the models-dir remedy. |

## Where the files land

```
Assets/Resources/Models/SceneCompiler/Authored/<file>.fbx   (+ .meta)
Assets/Resources/Models/SceneCompiler/Textures/             (sibling Textures/)
Assets/Models/Authored/<file>.fbx                           (clean mirror)
```

Authored picks take **no** runtime bounds-fit and **no** shrink clamp
(`fit_size = 0.0`), and their `ModelImporter` scale stays `1.0` — your layout
scale rides on `EntitySpec.scale`, which is the transform the spawner applies.

## Commands

```bash
# Inspect / normalize a layout (optional — --scene takes a raw layout too)
python main.py unity-scene "C:/Users/Owner/Documents/Blender/layouts/MONACO.json"
python main.py unity-scene layouts/MONACO.json -o scene.json

# Build with it
python main.py unity-create --file prompts/monaco.txt --scene scene.json

# Models folder override (default: the layout's own ../models sibling)
python main.py unity-scene layouts/MONACO.json --models-dir D:/assets/models
```

## Provenance

Every run stamps `schema.meta`:

```json
"authored_scene": {"layout": "MONACO", "groups": 2, "bound": 2, "injected": 0,
                   "spill": 2, "model_only": 0, "skipped": 0},
"authored_models": {"MonacoCircuit": {"src": "...", "file": "...", "scale": 0.01}}
```

so the schema artifact records exactly which models came from your hand and
which the resolver chose.
