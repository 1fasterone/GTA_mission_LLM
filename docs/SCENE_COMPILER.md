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
| a **role prefix** on the filename | `EntitySpec.kind` + `tag` | the LLM's guess from prose |

No schema field was invented for this: authored placement rides the same baked
arrays `unity_racetrack.py`'s start grid uses.

## Role prefixes — naming a model's JOB

Binding scores your model against entity names the Pipeline LLM invented at
Step 3. That works when the two share a word and fails silently when they do
not: `victorian_monobike.fbx` reaches an entity the model called
`PlayerMonobike`, and reaches nothing it called `Racer` — in which case the
bike is injected as `kind="prop"`, lands exactly where you put it, and is
wired as **scenery**. You cannot predict the model's word choice, so keyword
overlap can never be the control surface for "this one is the player."

Open the filename with a role word instead. Nothing in the Scene Compiler
changes — rename the file in `models/` and the role travels with it:

```
player_monobike.fbx     enemy_drone.fbx      pickup_scrap.fbx
rival_kart.glb          npc_marshal.fbx      building_grandstand.fbx
```

| Prefix | Becomes `kind` | Gets `tag` |
|---|---|---|
| `player_`, `hero_`, `avatar_` | `player` | `Player` |
| `enemy_`, `foe_`, `rival_`, `boss_` | `enemy` / `boss` | `Enemy` |
| `pickup_`, `collectible_`, `item_`, `goal_` | as written | `Collectible` |
| `npc_`, `ally_`, `character_`, `vehicle_`, `car_` | as written | — |
| `prop_`, `building_`, `tree_`, `road_`, `track_`, `wall_`, `water_`, … | as written | — |

Separator can be `_`, `-`, `.` or a camelCase boundary (`playerMonobike.fbx`).
The role word must **prefix something** — `vehicle.fbx` and `tree.fbx` are
ordinary asset names, not declarations, and are read as they always were.

What a declared role does:

- **Binds ahead of every keyword signal** (weight 8.0 vs. 3.0 for a name hit)
  to an entity that already holds that job — same `kind`, same role family, or
  the role's tag. The avatar is found by `tag == "Player"`, which is what the
  generator's player-merge selects on, so it works whatever the LLM named it.
- **Vetoes a conflicting bind.** `enemy_drone.fbx` can never reach the player,
  however well the names line up. `player`/`enemy`/`npc`/objective are
  mutually exclusive; `vehicle`, `character` and scenery kinds are descriptive
  and never veto (the player *is* a vehicle in a racing game).
- **Sets the bound entity's `kind` and `tag`** — this is the one place an
  authored scene reaches past models and transforms into wiring, and it
  happens only because you asked for it by naming the file.
- **Injects with that kind when nothing matches**, so an authored `pickup_`
  the LLM never wrote becomes a real objective instead of set dressing. A
  `player_` group with no match injects a tagged, `count == 1` avatar.
- **Never spreads to spill entities.** A grid of `player_` bikes feeds the
  named rivals the same *machine*, not the same job — otherwise the scene has
  three avatars. Spill is scored role-blind for exactly this reason.
- **Keeps the avatar a singleton.** `count > 1` silently disqualifies the
  player-merge, so a `player_` group placing a whole grid keeps pole position
  and re-homes the surplus as scenery, saying so loudly.

Layouts whose filenames carry no role word behave exactly as they did before
roles existed.

## Entities your layout does not cover

They keep resolving through the normal catalog tiers (Sketchfab, then Kenney)
— by design, but the binding step now **names them**:

```
scene: 2 entity(s) are NOT in your layout and will resolve from the asset
catalog — Grandstands(building x6), Marshals(npc x4). Place a model for each
in the Scene Compiler to author it instead.
```

The same list is stamped on `schema.meta["uncovered_entities"]`. If the output
contains models you did not choose, that message is the list to work from.

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
| declared **role** (filename prefix, above) | 8.0 |
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
