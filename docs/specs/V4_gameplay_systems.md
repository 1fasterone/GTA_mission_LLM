# V4 SPECIFICATION — Gameplay Systems, AI Graphs, and Physics Behaviors

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V4 (Gameplay Systems)
**Status:** Normative specification
**Depends on:** V1 (Rig + CAS), V2 (Deformation), V3 (Entity/Scene/Mission), Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V3)

**0.1** `MUST`/`SHOULD`/`MAY`/`DEGRADE` as in V3.
**0.2 Compiler, not tool.** Pure transform; no DCC/wall-clock/ambient-RNG/path/iteration-order dependence.
**0.3 Layering invariant.** V4 never re-certifies V1/V2/V3; consumes `rig/deform/anim/asset` + `entity/scene/mission` hashes as trusted leaves. Adds runtime gameplay semantics; does not re-rig/re-place/re-wire.
**0.4 Micro-type model + `clamp_enum`.** Composable, versioned micro-types (AI/physics/interaction/combat/nav/state); immutable per `(id,version)`.
**0.5 Determinism floor.** Canonical IR; quantize-before-hash.
**0.6 The Gameplay Determinism Contract (defining V4 rule).** Two guarantees: **Spec determinism (MUST)** — the emitted config (graphs, params, wiring, solver settings, eval order, seeds) is a pure, fully-hashed function of inputs; **Simulation determinism (CONTRACTED)** — identical replay of an ordered input/event trace under the pinned fixed-timestep and solver settings reproduces identical logical state. Backend float bit-exactness is delegated; all *inputs* to determinism (timestep, iteration counts, solver order, quantized constants, hash-seeded RNG) are pinned and hashed.

---

## 1. V4 Architectural Overview

### 1.1 Purpose

V3 assembled inert content; **V4 gives it executable gameplay semantics.**

| System | Produces | Identity |
|---|---|---|
| AI Graphs | perception→decision→action graphs | `ai_hash` |
| Physics Behaviors | rigid bodies, controllers, vehicles, ragdoll, constraints | `physics_hash` |
| Interaction | use/pickup/open/push/talk/inspect systems | `interaction_hash` |
| Combat / Damage | hitbox/hurtbox/projectile/melee/ranged | `combat_hash` |
| Advanced Navigation | navmesh + pathing | `nav_hash` |
| Gameplay State Model | global/entity/mission state + save/load | `state_hash` |

Root: `gameplay_hash`.

### 1.2 Consumption of V1/V2/V3/anim outputs

Receives the **Gameplay Resolution Table (GRT)**: V2 proxies (load-bearing), anim **clip event tracks**, V3 entity interface descriptors / scene nav nodes+volumes+manifests / mission graph. References by hash; binds only to V3-published hooks; V2 proxies are the physical truth (missing → synthetic proxy). Substrate fingerprint `gp_fp := {rig, deform, anim, asset, entity?, scene?, mission?}`.

### 1.3 Phase ordering (compile order; section order ≠ compile order)

```
P1 Physics (physics_hash) → P2 Advanced Navigation (nav_hash) → P3 AI Graphs (ai_hash)
  → P4 Interaction (interaction_hash) → P5 Combat/Damage (combat_hash) → P6 Gameplay State (state_hash)
gameplay_hash = H(ai, physics, interaction, combat, nav, state, + substrate refs)
```

No back-edges; coupling via the event bus (§1.4.1). Nav consumes the physics collision world (not raw geometry) → "advanced" vs V3's static nav input.

#### 1.3.1 Relationship to V3-P1 behaviors

V3 declared capability; V4 **realizes** it: `ai.*`→AI graph, `combat.*`→combat pipeline, `interact.*`→interaction systems, `loco/veh/prop.physics_*`→physics. V4 MUST consume the V3 closure as authoritative; no capability the entity didn't declare (else inert).

### 1.4 Deterministic gameplay identity

```
gameplay_hash = H("v4.gameplay", physics_hash, nav_hash, ai_hash, interaction_hash, combat_hash, state_hash,
                  sorted(entity_ref…), sorted(scene_ref…), sorted(mission_ref…),
                  determinism_contract_hash, registry/resolver versions, policy_hash, v4_version)
```

Invariant **I-GP-ID**: one AI-node param change touches only `node → ai_hash → gameplay_hash`.

#### 1.4.1 Gameplay event bus

All cross-system coupling flows through one declarative primitive:
```
binding { kind: clamp_enum(event|state|socket|anim|nav|proxy); direction: clamp_enum(read|write|subscribe|publish|drive);
          source: hash+channel/slot/socket; target: hash+channel/slot/socket }
```
Content-addressed, canonically sorted, never reaches inside another system → keeps the DAG acyclic. All `*_wiring` hashes = `H(domain, ordered(binding records))`.

---

## 2. AI Graph System

### 2.1 Purpose
Compile V3 `ai.*` closures into deterministic perception→decision→action graphs. Same graph + same ordered perception trace + same fixed tick ⇒ same decisions (**I-AI-REPLAY**).

### 2.2 Inventory
Perception (`perc.sight_cone/hearing/proximity/damage_sense/line_of_sight/memory/threat_table`), Decision (`dec.bt_node/fsm_state/fsm_transition/utility_scorer/utility_select/blackboard`), Action (`act.move_to/patrol/pursue/flee/wander/idle/face/use_cover/attack/reload/interact/play_anim/set_state/notify`), Nav glue (`navq.path_to/nearest_cover/reachable/flee_vector/patrol_route`), Coordination (`coord.group/role/formation/shared_blackboard`).

### 2.3 Schema
```
ai_graph { ai_id; paradigm: clamp_enum(behavior_tree|state_machine|utility); entity_ref;
           blackboard: sorted set<bb_slot>; nodes; edges; perception; bindings; tick:{rate_hz,budget} }
ai_node { node_id; type: clamp_enum(perception|decision|action|condition|navquery); micro_type; params; children: ordered }
ai_edge { from; to; type: clamp_enum(transition|fallback|interrupt|timed); guard; priority }
```
One root (unreachable pruned); BT child order authoritative; FSM/utility canonically sorted; action MUST resolve bindings or DEGRADE to `act.idle`.

### 2.4 Paradigms
All lower to the same node/edge IR. BT: ordered child traversal. FSM: transitions in priority→canonical order, interrupts first. Utility: highest quantized score, ties by canonical `node_id`. Nesting allowed + hashed.

### 2.5 AI → animation
`act.play_anim`/movement request anim roles via V3 `anim_map` + Retarget; active windows from **clip event tracks** (shared with combat §5.2). Missing role → nearest → pose-hold + fixed fallback duration (no stall). `ai_anim_wiring_hash`.

### 2.6 AI → mission
Subscribe to mission state/events; publish via `act.notify/set_state`. Unresolved → no-op DEGRADE.

### 2.7 AI → scene
Bind `act.patrol`↔`nav.patrol_path`, `act.use_cover`↔`nav.cover_node`, perception↔`vol.region`, spawn-attached↔`spawn.point`. Pathing restricted to owning+adjacent streaming cells. Missing node → DEGRADE coupling (→ stationary idle).

### 2.8 Hashing
```
ai_node_hash = H("v4.ainode", type, micro_type, canonical(params), ordered_or_sorted(children))
ai_graph_hash = H("v4.aigraph", paradigm, entity_ref, sorted(bb_slot defs), sorted(ai_node_hash…),
                  sorted(ai_edge records), sorted(perc_binding), ai_wiring_hash, tick, gp_fp, degrade_decisions)
ai_hash = H("v4.ai", sorted(ai_graph_hash…))
```

---

## 3. Physics Behavior System

### 3.1 Purpose
Compile deterministic physics config bound to V2 proxies (fixed-timestep, pinned solver iterations/order, quantized constants).

### 3.2 Micro-types
`phys.rigid_static/rigid_dynamic/character_controller/vehicle/ragdoll/constraint/trigger/breakable` — each realizing a V3 behavior, bound to a V2 proxy.

### 3.3 Proxy integration
Every body bound to a V2 proxy by name; absent → **synthetic proxy** (capsule/box/sphere from quantized bounds, `synthetic=true`). Canonical-ordered assignment.

### 3.4 IK/animation/physics blending (fixed authority order)
```
per frame, fixed order: 1 Animation pose → 2 IK correction (V2 chains) → 3 Physics override (ragdoll/hit) → 4 ordered blend (quantized per-region weights)
```
Anim→physics (ragdoll on death) quantized blend, handoff pose = last animated (deterministic). Physics→anim (get-up) nearest clip by quantized orientation buckets. IK vs physics mutually exclusive per region (fixed flag) → no contested authority. Foot-plant uses anim event-track plant flags, not runtime velocity. `phys_blend_hash`.

### 3.5 Hashing
```
phys_body_hash = H("v4.body", micro_type, proxy_ref|synthetic_spec, canonical(mass/drag/restitution/friction…), body_type)
phys_constraint_hash = H("v4.constraint", type, sorted(body_ref pair), canonical(limits/stiffness))
physics_hash = H("v4.physics", sorted(phys_body_hash…), sorted(phys_constraint_hash…), phys_blend_hash, solver_settings, gp_fp, degrade_decisions)
```

### 3.6 Multi-backend
rigid_dynamic→Rigidbody/Chaos body/Rigid Body/`PhysicsRigidBodyAPI`; character_controller→CharacterController/Movement Comp/kinematic/custom CC schema; vehicle→WheelCollider/Chaos Vehicle/constraints/custom; ragdoll→configurable joints/Physics Asset/constraints/articulation. Solver-settings hash recorded so divergence is attributable.

---

## 4. Interaction System

### 4.1 Purpose
Realize V3 `interact.*`: deterministic detect→eligibility→prompt→confirm→animate→effect→result pipeline.

### 4.2 Micro-types
`int.use/pickup/open/push/talk/inspect` — each binding to V3 interact behaviors + scene interactables + mission hooks.

### 4.3 Interaction → animation
Anim role resolved via V3 anim_map; effect gated on an anim event-track marker when present, else fixed quantized delay; missing role → instant effect + pose-hold. `int_anim_wiring_hash`.

### 4.4 Interaction → mission
`int.use/inspect`→`obj.interact` + `trg.interaction`; `int.pickup`→`obj.collect` counter; `int.talk`→dialogue/branch node. Unresolved → local-only DEGRADE.

### 4.5 Interaction → AI
AI as initiator (`act.interact`) or subscriber (perceives interaction event); via event bus only. `int_ai_wiring_hash`.

### 4.6 Hashing
```
interaction_node_hash = H("v4.intnode", micro_type, canonical(params), eligibility_condition, effect_spec)
interaction_hash = H("v4.interaction", sorted(interaction_node_hash…), int_anim_wiring_hash, int_mission_wiring_hash, int_ai_wiring_hash, gp_fp, degrade_decisions)
```

---

## 5. Combat / Damage System

### 5.1 Purpose
Deterministic damage pipeline: hit detection (hitbox/hurtbox), timing (anim events), force (physics impulse), consequence (AI reactions + state). Hit resolution order canonical; magnitudes quantized; RNG hash-seeded `(attacker_hash, shot_index, salt)`.

### 5.2 Micro-types
`dmg.hurtbox` (V2 proxy, per-region multipliers), `dmg.hitbox` (V2 proxy, active during anim window), `dmg.melee/ranged_hitscan/projectile/area/receiver`.

### 5.3 Animation event integration
Active windows **exclusively from anim clip event tracks** (no runtime-pose inference). Shared with AI §2.5. Missing track → fixed quantized window at clip midpoint.

### 5.4 Physics impulse
Confirmed hit → quantized impulse on target body (ordered by canonical target order); lethal → ragdoll handoff impulse (§3.4).

### 5.5 AI reaction
Hit publishes `on_damaged`/`on_death`; AI subscribes (`perc.damage_sense`) → flinch/cover/retaliate/alert. Mission counts `on_death` (predicate in V3). Reaction selection by quantized hit-direction buckets (no RNG).

### 5.6 Hashing
```
hitbox_hash  = H("v4.hitbox", proxy_ref|synthetic, active_window_source, canonical(params))
hurtbox_hash = H("v4.hurtbox", proxy_ref|synthetic, sorted(region multipliers))
weapon_hash  = H("v4.weapon", micro_type, canonical(damage/range/impulse/rof/spread), rng_policy)
combat_hash  = H("v4.combat", sorted(hitbox_hash…), sorted(hurtbox_hash…), sorted(weapon_hash…),
                 anim_event_wiring_hash, impulse_wiring_hash, reaction_wiring_hash, gp_fp, degrade_decisions)
```

---

## 6. Advanced Navigation System

### 6.1 Purpose
Runtime navmesh + pathing from the physics collision world (P1) + V3 scene nav nodes.

### 6.2 Micro-types
`nav.surface/volume/area_cost/link/obstacle_static/obstacle_dynamic/tile`.

### 6.3 Streaming-aware nav
Navmesh tiled to the V3 partition (one `nav.tile` per `stream.cell`) → cell-level skip-work. Cross-cell paths stitched via deterministic portal edges (quantized half-open intervals). Query into unloaded tile → DEGRADE to nearest reachable border portal (never null).

### 6.4 AI pathing
`navq.*` resolve against `nav.surface` matching the entity's character-controller dims. Deterministic corridors (fixed funnel, quantized). Local avoidance (RVO/ORCA-class) with quantized neighbor sets, fixed iterations, canonical agent processing order (by entity hash).

### 6.5 Hashing
```
nav_tile_hash = H("v4.navtile", cell_ref, agent_type, sorted(surface params), sorted(static obstacle refs), sorted(link refs), sorted(area_cost), bake_settings)
nav_hash = H("v4.nav", sorted(nav_tile_hash…), partition_scheme_ref, sorted(agent_type defs), physics_collision_world_ref, gp_fp, degrade_decisions)
```
Bake delegated; `bake_settings` pinned + hashed.

---

## 7. Gameplay State Model

### 7.1 Purpose
Authoritative, deterministic, save/loadable state; hashes the schema/layout (runtime values are runtime).

### 7.2–7.4 State
Global (flags/counters/score/time-of-day/faction relations/difficulty). Entity (AI blackboard persistables, health/armor, inventory, physics sleep keys, AI state) keyed by `(entity_hash, instance_hash)`. Mission (imported from V3 mission state model by reference).

### 7.5 Save/load micro-types
`save.slot/checkpoint/autosave/migration`. Persistence boundary = union of `persist=true` slots, canonical order → reproducible portable save. Invariant **I-STATE-REPLAY** (extends I-REPLAY across all systems). Missing migration → default new slots.

### 7.6 Hashing
```
state_slot_hash = H("v4.slot", name, type, default(quantized), range, persist)
state_hash = H("v4.state", sorted(global slot hashes), sorted(entity slot schema hashes), mission_state_hash, sorted(save micro-type defs), schema_version, gp_fp)
```
Hashes schema/layout, not live values.

---

## 8. Multi-Backend Export Rules

Emits declarative data + refs to standard runtime components (runtimes provided out of band), never bespoke gameplay code. AI graph → BT/FSM/utility asset (Unity SO graph / Unreal BT+Blackboard / Blender datablock / USD metadata subtree). Physics per §3.6. Interaction → Interactable component+events. Combat → hitbox/hurtbox colliders + weapon data + AnimEvent/AnimNotify hooks. Event bus → UnityEvents/SO channels / BP dispatchers+GameplayMessage / drivers / USD relationships. Identity sidecar extends V3: adds `determinism_contract_hash`. GUIDs derive from content hashes.

---

## 9. Determinism, CAS, and Skip-Work

**9.1** `gameplay_hash` folds the six sub-hashes (Merkle). **I-GP-LOCAL**: leaf change propagates only along its path.
**9.2** Extends the V1 chain: `…entity→scene→mission → gameplay_hash {physics,nav,ai,interaction,combat,state}`. Same CAS/primitive. Project root may compose `{scene…, mission…, gameplay_hash}`.
**9.3 Partial recompilation:** AI param→ai_hash; physics mass→physics_hash (static-collider shape change also re-bakes affected nav tiles; pure mass change doesn't); anim event-track→combat_hash + ai_anim_wiring; scene instance moved→that cell's nav tile + bound AI; state slot added→state_hash.
**9.4 Determinism contract (pinned/hashed):** `dt_phys`, solver iterations, constraint solve order (canonical by body-pair hash), `dt_ai`, AI agent order (canonical by entity_hash), nav avoidance iterations, RNG seeding `H(salt, attacker/agent, index)`, float quantization (`q_pos/q_rot/q_time/q_param`), combat magnitudes quantized. All hashed floats quantized before hashing.
**9.5 Skip-work:** `if CAS.contains(h): reuse else compute`; lowering cached per `(content_hash, backend, policy_hash)`.

---

## 10. Validation & Degrade-Never-Fail Model

**10.1** **Theorem (Total Gameplay).** Any V3-passing content gets a valid V4 layer (AI/physics/interaction/combat/nav/state), possibly simplified, always backend-safe and deterministic. No content-induced hard-fail.

**10.2 Validation:** AI (single reachable root, bindings resolved/degraded, BT order preserved, no unbacked capability); physics (bodies bound to proxy/synthetic, constraints reference existing bodies, blend authority well-formed); interaction (full pipeline resolves; targets exist or degrade); combat (hit/hurt bound, window source or fallback, impulse target valid, reactions to existing channels); nav (every cell has a tile, agent-type matches a controller, queries restricted/degraded); integration (bindings reference resolvable hashes; only V3-published hooks; event-bus pub/sub → acyclic).

**10.3** Diagnostics deterministic + hashed into the unit.

**10.4 Degradation ladders:** AI (nearest action → disable coupling → inert idle graph w/ interface); Physics (synthetic proxy → nearest body type → static collider); Nav (widen tolerance → nearest portal → straight-line clamped + flag); Combat (midpoint window → single capsule hurtbox → contact-trigger damage); Interaction (local-only → instant+pose-hold → inspect-notify); State (default new slots → namespace by hash → minimal valid state).

**10.5** Every emitted artifact valid for every backend; lowering total over validated IR. "Passes V1/V2/V3" ⇒ loadable, deterministic, playable gameplay layer on Unity/Unreal/Blender/USD.

---

## Appendix A — V4 Identity Chain

```
asset/rig/deform/anim → entity/scene/mission (V3) → gp_fp →
  physics_hash (P1) │ nav_hash (P2) │ ai_hash (P3) │ interaction_hash (P4) │ combat_hash (P5) │ state_hash (P6)
  → gameplay_hash (+ determinism_contract_hash)
```

## Appendix B — Phase I/O

| Phase | Consumes | Produces | Hash |
|---|---|---|---|
| P1 Physics | V2 proxies, V3 behaviors, anim events | bodies/constraints/blend | `physics_hash` |
| P2 Nav | physics world, scene nav nodes, partition | navmesh tiles + links | `nav_hash` |
| P3 AI | nav, physics, anim, entity/scene/mission hooks | per-entity AI graphs | `ai_hash` |
| P4 Interaction | physics, AI, anim, mission | interaction systems | `interaction_hash` |
| P5 Combat | proxies, anim events, impulse, reactions | damage pipeline | `combat_hash` |
| P6 State | slots from P1–P5 + mission state | state schema + save/load | `state_hash` |

**End of V4 Specification — Gameplay Systems, AI Graphs, and Physics Behaviors.**
