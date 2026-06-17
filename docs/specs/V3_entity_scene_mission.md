# V3 SPECIFICATION — Entity, Scene, and Mission Assembly

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V3 (Gameplay Assembly)
**Status:** Normative specification
**Depends on:** V1 (Rig Correctness + CAS substrate), V2 (Full Deformation Stack), Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions

**0.1 Requirement language.** `MUST` / `MUST NOT` are hard invariants; a violation is a compiler bug, not a content error. `SHOULD` is a strong default overridable only by an explicit, hashed policy flag. `MAY` is permitted behavior. `DEGRADE` denotes a mandatory fallback path that converts a would-be failure into a valid simplified output.

**0.2 Compiler, not tool.** V3 is a pure, side-effect-free transform: `compile(IR_in, policy) → IR_out + identity`. It does not open a DCC, does not mutate source assets, and does not depend on wall-clock, RNG seeds, hostname, path strings, float nondeterminism, or hashmap iteration order. Every output is a function of its hashed inputs only.

**0.3 Layering invariant.** V3 never re-certifies V1/V2 facts. It **consumes** `rig_hash`, `deform_hash`, `anim_hash`, `asset_hash` as opaque trusted certificates and treats them as immutable leaves of the V3 identity tree. V3 MUST NOT recompute rig or deformation correctness.

**0.4 Micro-type model.** All V3 capability is expressed as composable **micro-types** (behavior, scene-graph, mission). A micro-type is a small, versioned, schema-validated unit with declared inputs/outputs/dependencies/tags. Adding capability = adding micro-types, never rewriting the assembler. Every enum-valued schema field MUST use `clamp_enum` semantics (§7.5, §8): an out-of-range or unknown value clamps to the nearest valid member and emits a `DEGRADE` diagnostic instead of failing.

**0.5 Determinism floor.** All identity hashes are computed over the **canonical IR** (§7.4): UTF-8, NFC-normalized strings; integers in fixed width; floats quantized to fixed tolerance; arrays in canonical sort order; objects with sorted keys; no source paths, no timestamps.

---

## 1. V3 Architectural Overview

### 1.1 Purpose

V3 is the **gameplay-assembly layer**. V1 guarantees a rig is structurally correct; V2 guarantees it deforms correctly; Retarget/Clip Mapping guarantees animation is portable. None of these produce something *playable*. V3 converts certified rigs + deformation + clips into:

| Output | Phase | Identity |
|---|---|---|
| Fully wired **entities** (behaviorally complete actors/props) | P1 + P2 | `entity_hash` |
| Playable **scenes / worlds** (spatial + system composition) | P3 | `scene_hash` |
| Deterministic **mission / quest graphs** | P4 | `mission_hash` |

V3 is the boundary at which *semantic LLM intent* ("a guard who patrols and shoots") becomes *structural, hashed, backend-neutral assembly*. The LLM proposes; V3 disposes deterministically.

### 1.2 Consumption of V1/V2/Retarget outputs

V3 receives an **Asset Resolution Table (ART)** keyed by content hash:

```
ART := {
  rig_hash      → { skeleton contract, joint set, identity certificate }   # V1
  deform_hash   → { scaling, twist, correctives, IK chains, proxy set }    # V2
  anim_hash     → { canonical clip set, clip contracts, retarget profiles }# Retarget
  asset_hash    → { mesh, material, texture, audio, nav source CAS refs }  # V1
}
```

Rules:
- V3 MUST reference these only by hash. Any V3 node that names a rig, deformation, clip, or asset stores the **hash**, not a path or display name.
- A V3 input is **admissible** iff every referenced hash resolves in the ART and the referenced V1/V2 certificates are marked `valid`. Inadmissible references DEGRADE (§8.4), never hard-fail.
- The set `{rig_hash, deform_hash, anim_hash, asset_hash}` referenced by a V3 node forms its **substrate fingerprint** and is the first contributor to every downstream hash.

### 1.3 Phase ordering and dependencies

Phases are a **strict DAG**. Each phase consumes only the hashed outputs of prior phases.

```
   V1 rig_hash │ V2 deform_hash │ anim_hash │ asset_hash  (substrate)
                    │
   P1 ── Behavior Micro-Types ──────────► behavior_hash
                    │   (typed capability units bound to substrate)
                    ▼
   P2 ── Entity Assembly & Wiring ──────► entity_hash, wiring_hash
                    │   (behaviors + substrate → wired entity manifest)
                    ▼
   P3 ── Scene / World Composition ─────► scene_hash, spatial_hash, stream_hash, scene_wiring_hash
                    │   (entity instances + scene-graph micro-types)
                    ▼
   P4 ── Mission / Quest Graph Compilation ─► mission_hash, mission_node_hash, mission_state_hash, branch_hash
```

Dependency rules:
- **P1 → P2:** an entity may only bind behaviors whose `behavior_hash` is resolved.
- **P2 → P3:** a scene may only instance entities whose `entity_hash` is resolved.
- **P3 → P4:** a mission may only reference scene anchors/entities whose `scene_hash` (and contained node hashes) are resolved.
- **No back-edges.** P4 MUST NOT mutate P3 outputs; it references them. (Mission-driven spawns are expressed as P3 *spawn-point micro-types* with mission *bindings*, not as P4 editing the scene graph.)

### 1.4 Deterministic V3 identity model

Every V3 artifact has a **Merkle-style content hash** composed bottom-up from the substrate fingerprint. Composition operator `H(domain_tag, ordered children…)` is a domain-separated hash over canonical IR.

```
behavior_hash  = H("v3.behavior",  micro_type_id, version, substrate_fp, params, dep_hashes)
wiring_hash    = H("v3.wiring",     ordered(wire_records))
entity_hash    = H("v3.entity",     class, substrate_fp, sorted(behavior_hash…), wiring_hash, profile_refs)
spatial_hash   = H("v3.spatial",    sorted(placement_records))
stream_hash    = H("v3.stream",     partition_scheme, sorted(cell_manifests))
scene_wiring_hash = H("v3.scenewire", ordered(scene_wire_records))
scene_hash     = H("v3.scene",      sorted(entity_instance_hash…), spatial_hash, stream_hash,
                                    scene_wiring_hash, world_system_hashes)
mission_node_hash = H("v3.mnode",   node_type, params, sorted(binding_refs))
branch_hash    = H("v3.branch",     sorted(edge_records))
mission_state_hash= H("v3.mstate",  schema_version, sorted(state_slot_defs))
mission_hash   = H("v3.mission",    sorted(mission_node_hash…), branch_hash, mission_state_hash,
                                    sorted(scene_ref…), sorted(entity_ref…))
```

Invariant **I-ID**: changing any leaf (a single clip, a single param, one placement) changes exactly the hashes on the path to the root and no others. This is what makes skip-work (§7) sound.

---

## 2. Behavior Micro-Types (V3-Phase-1)

### 2.1 Purpose

A **behavior micro-type** is the atomic unit of gameplay capability. It is a typed, versioned contract that declares *what a thing can do* and *what substrate it requires*, independent of any engine. P1 produces a validated, hashed set of behavior instances; it does **not** place them in a world (that is P2/P3).

### 2.2 Behavior micro-type inventory (canonical registry)

| Family | Micro-types (examples) |
|---|---|
| **Locomotion** | `loco.biped_walk`, `loco.biped_run`, `loco.quadruped_gait`, `loco.strafe`, `loco.crouch`, `loco.swim`, `loco.fly`, `loco.climb`, `loco.jump` |
| **Combat** | `combat.melee`, `combat.ranged_hitscan`, `combat.ranged_projectile`, `combat.aim`, `combat.reload`, `combat.cover`, `combat.dodge`, `combat.death`, `combat.damage_receiver` |
| **AI / decision** | `ai.perception_sight`, `ai.perception_sound`, `ai.bt_root`, `ai.state_machine`, `ai.patrol`, `ai.pursue`, `ai.flee`, `ai.idle_scheduler`, `ai.target_select` |
| **Interaction** | `interact.use`, `interact.pickup`, `interact.carry`, `interact.talk`, `interact.lever`, `interact.button`, `interact.container` |
| **Vehicle** | `veh.drive_wheeled`, `veh.steer`, `veh.suspension`, `veh.enter_exit`, `veh.seat_set`, `veh.engine_audio`, `veh.damage_model` |
| **Door / portal** | `door.hinged`, `door.sliding`, `door.locked`, `door.auto_trigger` |
| **Pickup / item** | `pickup.instant`, `pickup.weapon`, `pickup.ammo`, `pickup.health`, `pickup.key`, `pickup.respawning` |
| **Camera** | `cam.follow`, `cam.aim_offset`, `cam.first_person`, `cam.cinematic_rail`, `cam.collision_pushout` |
| **Prop / world** | `prop.static`, `prop.physics_dynamic`, `prop.breakable`, `prop.attach_point` |
| **Lifecycle** | `life.spawnable`, `life.poolable`, `life.persistent`, `life.networkable` (flag-only) |

The registry is **open for extension, closed for mutation**: new micro-types may be added at any registry version; existing ones are immutable (a change ships as a new `version` field, old version retained for hash stability).

### 2.3 Micro-type schema

```
behavior_micro_type {
  id:            stable string id            # "combat.ranged_hitscan"
  version:       integer                      # immutable per (id,version)
  family:        clamp_enum(BehaviorFamily)
  tags:          set<clamp_enum(BehaviorTag)> # "needs_aim_ik","uses_locomotion",...
  inputs: {
     substrate:  set<SubstrateReq>            # e.g. rig joints, IK chain, clip slots
     params:     ordered map<key, TypedValue> # quantized, defaulted
     events_in:  set<EventChannel>
  }
  outputs: {
     events_out: set<EventChannel>
     state:      set<StateSlot>               # contributes to entity/mission state
     anim_slots: set<AnimSlotRequest>         # named clip roles it will drive
     ik_slots:   set<IkSlotRequest>           # named IK targets it will drive
     collider_slots: set<ProxyRequest>        # physics proxies it needs
  }
  deps: {
     requires:   set<micro_type_id>           # hard prerequisites
     optional:   set<micro_type_id>           # enhances if present
     incompatible: set<micro_type_id>         # mutually exclusive
     order:      partial_order                # before/after constraints
  }
  degrade:       DegradeProfile               # mandatory fallback (§8)
}
```

`SubstrateReq` examples: `joint("hand_r")`, `ik_chain("arm_r")`, `clip_role("fire")`, `proxy("hand_grip")`.

### 2.4 Class-specific behavior sets

A **class** is a named bundle of (required, default-optional, forbidden) micro-types.

| Class | Required | Default-optional | Forbidden |
|---|---|---|---|
| **humanoid** | `loco.biped_walk`, `ai.bt_root`, `combat.damage_receiver`, `interact.use` | `combat.melee`, `combat.ranged_*`, `loco.crouch`, `ai.patrol`, `cam.aim_offset` | `veh.drive_wheeled`, `loco.quadruped_gait` |
| **quadruped** | `loco.quadruped_gait`, `ai.bt_root`, `combat.damage_receiver` | `ai.patrol`, `ai.flee`, `combat.melee` | `combat.reload`, `interact.use`, `veh.*` |
| **car** | `veh.drive_wheeled`, `veh.steer`, `veh.suspension`, `veh.enter_exit`, `prop.physics_dynamic` | `veh.engine_audio`, `veh.damage_model`, `combat.damage_receiver` | `loco.*`, `combat.melee` |
| **rifle** (weapon item) | `pickup.weapon`, `combat.ranged_hitscan` (or `_projectile`), `prop.attach_point` | `combat.reload`, `pickup.ammo` | `loco.*`, `ai.*` |
| **door** | `door.hinged` \| `door.sliding`, `prop.static` | `door.locked`, `door.auto_trigger`, `interact.use` | `loco.*`, `combat.*` |
| **crate** | `prop.physics_dynamic`, `combat.damage_receiver` | `prop.breakable`, `interact.container`, `pickup.respawning` | `loco.*`, `ai.*` |

Class definitions are versioned and hashed; the class id + class version participates in `entity_hash`.

### 2.5 Substrate references from behaviors

- **Rig (`rig_hash`):** behavior names joints/sockets by canonical role; resolution maps role → joint via the V1 rig contract. Missing joint → DEGRADE per profile.
- **Deformation stack (`deform_hash`):** behaviors that drive runtime deformation request deform features by name (twist chain, IK chain, corrective trigger, proxy); absent → DEGRADE (disable coupling, keep behavior).
- **Animation clips (`anim_hash`):** behaviors declare `anim_slots`; the Retarget contract resolves each role to a concrete clip + per-engine retarget profile. Unfilled role → DEGRADE (pose-hold / nearest-clip).

### 2.6 Deterministic behavior hashing

```
behavior_hash = H("v3.behavior",
    micro_type_id, micro_type_version, registry_version,
    substrate_fp, canonical(params),
    sorted(resolved_substrate_refs), sorted(dep_hashes), degrade_decisions )
```

`degrade_decisions` is included so a degraded behavior is a distinct, reproducible artifact.

---

## 3. Entity Assembly & Wiring (V3-Phase-2)

### 3.1 Purpose

P2 composes a validated set of behaviors + substrate into a **single wired entity manifest** — a backend-neutral, fully connected description of one actor/prop.

### 3.2 Entity manifest schema

```
entity_manifest {
  entity_id:        stable string                 # NOT part of hash content
  class:            { id, version }
  substrate: {
     rig_ref:       rig_hash
     deform_ref:    deform_hash
     anim_ref:      anim_hash
     asset_refs:    sorted set<asset_hash>
  }
  behaviors:        sorted set<behavior_hash>     # the P1 closure (incl. deps)
  profiles: {
     retarget:      sorted set<retarget_profile_id>
     anim_map:      ordered map<anim_slot, anim_clip_ref>
  }
  ik:               sorted set<ik_binding>        # from deform_ref IK chains
  collision:        sorted set<proxy_binding>     # from deform_ref proxy set
  sockets:          sorted set<attach_point>      # named joints/sockets exposed for scene/mission use
  state_slots:      sorted set<state_slot_def>    # entity-local gameplay state
  wiring:           wiring_block                  # §3.5
  exports_hint:     backend_neutral export intent # §6 lowering uses this
}
```

`entity_id` is a **stable label**, deliberately excluded from the *content* hash so renaming does not invalidate skip-work; it is carried in a side-band identity record (§6.5).

### 3.3 Micro-type selection from LLM intent (semantic → structural)

```
intent → { class hint, capability tags, modifiers }     (LLM, free-form)
            │  deterministic resolver (no model in the loop)
class = resolve_class(class_hint)                         # clamp to nearest known class
candidate_set = class.required ∪ select_by_tags(capability tags)
candidate_set = apply_modifiers(candidate_set, modifiers)
final_set = compose(candidate_set)                        # §3.4
```

Rules: identical intent JSON ⇒ identical `final_set` (pure, versioned `resolver_version`). `clamp_enum` everywhere; no silent invention (unbacked capability DEGRADES to closest available, recorded).

### 3.4 Micro-type composition rules

`compose(set)` runs a fixed pipeline: (1) dependency closure; (2) incompatibility resolution (higher class-priority wins, DEGRADE-drop other); (3) optional promotion (only if their `requires` are satisfied); (4) ordering (topological by partial `order`, ties broken by `(family, id, version)` — canonical wiring order); (5) saturation check (substrate satisfiable, else DEGRADE per behavior). Output = frozen behavior closure + canonical order.

### 3.5 Wiring rules

**3.5.1 behavior → animation.** Each `anim_slot` bound to a clip via Retarget; unfilled → nearest clip in role family, else static pose-hold. Layer assignment derived from `layer_class`, deterministically.
**3.5.2 behavior → IK.** Each `ik_slot` binds a V2 IK chain; missing chain → disable that coupling. Solver order fixed by V2.
**3.5.3 behavior → physics / collision proxies.** Each `collider_slot` binds a V2 proxy; missing → conservative **synthetic auto-proxy** (capsule/box from quantized rig bounds, `synthetic=true`).
**3.5.4 behavior → mission/scene hooks.** Every entity exposes a fixed **hook surface** (named event channels, state slots, sockets/anchors) — the **interface descriptor** — so P3/P4 bind by name (resolved to hash at P4).

### 3.6 Deterministic entity hashing

```
wiring_hash = H("v3.wiring", ordered(wire_records))     # canonical wiring order from §3.4 step 4
entity_hash = H("v3.entity",
    class.id, class.version, resolver_version, registry_version,
    substrate_fp, sorted(behavior_hash…), wiring_hash,
    sorted(profile_refs), sorted(interface_descriptor) )
# entity_id (label) is NOT included → rename-stable.
```

Invariant **I-WIRE**: identical substrate + closure + wiring + interface ⇒ same `entity_hash` regardless of `entity_id` (dedup of structurally identical NPCs).

---

## 4. Scene / World Composition (V3-Phase-3)

### 4.1 Purpose

P3 composes **entity instances** + **scene-graph micro-types** into a playable, partitioned, system-integrated world.

### 4.2 Scene-graph micro-type inventory

| Category | Micro-types |
|---|---|
| **Spatial nodes** | `node.transform`, `node.group`, `node.anchor`, `node.landmark` |
| **Spawn** | `spawn.point`, `spawn.area`, `spawn.formation`, `spawn.pool` |
| **AI navigation** | `nav.anchor`, `nav.patrol_path`, `nav.cover_node`, `nav.link`, `nav.area_modifier` |
| **Triggers / volumes** | `vol.trigger`, `vol.kill`, `vol.water`, `vol.region`, `vol.objective` |
| **Lighting** | `light.zone`, `light.probe_volume`, `light.reflection_probe`, `light.time_of_day_bind` |
| **Audio** | `audio.zone`, `audio.emitter`, `audio.reverb_volume`, `audio.ambient_bed` |
| **Streaming / LOD** | `stream.cell`, `stream.volume`, `stream.lod_group`, `stream.impostor` |

### 4.3 Instancing rules

```
entity_instance {
  instance_id:   stable label                 # not in hash content
  entity_hash:   ref
  transform:     quantized TRS                 # pos(q_pos), rot(quat q_rot), scale(q_scale)
  overrides:     sorted, whitelisted param set
  bindings:      sorted set<hook_binding>
}
instance_hash = H("v3.einst", entity_hash, canonical(transform), canonical(overrides), sorted(bindings))
```

Overrides restricted to an override-safe whitelist (non-whitelisted DEGRADE, ignored). Identical entity+transform+overrides ⇒ identical `instance_hash` (GPU-instancing hint).

### 4.4 Spatial composition rules

Deterministic constraint solver (no RNG; any "randomness" hash-seeded from `instance_id + scene salt`, quantized):
- **Landmark-aware:** relative intent → absolute quantized transforms vs landmark hashes; missing landmark → scene-origin-relative.
- **Collision-aware:** validated vs V2 proxies; overlap → push-out (fixed axis priority, quantized step); unresolved after N steps → DEGRADE (`overlapping=true`, keep placement).
- **Navigation-aware:** `needs_navmesh` snaps to nearest nav sample within tolerance; spawn points MUST resolve to reachable sample or DEGRADE to flagged static spawn.
- **Behavior-aware:** patrol/cover/interaction require matching scene nodes; missing → DEGRADE the *coupling* (e.g. stationary), never the entity.

### 4.5 World-systems integration

Emits **system manifests** (bake inputs, not baked results): `nav_input_manifest`, `physics_manifest`, `lighting_manifest`, `audio_manifest`, LOD/streaming folded into `stream_hash`. Each canonical-ordered and individually hashed (`world_system_hashes`) so a lighting change does not invalidate navigation skip-work.

### 4.6 Scene partitioning and streaming

```
partition_scheme { type: clamp_enum(grid|quadtree|octree|volume), cell_size: quantized, origin: quantized, bounds: derived }
```

Every instance assigned to exactly one owning cell by a pure function of quantized transform + scheme (half-open `[min,max)` intervals, quantized — no border ambiguity). `stream_hash = H("v3.stream", partition_scheme, sorted(cell_manifests))` → cell-level skip-work.

### 4.7 Deterministic scene identity

```
spatial_hash      = H("v3.spatial", sorted(instance_hash by canonical transform key))
scene_wiring_hash = H("v3.scenewire", ordered(scene_wire_records))
scene_hash        = H("v3.scene", sorted(instance_hash…), spatial_hash, stream_hash,
                        scene_wiring_hash, sorted(world_system_hashes), partition_scheme )
```

Invariant **I-SCENE**: invariant under instance relabeling and input reordering (canonical sort); varies under any quantized transform/binding/system-input change.

---

## 5. Mission / Quest Graph Compilation (V3-Phase-4)

### 5.1 Purpose

P4 compiles a **deterministic mission/quest graph** over scenes and entities — pure data (nodes, typed edges, bindings, state model), no executable logic. Same graph + same ordered event trace ⇒ same state transitions on every backend.

### 5.2 Mission/quest micro-type inventory

| Category | Micro-types |
|---|---|
| **Objective** | `obj.reach`, `obj.eliminate`, `obj.collect`, `obj.interact`, `obj.escort`, `obj.survive`, `obj.timed`, `obj.defend` |
| **Trigger** | `trg.enter_volume`, `trg.exit_volume`, `trg.entity_state`, `trg.timer`, `trg.interaction`, `trg.objective_complete` |
| **Condition** | `cond.state_eq`, `cond.counter_ge`, `cond.flag`, `cond.all`, `cond.any`, `cond.not` |
| **Event** | `evt.spawn`, `evt.despawn`, `evt.set_state`, `evt.play_anim`, `evt.toggle_node`, `evt.give_reward`, `evt.notify` |
| **Reward** | `rew.item`, `rew.unlock`, `rew.score`, `rew.flag_set` |
| **Fail-state** | `fail.objective_failed`, `fail.entity_died`, `fail.timer_expired`, `fail.left_area` |
| **Control** | `ctl.sequence`, `ctl.parallel`, `ctl.branch`, `ctl.gate` |

### 5.3 Mission graph schema

```
mission_graph { mission_id; nodes: sorted set<mission_node>; edges: sorted set<mission_edge>;
                state_model; scene_refs: sorted set<scene_hash>; entity_refs: sorted set<entity_hash> }
mission_node { node_id; type: clamp_enum(objective|trigger|condition|event|branch|sequence|parallel|gate);
               micro_type; params: canonical map; bindings: sorted set<binding> }
mission_edge { from; to; type: clamp_enum(success|failure|conditional|timed|always);
               guard: optional cond ref; delay: optional quantized seconds }
```

Constraints (DEGRADE never hard-fail): well-formed control DAG, loops only via `ctl.gate`/`trg.timer` back-edges (decidable); every node reachable from a unique `start` (unreachable → pruned); ≥1 `complete` and ≥1 `fail` terminal (missing → auto-inserted).

### 5.4 Objective system

| Category | Micro-type | Completion predicate |
|---|---|---|
| reach | `obj.reach` | tracked entity enters bound volume/anchor radius |
| eliminate | `obj.eliminate` | bound target-set `on_death` count ≥ N |
| collect | `obj.collect` | bound `pickup.*` collected count ≥ N |
| interact | `obj.interact` | bound `interact.*` `on_interact` fired |
| escort | `obj.escort` | escortee alive AND reaches destination anchor |
| survive | `obj.survive` | timer elapses without fail condition |
| timed | `obj.timed` | wraps another objective with `trg.timer` fail edge |
| defend | `obj.defend` | bound entity/zone integrity ≥ threshold until condition |

Objective→scene wiring by hash (unresolved → DEGRADE: auto-complete-safe or optional). Objective→entity wiring via interface descriptor (`on_death`/state slots/position socket).

### 5.5 Trigger/condition/event systems

Triggers = sources (bind scene volumes/entity interfaces → emit events). Conditions = pure predicates over mission state + bound slots (fixed post-order eval). Events = sinks (`evt.spawn` refs entity_hash + spawn point; unresolved target → no-op DEGRADE).

### 5.6 Branching logic

Conditional branch: N guarded `conditional` edges in canonical order, first satisfied wins. Parallel: join `all|any|count(k)`; state-disjoint/commutative or DEGRADE to canonical sequencing. Fail-state: nearest enclosing handler else mission fail terminal. Optional branches isolated from required-path completion.

### 5.7 Mission state model

```
state_model { schema_version; global: sorted set<state_slot>;
              per_objective: map<objective_id, sorted set<state_slot>>;
              per_trigger: map<trigger_id, sorted set<state_slot>>;
              save_micro_types: sorted set<save_slot> }
state_slot { name, type, default(quantized), range, persist: bool }
```

Fixed-layout canonical record; reproducible save blob; `save_micro_types` defines persistence boundary. Invariant **I-REPLAY**: replaying the same ordered event trace from the same save reproduces identical state.

### 5.8 Deterministic mission identity

```
mission_node_hash  = H("v3.mnode", type, micro_type, canonical(params), sorted(bindings))
branch_hash        = H("v3.branch", sorted(edge_records))
mission_state_hash = H("v3.mstate", schema_version, sorted(state_slot_defs))
mission_hash       = H("v3.mission", sorted(mission_node_hash…), branch_hash, mission_state_hash,
                        sorted(scene_refs), sorted(entity_refs), resolver_version, registry_version )
```

Invariant **I-MID**: editing one objective changes its node hash + `mission_hash`, leaves siblings/`branch_hash` untouched (node-level skip-work).

---

## 6. Multi-Backend Export Rules (Unity, Unreal, Blender, USD)

### 6.1 Export model

Canonical output = backend-neutral V3 IR + identity. Export = pure lowering `lower(IR, backend) → artifacts + identity_sidecar`, identity-preserving (never changes a hash).

### 6.2 Entity export mapping

| Concept | Unity | Unreal | Blender | USD |
|---|---|---|---|---|
| Entity manifest | Prefab | Blueprint/Actor | Collection (linked) | typed Prim with refs |
| Behavior micro-type | Component | Component/BP node group | custom-prop + driver/constraint | `apiSchema`/custom attrs |
| Anim binding | Animator + clips | AnimBP/montage | Action + NLA | `SkelAnimation` + `BlendShape` |
| IK binding | IK rig refs | Control Rig | rig constraints | `SkelBindingAPI` + custom |
| Proxy/collision | Colliders | Collision bodies | rigid/collision objects | `UsdPhysics` colliders |
| Sockets/anchors | Empty children | Sockets | Empties | child `Xform` prims |

Behaviors lower to **declarative bindings**, not generated code.

### 6.3 Scene-graph export mapping

| Concept | Unity | Unreal | Blender | USD |
|---|---|---|---|---|
| Scene | Scene asset | Level/World | .blend scene/linked collections | Stage/root layer |
| Partition cell | Additive scene | World Partition cell/Data Layer | linked collection per cell | sublayer/payload per cell |
| Entity instance | Prefab instance | Actor instance | Collection instance | `references`/`instanceable` prim |
| Nav/cover/patrol | NavMesh + waypoints | NavMesh + links/splines | Empties + custom data | custom nav schema prims |
| Trigger/volume | Collider+trigger GO | Trigger volume actor | Empty + bounds | Region prim w/ metadata |
| Lighting/audio | Light/Reflection/Audio | Light/Audio/Reverb actors | Light/Speaker | Lux/Usd light + custom |
| Streaming | Addressables | World Partition | library overrides | payloads / `active`+`kind` |

### 6.4 Mission/quest export mapping

ScriptableObject graph (Unity) / Data Asset (Unreal) / structured metadata (Blender) / metadata prim subtree (USD). Missions = data assets interpreted by the engine mission-runtime (provided once); Blender/USD store portable round-trippable JSON-in-schema.

### 6.5 Engine-specific identity preservation

Every artifact carries an **identity sidecar** (`v3_kind`, `content_hash`, `substrate_fp`, `child_hashes`, `label_map`, `compiler`). Content hash is source of truth; `backend_guid = derive(content_hash, backend, label)` → stable references across re-compiles and DCC round-trips.

---

## 7. Determinism, CAS, and Skip-Work

**7.1** V3 hashes extend the V1 Merkle chain; stored in the same CAS, same primitive/canonicalizer.

**7.2 Partial recompilation:** entity-level (behavior param change recomputes that behavior→entity; old-`entity_hash` instances stay valid); scene-level (moved instance recomputes its cell manifest + spatial/stream/scene; other cells byte-identical); mission-level (edited objective recomputes one node hash → mission_hash; siblings reused).

**7.3 Skip-work:** `if CAS.contains(h): reuse else compute`. Sound: pure hashes, side-effect-free compiler, degrade decisions folded into hash.

**7.4 Canonical IR:** UTF-8/NFC; sorted keys; semantically-unordered arrays sorted by canonical key; meaningful-order arrays derived deterministically (topo-sort + fixed tie-break); no paths/timestamps/host ids.

**7.5 Quantization/ordering/tolerances:** fixed policy-pinned constants (`q_pos`, `q_rot` w≥0 hemisphere, `q_scale`, `q_time`, `q_param`, `tol_overlap`, `tol_nav`, `tol_landmark`); all hashed floats quantized before hashing; "random" placement hash-seeded + quantized. Changing a tolerance changes `policy_hash` → justified full recompile.

---

## 8. Validation & Degrade-Never-Fail Model

### 8.1 Core guarantee

**Theorem (Total Assembly).** Any rig passing V1/V2 can ALWAYS be assembled into (1) a valid entity, (2) a valid scene placement, (3) a valid mission reference (possibly simplified). No content-induced hard-fail path; only compiler/registry bugs are hard errors (CI-caught).

### 8.2 Validation rules

Behavior sets (schema-valid clamped params, satisfiable closure, incompatibilities resolved). Entity manifests/wiring (every slot bound or degrade-stubbed; interface well-formed; class required-set present). Scene graphs (every instance has owning cell; placements validated-or-flagged; manifests self-consistent). Mission graphs (reachable from start; terminals present; bindings resolve or degrade; no undecidable loops).

### 8.3 Error & degradation model

```
diagnostic { severity: info|DEGRADE|compiler_error; phase; code; subject; decision }
```

DEGRADE deterministic and hashed into the affected unit; aggregated into a per-artifact degrade report; build still succeeds.

### 8.4 Degradation ladders (first applicable rung)

- **Behavior:** nearest-compatible micro-type → disable unsatisfiable coupling → inert safe stub (hook surface preserved).
- **Substrate-binding:** missing clip → nearest → pose-hold; missing IK → drop coupling; missing proxy → synthetic capsule/box; missing joint → nearest ancestor → origin.
- **Placement:** push-out → snap to nearest nav/landmark → keep authored transform + flag.
- **Mission:** unresolved binding → mark optional → prune dangling node → auto-insert terminal → trivially-completable `start→complete` stub.

### 8.5 Backend-safety guarantee

Every emitted artifact is structurally valid for every backend; lowering is total over validated IR. "Passes V1/V2" ⇒ loadable, playable-safe entity + placement + mission on Unity, Unreal, Blender, USD.

---

## Appendix A — V3 Identity Chain

```
asset/rig/deform/anim → substrate_fp → behavior_hash (P1) → wiring_hash + entity_hash (P2)
  → instance_hash → spatial/stream/scene_wiring/world_system + scene_hash (P3)
  → mission_node/branch/state + mission_hash (P4)
```

**End of V3 Specification — Entity, Scene, and Mission Assembly.**
