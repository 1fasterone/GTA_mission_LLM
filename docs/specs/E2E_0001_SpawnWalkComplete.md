# E2E TEST SPECIFICATION — `E2E_0001_SpawnWalkComplete`

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Artifact class:** End-to-end vertical-slice test (a V11 `test.scenario` + `test.regression`)
**Scope:** V1 → V5 (with the Retarget clip layer)
**Status:** Normative test specification
**Determinism basis:** V5 §0.6 (snapshot-replayable world), V11 §0.7 (Replay-Equivalence), V11 §0.9 (Divergence-Localization)

> **IMPLEMENTATION STATUS (2026-06-17):** A **minimal runnable reference kernel for this slice now
> exists** at `src/vstack/` (`canon.py` — content-addressed hashing; `slice_v1_v5.py` — the V1→V5
> identity chain + tick-barrier sim; `e2e_0001.py` — the runner). `tests/test_e2e_0001.py` (11 tests)
> passes: the slice spawns, walks at a constant quantized 0.050 u/tick, completes the objective at
> **tick 91**, replays **byte-identically** (and reproduces the same hashes across separate processes),
> and degrades to a distinct, reproducible golden. Run it: `python -m src.vstack.e2e_0001 --degrade`.
> The *broader* V1–V13 compiler remains a specification — this kernel implements only the minimal
> vertical slice these E2E steps require, not the full architecture.

---

## 0. Conventions

`MUST`/`MUST NOT` = hard test invariants. `SHOULD` = default. `DEGRADE` = a mandatory fallback that keeps the run valid, deterministic, and completing. Per V11, a test FAILURE is the test succeeding (it found a divergence); the degrade-never-fail model (§7) governs the *system under test*, not the *verdict*. `worldstate_hash@N` = the V5 runtime snapshot hash `worldstate_value_hash@N` (per-tick oracle).

---

## 1. Test Overview

### 1.1 Purpose
The first end-to-end test and canonical determinism tripwire: validates that one rig flows from V1 structural certification to a live V5 tick-advancing simulation, firing every link in the identity chain once, fully reproducible from `(build_hash, world_seed, tick_schedule)`.

### 1.2 Why minimal
Exactly one of everything needed to produce each hash: 1 rig, 1 deformation stack (skin + 1 IK chain + 1 capsule proxy), 2 clips (`Idle`,`Walk_Forward`), 1 behavior closure (`Idle→WalkTo`), 1 entity, 1 scene (1 cell, 1 spawn, 1 target volume), 1 mission (1 `obj.reach`), 1 physics body (capsule controller), 1 nav surface, 1 AI graph, N sim ticks. Remove any → a hash is unexercised; add any → not minimal. **Empty input trace.**

### 1.3 Deterministic identity expectations
Headline chain: `rig_hash → deform_hash → entity_hash → scene_hash → mission_hash → behavior_hash → physics_hash → worldstate_hash@N`.

Build-DAG order: `rig_hash → deform_hash → anim_hash → behavior_hash(V3-P1) → entity_hash → scene_hash → mission_hash → [V4: physics_hash → nav_hash → ai_hash → gameplay_hash] → [V5: worldstate_hash@0…N]`. `behavior_hash` is the V3-P1 capability; its V4 realization is `ai_hash`. With `input_trace=∅`, the entire run is a pure function of build+seed+schedule — the purest determinism test in the suite.

---

## 2. Preconditions

**Fixed inputs:** `world_seed=0x00000001`; `tick_schedule`= 30 Hz, `dt=1/30s`, integer ticks, net≡sim 1:1; `input_trace=∅`; quantization `q_pos=1e-3`, `q_rot` (w≥0), `q_time=1e-3` (round-half-even); `build_hash`=composed `H(V1…V5 roots)`.

**V1:** one certified humanoid/biped rig; `rig_hash` stable; sockets `pelvis/foot_l/foot_r/root`.
**V2:** skin + optional twist + 0 correctives + 1 leg IK chain + 1 `body_capsule` proxy; `deform_hash` stable, references `rig_hash`.
**Retarget:** clips `{Idle, Walk_Forward}` with contracts/profiles; `Walk_Forward` has foot-plant event-track flags; `anim_hash` stable.
**V3 entity:** `class=humanoid`; behaviors `{loco.biped_walk, ai.bt_root}`; `anim_map={idle→Idle, walk→Walk_Forward}`; IK→leg chain; collision→`body_capsule`; interface exposes `on_reach` + `ai_state`. ⇒ `entity_hash` (rename-stable).
**V3 scene:** single grid cell; `spawn.point P_spawn=(0,0,0)`; `vol.objective T_target` (sphere center `(0,0,5.000)`, r `0.500`); flat `nav.surface`. ⇒ `scene_hash` (+ spatial/stream/scene_wiring).
**V3 mission:** one `obj.reach` bound to `T_target`; `start→O1→complete`; terminals present; O1 subscribes to `on_reach`. ⇒ `mission_hash`.
**V4 behavior→AI:** FSM — `Idle` → (guard `O1==active`) → `WalkTo` runs `act.move_to(T_target.center)` → on arrival publishes `on_reach`. Single unambiguous transition. ⇒ `behavior_hash`/`ai_hash`.
**V4 physics:** `phys.character_controller` (kinematic capsule) bound to `body_capsule`; quantized step/slope/gravity; pinned solver. ⇒ `physics_hash`.
**V4 nav:** one flat `nav.tile`; straight-line path. ⇒ `nav_hash`. Motion: `walk_speed=1.500 u/s` → `0.050 u/tick`.
**V5:** `sim.clock` + driver reading entity+mission state to the blackboard; no factions/ecology/economy/weather (absent → valid); tick-barrier double-buffer; `worldstate_hash@N` emitted every barrier (incorporates tick N → distinct per tick); snapshot hashes quantized transform + controller + `ai_state` + objective state + tick.

---

## 3. Test Steps (Canonical)

1. **Load scene** — instantiate `scene_hash`; init world @ tick 0; spawn entity at `P_spawn` via the spawn point. *Assert:* `scene_hash`==golden; `worldstate_hash@0`==golden; entity at `P_spawn`, `ai_state=Idle`, `O1=inactive`.
2. **Spawn entity (deform applied)** — realize `entity_hash`; bind deform (skin+leg IK+`body_capsule`); attach physics capsule. *Assert:* `entity_hash`==golden; `deform_hash`==golden (applied); controller bound to `body_capsule` (non-degraded).
3. **Begin mission** — `start→O1` activates @ tick 1; O1 subscribes `on_reach`. *Assert:* `mission_hash`==golden; `O1=active` @ tick 1.
4. **Behavior loop (Idle→WalkTo); physics active** — @ tick 1 FSM transitions (guard true); `act.move_to` begins; controller integrates. *Assert:* `behavior_hash`/`ai_hash`/`physics_hash`==golden; transition @ exactly tick 1; `ai_state=WalkTo` thereafter.
5. **Movement** — per movement tick advance `+0.050` along `+Z`; foot-plant IK from event-track; `worldstate_hash@N` increments each tick. *Assert (1≤k≤90):* `Z@(1+k)=0.050×k`; constant delta `0.050`; anim=`walk`; each `worldstate_hash@N`==golden.
6. **Enter target; complete** — at `Z=4.500` (dist to center `0.500`=radius) entity is inside `T_target` → `on_reach` → `obj.reach` satisfied → `O1=complete` → mission `complete`. *Assert:* completion @ exactly golden tick `N=91`; mission terminal `complete`; entity inside `T_target`.
7. **Capture final** — snapshot. *Assert:* `worldstate_hash@N`==golden; full `@0…N` sequence + rolling checksum match.

---

## 4. Deterministic Replay Requirements

**Replay tuple:** `(build_hash, world_seed=0x00000001, 30Hz/1:1, input_trace=∅)`. Empty trace → no exogenous variability → strongest I-REPLAY-EQ.

**Byte-identical reproduction (MUST):** positions (per-tick quantized), animation states, behavior transitions (tick), mission transitions (tick), and the full `worldstate_hash@N` sequence + checksum.

**Cross-backend parity (MUST):** the same replay on the V12 reference VM and on Unity/Unreal/Blender/USD MUST produce identical logical `worldstate_hash@N`; quantization absorbs sub-tolerance float diffs; surviving divergence = real bug (or blessed variance).

**Divergence rules (I-DIVLOC):** binary-search `worldstate_hash@N` → first diverging tick T → diff snapshot sub-fields + per-layer sub-hashes → emit bug coordinate `{ref/backend, tick T, subsystem, sub-hash diff, replay}`.

**Failure conditions:** any hash-sequence/position/behavior-timing/mission-timing/identity/backend divergence; non-termination by `tick_budget=2N=182`; or a hard compiler/VM error (which §7 prevents). Failure is reported loudly, never degraded away.

---

## 5. Expected Outputs

**Primary success:** spawn@0 → `Idle→WalkTo`@1 → constant `0.050`/tick walk → enter `T_target` → `O1` complete @ `N=91` → mission `complete`.
**Secondary success:** every `*_hash`==golden; full `worldstate_hash@0…N` reproduced; cross-backend parity holds.
**Golden bundle** `golden.E2E_0001` (CAS, pinned to `build_hash`): `rig_hash, deform_hash, anim_hash, behavior_hash, entity_hash, scene_hash, spatial/stream/scene_wiring, mission_hash, mission_node(O1)/branch/state, physics_hash, nav_hash, ai_hash, gameplay_hash, sim_hash, worldstate_hash@0, worldstate_hash@N, worldstate_seq_checksum, completion_tick=91, per_tick_delta=0.050, degrade_flags=∅`. Updating a golden value is explicit + provenance-stamped (V11 §6.4).

---

## 6. Validation Rules

V-1 Hash stability (each `*_hash`==golden on recompile) · V-2 Chain completeness (every chain hash produced, non-degraded) · V-3 Behavior determinism (`Idle→WalkTo`@1, unambiguous) · V-4 Physics determinism (constant `0.050`/tick, no drift, bound to `body_capsule`) · V-5 Mission correctness (O1 active@1, complete@N, terminal `complete`) · V-6 Sim-tick correctness (`worldstate_hash@N` every tick, distinct, monotonic in tick, ==golden) · V-7 Replay parity self (I-REPLAY-EQ) · V-8 Replay parity backends (I-DIVLOC) · V-9 Read-only purity (drive via funnels only — here none; read hashes only) · V-10 Termination (complete by `2N`). All verdicts independent of wall-clock/machine/thread (I-EXP).

---

## 7. Degrade-Never-Fail Model

**Principle:** if any subsystem is missing/invalid the slice MUST still (a) spawn the entity, (b) complete the mission, (c) progress `worldstate_hash@N` deterministically. A degraded run is a valid, reproducible run with a **distinct golden bundle** (`degrade_flags≠∅`); the test fails only on divergence from the (degraded or clean) golden, never on degradation itself.

| Layer | If missing/invalid | Fallback |
|---|---|---|
| V1 rig | uncertifiable | generic biped placeholder from bounds (flagged); entity spawns |
| V2 deform | absent | base skin, no twist/correctives; **synthetic `body_capsule`** from bounds (`synthetic=true`) |
| Retarget | `Walk_Forward` unresolved | nearest clip → pose-hold; movement still occurs; IK falls back to fixed cadence |
| V3 entity | unrealizable | inert placeholder capsule at `P_spawn` exposing the interface; spawns |
| V3 scene | spawn missing | spawn at origin; `T_target` missing → objective auto-complete-safe |
| V3 mission | objective unbindable | `obj.reach` → optional/auto-complete after a fixed tick budget → mission completes |
| V4 AI | graph unrealizable | deterministic scripted straight-line advance (`0.050`/tick); else auto-complete at budget |
| V4 physics | absent | kinematic move (no collision) at `0.050`/tick |
| V4 nav | absent | straight-line clamped to cell |
| V5 sim | stub absent | minimal deterministic tick counter still emits `worldstate_hash@N=H(frozen state, tick N)` → progression preserved |

**Degrade invariants:** the fully-degraded run still produces the complete hash chain + full `worldstate_hash@0…N` ending in `mission=complete`; it is deterministic/reproducible (degrade flags + fallback values hashed); degraded vs clean goldens are distinct comparable artifacts; degrades surface as `info`/`DEGRADE`, never a silent pass disguised as the clean run.

---

## Appendix A — Identity chain fired
```
rig_hash → deform_hash → anim_hash → behavior_hash → entity_hash → scene_hash → mission_hash
  → physics_hash → nav_hash → ai_hash → gameplay_hash → sim_hash → worldstate_hash@0…N
```

## Appendix B — Canonical tick timeline
| Tick | Phase | Z (quantized) | ai_state | O1 | Note |
|---|---|---|---|---|---|
| 0 | spawn/idle | 0.000 | Idle | inactive | `worldstate_hash@0` |
| 1 | mission begins; transition | 0.000 | Idle→WalkTo | active | transition tick (==1) |
| 2 | walking | 0.050 | WalkTo | active | first step |
| … | walking | 0.050×(N−1) | WalkTo | active | constant `0.050`/tick |
| 91 | enter target | 4.500 | WalkTo | **complete** | dist 0.500=radius; `on_reach` |
| **N=91** | final | 4.500 | (idle) | complete | `worldstate_hash@N` |

Constants: `1.500 u/s ÷ 30 = 0.050 u/tick`; `5.000 − 0.500 = 4.500` → 90 movement ticks + 1 transition ⇒ `N=91`; `tick_budget=182`.

## Appendix C — Divergence taxonomy → first-look subsystem
position/delta → V4 physics/nav (or V2 proxy) · anim/foot-plant → Retarget/V2 IK · ai_state tick → V4 AI · O1/completion → V3 mission wiring / V4 `on_reach` · tick/layout → V5 tick-barrier/schema · an upstream `*_hash` → V1/V2/V3 identity.

---

**End of `E2E_0001_SpawnWalkComplete` Test Specification.**
