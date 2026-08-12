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
- **The four part helpers are NOT primitives** (this was previously recorded as a known limit and
  was wrong): `add_wheel` is a treaded tyre plus a spoked chrome hub, `add_disc` a domed lens,
  `add_slab` a tapered aerofoil, `add_strut` a two-segment leg with a joint bulge. The file
  contains no `primitive_*_add` call at all. They are merged into the welded body mesh rather than
  unioned as separate objects, and that is **deliberate, not a gap**: `rig_vehicle` skin-binds each
  wheel's vertices to its `Wheel.*` bone via a MATERIAL test (`Material.tire`) over the single
  mesh, and `_finalize` bakes the whole vehicle to the ground as one object. Splitting the parts
  out would break both. Do not "fix" it.
- **The real remaining gap is wheel arches.** The body is a slab-sided box, so each wheel is sunk
  into a flat flank with no arch cut around it — the biggest tell left on the car.
- **Because it is one mesh, a part's identity is its `material_index` alone**, which makes a missed
  material assignment invisible to every geometry check: the mesh stays manifold and the part stays
  in place, it just renders as something else. Two such defects were fixed here — `add_wheel`
  assigned `Material.tire` only to the tread band it collected for grooving, leaving each tyre's
  two end caps (one n-gon covering the whole disc) on slot 0, so **all four wheels rendered in body
  paint**; and the side character line asked `recessed_grid` for `Material.trim`, which recolours
  every cell it creates on a face that IS the whole door, turning the entire flank black on a coin
  flip of the random seed. `pytest -m blender_render -k rubber` asserts material by world position.
- **Routing contract (both CLIs):** a base that keyword-routes here but has no builder here must
  reach its real builder or stop — it must never quietly become another base. `clamp_params` and
  `build_base_code` raise `UnknownBaseError` (`[base.strict]`, naming where the builder lives), and
  `procedural_pipeline.py` auto-selects the detailed backend for `DETAILED_ONLY`
  (= `DETAILED_BASES - BASE_PARAM_SCHEMA`, derived so it cannot go stale, currently `{"car"}`).
  Both used to substitute `"furniture"` for an unrecognised base, so
  `procedural_pipeline.py "a jeep with big tires"` emitted a **table** and reported it as base
  `"car"`; `main.py unity-model` was always correct via `_VG_VARIANTS`. `plane` is deliberately NOT
  in `DETAILED_ONLY` — base_models builds the edge-loop-sculpted plane, and auto-routing would swap
  it for `vehicle_generator`'s. `--detailed` remains an explicit opt-in for it.

### 2. boat ✅ (implemented, AAA-sculpted)
- **Variants:** rowboat, sailboat, schooner (2-mast), motorboat, tugboat/steamer.
- **Parts:** double-skinned U-section `Hull` (one closed station loop = outer skin + inner skin, so
  the bulwark has real thickness and the shell is watertight), `Deck` (flat sole sweeping up to the
  sheer past 60% half-length → real fore/after decks), `Deckhouse` + `Wheelhouse` (floor ring →
  recessed window band → eave → crowned roof, aft door notch, raked screen), `Mast_*` + `Yard_*`
  (tapered n-gon spars, flared truck), `Sail_*` (bellied cloth volumes), `Funnel` + `FunnelBand`
  (hollow stack with a rolled rim + bridged collar annulus), foil `Rudder` + `RudderStock`,
  `PropHub` + `PropShaft` + twisted-foil `PropBlade_*`.
- **Hull profile:** one `station(ty)` closure returns (half-beam, section depth, rim height) and is
  the single source of truth — the deck outline, the rudder's top and the propeller's depth all
  query it instead of re-deriving the hull's shape. Eased plan taper floored at 0.10 half-beam
  (a stem, not a knife edge), sheer + rocker rising toward both ends.
- **Scale knobs:** hull_len/beam/depth, deck_h, mast_h.
- **Structural knobs:** `masts` (0–3), `sails`(0/1), `prop`(0/1 → motor), `cabin`(0/1),
  `rudder`(0/1), `funnel`(0/1).
- **Rig:** Root → Hull → `RudderSteer` (yaws) + `Propeller` (spins); both name-driven by
  `VehicleBoneDriver` (steer / prop). Rigid mechanical bind (weight 1.0). Full Unity pipeline
  (UV + COL_ hull + LOD0/1). Hull keel at z=0 amidships.
- **Gate:** `pytest -m blender_render -k boat` builds all six variants in real headless Blender and
  asserts every mesh is manifold and every `BOAT_BONES` entry gets a non-empty vertex group.

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

### 5. furniture ✅ (implemented, AAA-sculpted)
- **Variants:** table, desk, chair, stool, bench, sofa, bookcase, dresser, pedestal table.
- **Parts:** `top` (slab with a MOULDED edge — the rim is stepped in and out, not a cut cube),
  `leg_*` TURNED on a profile of revolution (post block → cove → vase swell → taper → foot) with
  the `leg_*_block` its `apron_x_*`/`apron_y_*` rails tenon into, panelled carcass (`side_*`,
  `back_panel`, `plinth`, `pedestal`) with recessed fields, `shelf_*`, a joined back
  (`back_stile_*` + `back` crest rail + `back_rail`), and `Drawer.*` fronts with a recessed panel
  and a turned brass `knob_*`. Upholstered pieces (`seat`+`cushion`) become a frame deck carrying
  loose `cushion_*` and `back_cushion_*` (1 for an armchair, 2–3 across a sofa). With a `back` they
  gain real ARMS: an upholstered `arm_*` panel landing on the frame rail, capped by an `arm_roll_*`
  bolster lofted front-to-back, plus a `back` panel with its own `back_roll` top rail. The cushions
  span the width BETWEEN the arms — running them the full width buried each arm inside the end
  cushion, and the piece read as a padded bench. Note `slab(puffy=True)` bulges its pad out to
  exactly the `sw`/`sd` asked for; its two bulge rings scale cumulatively (1.05 × 1.07), so the
  top ring starts in by that factor. A padded piece must set BOTH `seat` and `cushion` — `cushion`
  alone is silently inert (that is what left `bed` a bare slab on legs).
- **Toolkit:** `quad_ring`/`extrude_ring` for slabs and panels, `notch` for recessed fields and the
  cushion's stitched seam, `loft()` for turned legs, columns and knobs. Moulding is auto-dropped on
  panels thinner than 6% of their footprint: a 15 mm shelf takes a crisp arris, and moulding one
  then shading it smooth reads as a pillow.
- **Scale knobs:** width, depth, height, top_t, leg_r.
- **Structural knobs:** `legs` (0 carcass / 1 pedestal / 3 / 4), `seat`(0/1), `back`(0/1),
  `shelves` (count), `drawers` (count), `cushion`(0/1).
- **Rig:** static decor → the no-op rigger. Drawer fronts stay named `Drawer.*` by role for later
  articulation.
- **Gate:** `pytest -m blender_render -k furniture` — 9 variants, every mesh manifold.

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

### 8. bikes ✅ (implemented, AAA-sculpted)
- **Variants:** bicycle, road motorbike, dirt/off-road bike, unicycle, chopper.
- **Parts:** a real diamond frame (`head_tube`/`top_tube`/`down_tube`/`seat_tube` plus PAIRED
  `chain_stay_*`/`seat_stay_*` — one centreline stay read as a bike sliced in half), twin tapered
  `fork_*` blades on a `fork_crown` + `steerer`, a swept `handlebar` with `grip_*`, a sculpted
  `seat` (superellipse saddle: narrow nose, wide dished tail) and `tank` (teardrop), `Wheel.F/R`
  (reuse `make_wheel`), and for a motorbike an `engine` crankcase + finned `cylinder` barrel +
  bent `exhaust`. Bicycles get `chainring`, `crank_*`, `pedal_*`.
- **Toolkit:** `loft()` for every tube (an arbitrary spine, so the swept bar and bent exhaust are
  the same call as a straight seat tube, and a butted tube is just a radius per station);
  `quad_ring`/`extrude_ring` for hard-surface parts; `_se_ring` (superellipse) for saddle and tank.
- **Scale knobs:** wheelbase, wheel_r, wheel_w, frame_h, handlebar_w, rim. `wheel_w` spans
  0.022–0.32 m (road tyre → chopper) and defaults to **0.045**, a city/hybrid bicycle tyre: the
  default bike is a bicycle (`pedals`=1, `engine`=0), so its default tyre must be one. The old
  0.10 default put a motorcycle tyre on every pedal bike and the old 0.06 floor made a road tyre
  unrequestable. Tread grooves are sized off the tread face itself — `notch()`'s `inset` is an
  ABSOLUTE distance, and the old flat 0.10 exceeded the whole face on any tyre under 200 mm, so
  `inset_individual` inverted and the grooves stood PROUD of the tread as lugs, moving the tyre's
  lowest point and quietly breaking the `wheel_z = wheel_r` ground-contact assumption.
- **Structural knobs:** `wheels` (1 unicycle / 2), `engine`(0/1 → motorbike vs bicycle),
  `tank`(0/1), `pedals`(0/1), `suspension`(0/1, dirt forks).
- **Rig:** root → frame; `steer` (fork + handlebar bind to it), `Wheel.F/R` (spin, front parented
  to `steer`). Binding is still by ROLE; positions come from `BIKE_BONES`, built in `build()`.
  **They used to come from `obj.location`**, which is `(0,0,0)` for every bmesh part in this file
  (world-coord verts, object left at the origin — see `loft()`), so both wheel bones sat on the
  world origin and `VehicleBoneDriver`'s roll swung the wheels around it in a circle instead of
  spinning them on their axles. The steering stack is also measured off the TYRE now, not off a
  fraction of `frame_h`, which had put the fork crown 0.15 m inside the front wheel at the default
  knobs.
- **Gate:** `pytest -m blender_render -k bike` — manifold meshes, every `BIKE_BONES` entry bound,
  and each wheel bone asserted to be within 5 cm of its wheel's centroid.

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
2. ✅ **bike** — reuses `make_wheel` + wheel-bone rig; proves part reuse across bases.
3. **traffic props** — simple, static, high prop-value for GTA-style scenes; adds `Light` parts.
4. ✅ **furniture** — static + drawer knobs. (Lamps/lights + material presets not built.)
5. **building** — adds `make_window`/`make_hinge_panel` at scale + door rig.
6. ✅ **gun** (shipped as `weapon`) — enum part-swaps (scope/muzzle/stock).
7. ✅ **boat** — hull lofting + prop/rudder rig.
8. **animals** — the biggest: `make_limb` chains + `rig_chain`; quadruped → bird → fish → insect → spider.

All seven implemented families (car, bike, boat, furniture, humanoid, plane, weapon) have now had
the AAA sculpting pass. `building`, `traffic props` and `animals` are unbuilt, not unsculpted.

Each step: add schema + parts + assembler + rigger, unit-test (clamp + compile + role tags),
then live-verify in Blender (build → rig → pose → render) and confirm rigged FBX export.

## Notes / honest limits

- **No base is a primitive-composite any more.** Every family in `_BUILDERS` (plus `car` in
  `vehicle_generator.py`) is edge-loop sculpted or ring-lofted, and each one is pinned by a
  `_no_bare_primitives` gate over its whole knob matrix plus a headless Blender build that asserts
  manifold meshes. Nothing primitive remains inside `vehicle_generator.py` either — its four part
  helpers (`add_wheel`/`add_disc`/`add_strut`/`add_slab`) are sculpted, and their merging into the
  welded body mesh is required by `rig_vehicle`'s material-based wheel binding rather than a gap
  (see the car section above; the real one left there is wheel arches). `tube_between()` in `_PARTS` is now
  unused by every base (bike was its last caller) and is the one remaining
  `primitive_cylinder_add` in the shared toolkit; it stays only as a helper for future bases and
  should be replaced by `loft()` if anything reaches for it. **weapon** was the first: every part
  (receiver/barrel welded into one
  spine, muzzle device, stock, grip, trigger guard, magazine, foregrip, scope+mounts) is now built
  from `quad_ring`/`ngon_ring` + `extrude_ring` (real `bridge_loops` edge-loop extrusion, the same
  move as extruding a loop in the modeller) instead of primitive unions, and is gated into the full
  Unity pipeline (UVs + LOD0/1 + convex collider) like humanoid/plane/boat. **car** (see the car
  section above) is built by `vehicle_generator.py`'s own welded-bmesh toolkit
  (`extrude_face`/`scale_face`/`recessed_grid`) with real PBR materials — not primitive-composite for
  the body, and its wheels/lights/struts/wing-slabs are sculpted too. **plane** was the third: the
  ellipsoid-scaled `sphere()` fuselage/nose/tail is now a `ngon_ring`/`extrude_ring` ring-loft tube
  with tapered nose/tail caps, the cockpit is a real `uvsphere_bm` dome, wings/stabilisers/prop
  blades are `quad_ring`-tapered aerofoils, engines are ring-loft nacelles, gear struts are tapered
  with a joint bulge, and the wheels (via the now-shared `make_wheel()`, also used by bike) have
  real tread grooves + a recessed-spoke hub — no part left primitive. `PLANE_BONES`'s name-based
  rig binding (not material_index like car's wheels) was the contract to preserve; every part still
  carries the same `"bone"` value it always did. **humanoid** (ring-stack body, proximity skin,
  Unity Humanoid avatar) is the other base that was already fully sculpted, no rework needed —
  ported from the proven standalone generator (`humangen.py`) and ships the full Unity pipeline.
  **boat** was the fourth: the cabin/masts/sails/funnel/rudder/prop that used to be
  primitive-composite are now a ring-grown deckhouse and wheelhouse with recessed window bands,
  tapered n-gon masts/yards/stock/hub, doubly-curved cloth sails, a hollow funnel with a bridged
  collar, and NACA-foil rudder and twisted propeller blades. Three hull defects the new headless
  render gate exposed were fixed with it, since they made the sculpted parts look worse than they
  were: the hull was a single-sided open shell (non-manifold at every rim edge, see-through from
  inboard once Unity culls backfaces) and is now double-skinned; the plan taper collapsed to 4% at
  the ends, so both ends rendered as two thin prongs with the cockpit slot running out between
  them, now an eased taper floored at 10% with real sheer and a deck that sweeps up to close the
  ends; and the propeller/rudder hung in open water below the counter, now placed off the hull's
  own `station()` profile with a shaft into the hull. **bike** and **furniture** closed the set:
  bike moved every frame tube to `loft()` (freeing the last `tube_between` caller) and fixed a rig
  that placed both wheel bones on the world origin; furniture replaced the slab-on-cylinders with
  turned legs, aprons, moulded edges, panelled carcasses and loose cushions.
- For unique *hero* assets, the complementary **text-to-3D** track (cloud or bigger VRAM) can feed
  the same Blender sanitizer (cleanup → UV → LOD → rig). Parametric stays the controllable spine.
