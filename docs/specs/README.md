# Deterministic Multi-Backend Game-Assembly Compiler — Layer Specifications (V1–V13)

This directory holds the normative layer specifications for a **deterministic, multi-backend
game-assembly compiler**. Each layer (`V1`…`V13`) certifies one concern and feeds the next via a
content-addressed (CAS) Merkle hash chain. Backends targeted throughout: **Unity, Unreal, Blender, USD**.

> **Provenance note.** Specs **V3–V13** were authored as full design documents in a working session
> (2026-06-16). Specs **V1** and **V2** were the *assumed foundation* that every later spec builds on;
> they are written here to complete the set, in the same format and consistent with all cross-references.

## The one spine

Every layer reuses the same five mechanisms:

1. **Determinism** — canonical IR, quantize-before-hash, fixed tolerances, canonical ordering, hash-seeded RNG.
2. **CAS** — every artifact is content-addressed; identity is a Merkle composition over the dependency closure.
3. **Micro-types** — capability is composable, versioned micro-types; new capability = new micro-types, never engine rewrites.
4. **`clamp_enum`** — every exposed enum/field clamps to a valid value (out-of-range → nearest + DEGRADE), never throws.
5. **Degrade-never-fail** — no content-induced hard-fail path; every defect degrades to a valid, reproducible, backend-safe output.

## Layer index

| Layer | Title | Root hash | Defining invariant(s) |
|---|---|---|---|
| [V1](V1_rig_correctness.md) | Rig Correctness + Substrate | `rig_hash`, `asset_hash` | structural certification; CAS; skip-work |
| [V2](V2_deformation_stack.md) | Full Deformation Stack | `deform_hash` | ordered stack: scaling→twist→correctives→IK→proxies |
| [V3](V3_entity_scene_mission.md) | Entity/Scene/Mission Assembly | `entity_hash`, `scene_hash`, `mission_hash` | degrade-never-fail assembly |
| [V4](V4_gameplay_systems.md) | Gameplay Systems | `gameplay_hash` | Gameplay Determinism Contract |
| [V5](V5_systemic_simulation.md) | Systemic Simulation & Emergent World | `sim_hash` | tick-barrier double-buffer; snapshot-addressable world |
| [V6](V6_presentation.md) | Presentation / Camera / UI/HUD | `presentation_hash` | Read-Only Projection (I-PROJ); Strippable (I-STRIP) |
| [V7](V7_networking.md) | Networking / Replication / Authority | `net_hash` | Two-Truths (I-AUTH); Offline-Equivalence (I-OFFLINE) |
| [V8](V8_ugc_liveops.md) | UGC / Mod Ecosystem / Live-Ops | `liveops_hash`, `config_hash` | Capability-Boundary (I-CAP); Reversibility (I-REV) |
| [V9](V9_toolchain_editor.md) | Toolchain / Editor / Authoring UX | `editor_hash` | Operation-Determinism; Edit-as-Compiler-Input; Snapshot-Reversibility |
| [V10](V10_analytics_adaptive.md) | Analytics / Telemetry / Adaptive Balancing | `analytics_hash` | Observer-Purity (I-OBS); Bounded-Reproducible-Adaptation (I-ADAPT) |
| [V11](V11_qa_replay.md) | Automated QA / Fuzzing / Deterministic Replay | `qa_hash` | Replay-Equivalence (I-REPLAY-EQ); Divergence-Localization (I-DIVLOC) |
| [V12](V12_runtime_vm.md) | Runtime VM / Hot-Reload / Live Debugging | `runtime_hash` | Reference-Executor (I-VMDET); Mutation-as-Recorded-Transition (I-LIVE) |
| [V13](V13_cross_game_interop.md) | Cross-Game Interop / Multi-Project Asset Graphs | `interop_hash` | Content-Address Universality (I-CAU); Port-as-Pure-Rebinding (I-PORT) |

## The identity chain

```
asset/rig/deform/anim (V1/V2/Retarget)
  └─ entity ─ scene ─ mission (V3)
       └─ gameplay (V4)
            └─ sim (V5)
                 └─ presentation (V6)        ← read-only, strippable
                      └─ net (V7)            ← detachable wrapper; offline == networked for V1–V6
                           └─ liveops/config (V8)   ← reversible authoring/evolution wrapper
                                [└─ editor (V9)]      ← authoring environment (drives the chain)
                                └─ analytics (V10)    ← detachable observer + bounded adapter
                                     └─ qa (V11)      ← verifies the chain; qa_fp CONTAINS build_hash(V1–V10)
                                          └─ runtime (V12)  ← executes build_hash; PRODUCES worldstate_hash@N
                                               └─ interop (V13)  ← spans multiple projects via the global CAS
```

`build_hash` composes the per-layer roots; the global CAS is project-independent (V13 I-CAU), so a
studio's whole catalog deduplicates against one store.

## Conventions (shared by all specs)

- **Normative language:** `MUST`/`MUST NOT` (hard invariant), `SHOULD` (overridable only by a hashed
  policy flag), `MAY`, `DEGRADE` (mandatory reproducible fallback).
- **Compiler, not tool:** each layer is a pure transform `compile(IR_in, policy) → IR_out + identity`;
  it emits *specifications*, not interactive DCC behavior.
- **Determinism floor:** UTF-8/NFC strings; fixed-width ints; quantized floats (round-half-even);
  canonically-ordered collections; no paths/timestamps/host identifiers in hashed IR.

See the project memory note `gta-v-layer-scheme` for the condensed one-line-per-layer definitions.
