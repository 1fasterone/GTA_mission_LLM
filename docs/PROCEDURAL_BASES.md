# Procedural Base-Model Library — Plan

The pipeline: **prompt → (LLM) base + params → (deterministic) parts → assembler → rig → export.**
The LLM only classifies and turns knobs; geometry is authored and recognisable by construction.
Proven on `humanoid`/`plane`/`boat`/`weapon` (this file's `_BUILDERS`) and on `car` (built by the
sibling `vehicle_generator.py`, not this file — see the car section below). This doc plans the rest.

## The pattern (every base follows it)

1. **Parts** (`base_models._PARTS`) — reusable *functional* builders. Each returns one named,
   role-tagged object whose **origin sits at the right pivot** so a rig/engine can drive it
   (a wheel spins about its axle; a door swings on its hinge edge). Parts are shared across bases.
2. **Assembler** (`_BUILDERS[base]`) — composes parts into the object using the knobs. Sets
   `obj.name` + `obj["role"]` (role tags survive Blender's name-collision renames).
3. **Rigger** (`_RIGGERS[base]`, defines `rig_model(parts, ox)`) — builds the skeleton at the
   actual part positions and skin-binds each part rigidly to its bone (single vertex group,
   weight 1.0). Bone names follow `rigging.py` conventions so animation retargets by name.
4. **Knobs** — two kinds. **Scale** knobs stretch one shape; **structural** knobs change *which
   parts exist / where* (the real source of variety: `bed`, `wheel_pairs`, `door_count`).
   Each base exposes both in `BASE_PARAM_SCHEMA[base]` as `(default, lo, hi)`; `clamp_params`
   fills/clamps/validates so a bad LLM token can't break a build.

Determinism/caching/export are inherited free: deterministic builders → stable `geo_hash`,
normalized FBX, and the verified Unity import path (separate functional parts = WheelCollider-ready).

## Shared part library (build once, reuse everywhere)

| Part | Origin / pivot | Used by |
|---|---|---|
| `make_wheel(r,w,rim)` ✅ | axle centre (spins about X) | car, bike, traffic cart, gun carriage |
| `make_panel(w,l,h,bevel)` (≈ `box`) | centre | every base (bodies, slabs, walls) |
| `make_tube(r,len,axis)` (≈ `cyl`) | centre | limbs, masts, barrels, poles, frames |
| `make_hinge_panel(...)` | **hinge edge** (swings) | door, gun stock fold, furniture lids, car doors |
| `make_window(w,h,inset)` | centre | building, vehicle (cosmetic) |
| `make_limb(segments,r)` | proximal joint | animals, humanoid (chain of tubes) |
| `make_foot/hoof/paw` | ankle | animals, humanoid |

Riggers reuse `rigging.py` bone specs (e.g. `RigTemplate*` armatures) where a base maps to an
existing class (humanoid, quadruped); new mechanical rigs are generated procedurally from part
positions like the car's.

## Smooth-mesh toolkit (ring-loft) — the quality upgrade

Early bases stacked separately-scaled primitives (`box`/`cyl`/`sphere`), which read as blocky.
The shared toolkit in `base_models._MESH_TOOLKIT` (emitted into every build script) lifts organic
shapes to smooth low-poly without changing the parts/assembler/rigger pattern:

| Helper | What it does |
|---|---|
| `loft(name, sections, rgb, n)` | tapered tube/limb/torso from a spine of `(center, radius)` sections — rings bridged into smooth quads. Verts in **world** coords (object at origin) so proximity weighting reads `v.co`. |
| `uvsphere_bm(name, center, r, rgb)` | head / eyes as a real bmesh sphere |
| `finish(o, smooth, subsurf, bevel)` | shade-smooth + optional Subsurf/Bevel — the single chokepoint that lifts the silhouette (operator-free, headless-safe) |
| `uv_unwrap(o)` | smart-project UVs (tolerant of headless context) |
| `collider_hull(name, parts)` | one convex `COL_*` hull merged from render parts → Unity convex `MeshCollider` |
| `make_lods(parts, levels)` | rename render parts `LOD0_*`, emit decimated `LOD1_*` copies (skinned copies keep weights; the FBX exporter bakes the decimate, preserves the skin) |

**Proximity skinning** replaces rigid weight-1.0 binds for organic bases: each vertex is weighted to
every bone within a falloff `radius` of its nearest bone-segment point, normalized — so joints
deform smoothly across two bones. (`_HUMANOID_RIG.seg_weight`, after the recipe in
*Fully Rigged Humanoid Character.md*.)

**Full Unity pipeline** (humanoid; `src/micro/model._build_base_code_with_fbx`): after build+rig the
emit runs `uv_unwrap` over the parts, builds a `COL_*` hull and `LOD0_/LOD1_` copies, then exports one
FBX. `templates/unity/ProcGenImporter.cs` (an `AssetPostprocessor` the overlay drops into
`Assets/Editor/`) auto-sets the **Humanoid** rig when a `Hips` bone is present and wires the
`LODGroup` + `MeshCollider` from those naming conventions.

---

## Base families

### 1. car ✅ (built by `vehicle_generator.py`, not this file's `_BUILDERS`)
`base_models.py` does NOT have a `car` entry — `BASE_NAMES`/`BASE_PARAM_SCHEMA` skip it on
purpose. Car used to have a second, primitive-composite implementation here (`_CAR_SRC`/`_CAR_RIG`,
box/cyl body + `make_wheel` wheels) but it was never reachable from the live `unity-model` CLI
(`_VG_VARIANTS = {"car"}` in `src/micro/model.py` always routed to `vehicle_generator.py` instead)
and was deleted rather than kept as a dead, drifting duplicate.
- **Real implementation:** `vehicle_generator.generate_vehicle()` — ONE continuous bmesh (inset+
  extrude+rake greenhouse, `recessed_grid` grille/vents, real PBR materials with procedural
  normal-map noise), not a primitive-composite. `"car"` still keyword-routes correctly
  (`base_models.keyword_base`) into this path via `src/micro/model.py`'s `_variant_table()`.
- **Still primitive today (candidate for the next AAA pass):** `add_wheel`/`add_disc`/`add_strut`
  (plain `_cone()` cylinders for wheels/lights/struts) and `add_slab` (`bmesh.ops.create_cube` for
  wings/fins) in `vehicle_generator.py` — geometrically simple shapes merged into the same welded
  mesh, not a separate-object union, but not sculpted detail either.

### 2. boat ✅ (implemented)
- **Variants:** rowboat, sailboat, schooner (2-mast), motorboat, tugboat/steamer.
- **Parts:** lofted U-section `Hull` (tapered bow/stern, keel z=0), inset deck, cabin/wheelhouse,
  masts + square sails, funnel, steerable `Rudder`, motor propeller (`PropBlade_*`).
- **Scale knobs:** hull_len/beam/depth, deck_h, mast_h.
- **Structural knobs:** `masts` (0–3), `sails`(0/1), `prop`(0/1 → motor), `cabin`(0/1),
  `rudder`(0/1), `funnel`(0/1).
- **Rig:** Root → Hull → `RudderSteer` (yaws) + `Propeller` (spins); both name-driven by
  `VehicleBoneDriver` (steer / prop). Rigid mechanical bind (weight 1.0). Full Unity pipeline
  (UV + COL_ hull + LOD0/1). Hull keel at z=0.

### 3. building
- **Variants:** house, shop, tower, warehouse, hut.
- **Parts:** footprint slab, wall ×4 (with cut openings), `Door` (hinge), `Window` grid, roof
  (flat/gable/hip), trim/cornice, chimney, balcony.
- **Scale knobs:** width, depth, floor_h, floors.
- **Structural knobs:** `floors` (stacked), `roof_type` (flat/gable/hip), `door_count`,
  `windows_per_floor`, `trim`(0/1), `chimney`(0/1), `balcony`(0/1).
- **Rig:** mostly static; `Door` bones (hinge) so doors open. Root at ground.

### 4. gun
- **Variants:** pistol, rifle, smg, shotgun, sniper.
- **Parts:** receiver, barrel, `Muzzle` (swap: none/suppressor/brake), `Scope` (swap:
  irons/red-dot/scope), `Magazine` (clip), `Stock` (none/fixed/folding), grip, trigger.
- **Scale knobs:** barrel_len, receiver_size, grip_len.
- **Structural knobs:** `scope` (enum), `muzzle` (enum), `mag`(0/1, size), `stock` (enum),
  `foregrip`(0/1).
- **Rig:** root → receiver; `Trigger` bone (pivot), `Bolt`/`Slide` bone (travel), `Magazine`
  bone (insert/eject), `Stock` hinge if folding. Origin at grip for hand attach.

### 5. furniture
- **Variants:** chair, table, bed, shelf, lamp, desk, stool, sofa.
- **Parts:** top/surface, `Leg` ×N, backrest, cushion, drawer (slide), shade, `Light` (emissive +
  point light), mattress, frame.
- **Scale knobs:** width, depth, height, leg_thickness.
- **Structural knobs:** `leg_count` (3/4), `back`(0/1), `arms`(0/1), `drawers` (count),
  `light`(0/1 + colour/intensity), `material` (wood/metal/fabric → colour+roughness preset).
- **Rig:** mostly static; `Drawer` bones (slide), lamp `Shade` (tilt). Lights are real
  `bpy.data.lights` parented in.

### 6. animals
- **Sub-bases (distinct topologies — not one base):**
  - `quadruped` (mammal): spine, head, 4 legs (chain), tail, ears. → maps to `rigging.py` quadruped.
  - `bird`: body, head/beak, 2 wings (fold), 2 legs, tail.
  - `fish`: body (tapered), dorsal/pectoral/tail fins. → spine + fin bones (swim).
  - `insect`: thorax/abdomen/head, 6 legs, antennae, optional wings.
  - `spider`: cephalothorax/abdomen, 8 legs (chain ×8).
- **Scale knobs:** body_len, leg_len, height, head_size.
- **Structural knobs:** `legs` (4/6/8), `wings`(0/1), `tail`(0/1), `fins` (fish), `horns`(0/1).
- **Rig:** spine chain + per-limb chains (walk/swim/fly cycles). Heavy reuse of a `make_limb`
  chain part + a generic `rig_chain` helper in `rigging.py`.

### 7. traffic props
- **Variants:** street light, traffic light, sign, hydrant, bollard, bench, bin.
- **Parts:** pole, `Lamp_head`, `Signal` (3 lamps + housing), `Sign_plate` (swap shape:
  circle/triangle/rect), base/foot, `Light` emitters.
- **Scale knobs:** pole_h, head_size.
- **Structural knobs:** `prop_type` (enum chooses the head), `arms` (street-light count),
  `signal_faces` (1–4), `sign_shape`.
- **Rig:** static (root at ground); signal lamps as named parts the engine toggles
  (material emission swap, no bones needed).

### 8. bikes
- **Variants:** bicycle, road motorbike, dirt/off-road bike, unicycle, scooter, chopper.
- **Parts:** frame (tube assembly), `Wheel.F/R` (reuse `make_wheel`!), handlebars (steer),
  seat, fork, fuel tank (motorbike), pedals (bicycle), engine block.
- **Scale knobs:** wheelbase, wheel_r (knobby/big for dirt), frame_size.
- **Structural knobs:** `wheels` (1 unicycle / 2), `engine`(0/1 → motorbike vs bicycle),
  `tank`(0/1), `pedals`(0/1), `suspension`(0/1, dirt forks), `handlebar_style`.
- **Rig:** root → frame; `Wheel.F/R` (spin), `Handlebars` + `Fork` (steer), `Pedal` bones
  (bicycle). Reuses the car's wheel-bone approach.

### 9. (open) anything else the prompt needs
The router can grow: tree/plant (lean on `recipes.py`), barrel/crate/prop, fence/wall segment,
container, weapon-melee (sword/axe = blade + hilt parts), tool. Add a base when a silhouette
can't be a knob on an existing one.

---

## Rigging conventions (`rigging.py`)

- Bone names are the contract for animation retargeting: `root`, `body`/`spine`, `Wheel.FL`,
  `Door`, `Trigger`, `Wing.L`, `Leg.FL.1/2/3`, etc.
- Two rig styles:
  - **Mechanical / procedural** (car, gun, building doors, traffic) — bones generated from part
    positions, parts rigidly skin-bound (weight 1.0). Already proven on car.
  - **Organic chains** (animals, humanoid) — bone chains via the `RigTemplate*` armatures +
    auto/heat weights, fitted to the mesh.
- Each base's rigger lives next to its assembler (`_RIGGERS[base]`); shared rig helpers
  (`rig_chain`, `fit_bind`) go in `rigging.py`.

## Build order (incremental, each shippable + tested)

1. ✅ **car** — part library + structural knobs + bone rig + FBX export.
2. **bike** — fastest win, reuses `make_wheel` + wheel-bone rig; proves part reuse across bases.
3. **traffic props** — simple, static, high prop-value for GTA-style scenes; adds `Light` parts.
4. **furniture** — static + drawer/light knobs; adds `make_hinge_panel`, lights, material presets.
5. **building** — adds `make_window`/`make_hinge_panel` at scale + door rig.
6. **gun** — enum part-swaps (scope/muzzle/stock) — proves the "swap" structural knob.
7. **boat** — hull lofting + prop/oar/rudder rig.
8. **animals** — the biggest: `make_limb` chains + `rig_chain`; quadruped → bird → fish → insect → spider.

Each step: add schema + parts + assembler + rigger, unit-test (clamp + compile + role tags),
then live-verify in Blender (build → rig → pose → render) and confirm rigged FBX export.

## Notes / honest limits

- Mechanical bases still built as **primitive-composites** (`box`/`cyl`/`cone`/`sphere` unions) —
  bike/furniture — are placeholders, not AAA meshes, and are the next candidates for the edge-loop
  sculpting toolkit below. **weapon** was the first: every part (receiver/barrel welded into one
  spine, muzzle device, stock, grip, trigger guard, magazine, foregrip, scope+mounts) is now built
  from `quad_ring`/`ngon_ring` + `extrude_ring` (real `bridge_loops` edge-loop extrusion, the same
  move as extruding a loop in the modeller) instead of primitive unions, and is gated into the full
  Unity pipeline (UVs + LOD0/1 + convex collider) like humanoid/plane/boat. **car** (see the car
  section above) is built by `vehicle_generator.py`'s own welded-bmesh toolkit
  (`extrude_face`/`scale_face`/`recessed_grid`) with real PBR materials — not primitive-composite for
  the body, though its wheels/lights/struts/wing-slabs still are. **plane** was the third: the
  ellipsoid-scaled `sphere()` fuselage/nose/tail is now a `ngon_ring`/`extrude_ring` ring-loft tube
  with tapered nose/tail caps, the cockpit is a real `uvsphere_bm` dome, wings/stabilisers/prop
  blades are `quad_ring`-tapered aerofoils, engines are ring-loft nacelles, gear struts are tapered
  with a joint bulge, and the wheels (via the now-shared `make_wheel()`, also used by bike) have
  real tread grooves + a recessed-spoke hub — no part left primitive. `PLANE_BONES`'s name-based
  rig binding (not material_index like car's wheels) was the contract to preserve; every part still
  carries the same `"bone"` value it always did. **humanoid** (ring-stack body, proximity skin,
  Unity Humanoid avatar) is the other base that was already fully sculpted, no rework needed —
  ported from the proven standalone generator (`humangen.py`) and ships the full Unity pipeline.
  **boat** (lofted U-hull + masts/sails or cabin/funnel/prop, with a rudder+prop mechanical rig)
  ships the same pipeline, though its cabin/masts/sails/funnel/rudder/prop are still
  primitive-composite. Bike/furniture adopt the sculpting toolkit next.
- For unique *hero* assets, the complementary **text-to-3D** track (cloud or bigger VRAM) can feed
  the same Blender sanitizer (cleanup → UV → LOD → rig). Parametric stays the controllable spine.
