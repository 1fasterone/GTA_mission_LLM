# V1 SPECIFICATION — Rig Correctness & Content-Addressed Substrate

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V1 (Substrate & Structural Certification)
**Status:** Normative specification
**Depends on:** nothing (this is the root layer)

> **Provenance:** authored to complete the V1–V13 set; V1 was the assumed foundation referenced by
> every later spec. Written here in the shared format and consistent with all downstream references.

---

## 0. Normative Conventions

**0.1 Requirement language.** `MUST`/`MUST NOT` = hard invariants (violation = compiler bug). `SHOULD` = default overridable only by a hashed policy flag. `MAY` = permitted. `DEGRADE` = mandatory fallback converting a would-be failure into a valid, simplified, reproducible output.

**0.2 Compiler, not tool.** V1 is a pure transform `compile(asset_in, policy) → certified_substrate + identity`. No DCC, no wall-clock, no ambient RNG, no path/hostname/iteration-order dependence. Output is a function of hashed inputs only.

**0.3 Foundational role.** V1 establishes the two things every other layer relies on: (a) **structural/rig correctness certification** (the geometry/skeleton is well-formed and canonical), and (b) the **content-addressed substrate** (CAS, hashing, skip-work) that the entire V1–V13 identity chain is built on. V1 has no layer below it; it is the root of the Merkle chain.

**0.4 Micro-type model + `clamp_enum`.** All V1 capability is composable, versioned micro-types (skeleton/joint/constraint/asset). Each declares inputs/outputs/constraints/deps/tags/degrade; immutable per `(id,version)`. Every enum field uses `clamp_enum`. New capability = new micro-types, never engine rewrites.

**0.5 Determinism floor.** Hashes are over **canonical IR**: UTF-8/NFC strings; fixed-width ints; floats quantized to fixed tolerance (round-half-even); semantically-unordered collections sorted by declared canonical key; no paths/timestamps/host identifiers.

**0.6 The Content-Address Principle (I-CAS — the defining V1 rule).** Every artifact is identified by a hash computed over its **canonical IR + the hashes of its dependency closure** — and by *nothing else* (no path, no project, no machine, no timestamp). Consequences exploited by every later layer:
- **Identity = content.** Two byte-identical artifacts have the same hash; a one-bit change yields a new hash. Identity is intrinsic, not assigned.
- **Merkle composition.** A composite's hash is `H(domain_tag, ordered child hashes + own canonical fields)`; changing one leaf changes only the hashes on its path to the root.
- **Skip-work.** `if CAS.contains(h): reuse else compute` is sound because hashes are pure over canonical inputs and the compiler is side-effect-free.
- **Project-independence.** Because no project identity is in a hash, the CAS is inherently global — the basis of V13 cross-game interop (I-CAU).

---

## 1. V1 Architectural Overview

### 1.1 Purpose

Before anything can be assembled, deformed, animated, or simulated, its **structural foundation must be certified correct and given a stable identity**. V1 ingests raw assets (skeletons/rigs, meshes, textures, audio), normalizes them to canonical form, certifies their structural correctness, and stores them in the content-addressed substrate — emitting `rig_hash` and `asset_hash` as the immutable leaves of the whole stack.

| System | Produces | Identity |
|---|---|---|
| Asset Substrate (CAS) | normalized, content-addressed asset store | `asset_hash` |
| Rig Correctness | certified skeleton/rig contract | `rig_hash` |
| Identity & Skip-Work | the Merkle hashing + dedup engine | (the substrate for all layers) |

### 1.2 Inputs and consumption

V1 is the root; it consumes **external assets**, not prior layers:

```
inputs := { skeleton/rig files, meshes, textures/materials, audio, nav-source geometry }
```

All inputs are **normalized deterministically** (units, axes, naming, ordering) and hashed into the CAS. The same external file imported twice yields the same hash (deterministic normalization is a MUST — §6).

### 1.3 Phase ordering and dependencies

```
   external assets
        │
   P1 ── Asset Ingestion & Normalization ──► asset_hash    (canonicalize → CAS)
        │
   P2 ── Rig Correctness Certification ─────► rig_hash      (skeleton contract; structural validation)
        │
   P3 ── Identity & Skip-Work Substrate ────► (CAS + Merkle hashing; consumed by V2–V13)
```

### 1.4 Deterministic identity

```
asset_hash = H("v1.asset", asset_kind, canonical(normalized payload), normalization_policy)
rig_hash   = H("v1.rig",   sorted(joint records), hierarchy, sorted(constraint records),
                           bind_pose (quantized), naming_contract, asset_refs)
```

Invariant **I-V1-LOCAL**: changing one joint/asset changes exactly the hashes on its path to the root.

---

## 2. Asset Substrate & CAS (P1)

### 2.1 Asset micro-types

| Micro-type | Normalizes | Gate |
|---|---|---|
| `asset.mesh` | geometry: axes, units, winding, vertex order | manifold/closed checks; quantized positions |
| `asset.texture` | color space, resolution policy | format validity |
| `asset.audio` | sample rate, channels | format validity |
| `asset.skeleton` | joint set + hierarchy (feeds rig, §3) | structural checks |
| `asset.navsource` | walkable geometry for downstream nav | geometry validity |

### 2.2 Canonical normalization (MUST)

Ingestion applies a fixed normalization: coordinate axes to the canonical frame; units to canonical scale; deterministic naming (via the naming contract); canonical element ordering (vertices/joints sorted by a declared key); float quantization before hashing. Normalization is **pure and idempotent** — re-normalizing a normalized asset is a no-op, and the same source always yields the same `asset_hash`.

### 2.3 Content-addressed storage

Each normalized asset is stored in the CAS keyed by `asset_hash`; duplicates dedup automatically. The CAS is the single store shared by all layers (and, per V13, across projects).

### 2.4 Deterministic asset hashing — see §1.4 (`asset_hash`).

---

## 3. Rig Correctness Certification (P2)

### 3.1 Purpose

Certify that a skeleton/rig is **structurally correct** and conforms to a canonical rig contract that downstream layers (V2 deformation, Retarget, V3 entity sockets) can rely on.

### 3.2 Rig micro-types

| Micro-type | Represents |
|---|---|
| `rig.joint` | a bone: name (canonical role), parent, local bind transform (quantized) |
| `rig.hierarchy` | the parent/child DAG (single root; acyclic) |
| `rig.constraint` | structural constraints (limits, look-at, parent constraints) |
| `rig.socket` | named attachment points (hand_r, head, …) exposed to V3 |
| `rig.class` | the rig's class (humanoid/quadruped/vehicle/…) via `clamp_enum` |

### 3.3 Correctness rules (certified, DEGRADE never hard-fail)

- **Single-rooted, acyclic hierarchy** (a cycle is repaired deterministically or the asset DEGRADES to a flagged placeholder rig).
- **Canonical joint roles** resolved via the naming contract (unmapped joints → nearest role or generic, recorded).
- **Bind pose well-formed** (no degenerate transforms; quantized).
- **Class assignment** via `clamp_enum` (unknown → nearest known class).
- **Socket surface** published for V3 (missing expected socket → nearest ancestor joint, recorded).

A rig that cannot be fully certified DEGRADES to a **flagged but structurally valid** rig (e.g. a generic biped skeleton from bounds) — so downstream layers always receive a usable `rig_hash`.

### 3.4 Deterministic rig hashing — see §1.4 (`rig_hash`).

---

## 4. Identity & Skip-Work Substrate (P3)

### 4.1 The hashing engine

V1 defines the single hash primitive + canonicalizer used by **every** layer: domain-separated `H(tag, …)` over canonical IR. All downstream `*_hash` values are Merkle compositions rooted at `rig_hash`/`asset_hash`.

### 4.2 Skip-work

```
for each work unit U with hash h:
   if CAS.contains(h): reuse CAS[h]
   else:               compute U; CAS.put(h)
```

Sound by I-CAS. This is the mechanism every later layer's "partial recompilation" and "skip-work" sections inherit.

### 4.3 The identity chain seed

`rig_hash`/`asset_hash` are the immutable leaves; V2 composes `deform_hash` over them, V3 composes `entity/scene/mission`, and so on up to V13. The whole tower is one Merkle DAG anchored here.

---

## 5. Multi-Backend Export Rules (Unity, Unreal, Blender, USD)

| Concept | Unity | Unreal | Blender | USD |
|---|---|---|---|---|
| Skeleton/rig | Avatar / SkeletonAsset | Skeleton asset | Armature | `UsdSkel` skeleton |
| Mesh | Mesh asset | Static/Skeletal Mesh | Mesh datablock | `UsdGeomMesh` |
| Socket | Transform/socket | Socket | Empty | child `Xform` prim |
| Identity | GUID derived from `rig_hash`/`asset_hash` | asset tag | ID custom prop | `customData` hash |

Export is identity-preserving lowering; backend GUIDs derive deterministically from content hashes, so references are stable across recompiles and DCC round-trips.

---

## 6. Determinism, CAS, and Skip-Work

- **Deterministic normalization (MUST):** same source asset ⇒ same `asset_hash`/`rig_hash`, on any machine.
- **Quantize-before-hash:** all floats quantized (positions `q_pos`, rotations canonicalized quaternions `q_rot`, scale `q_scale`) before hashing.
- **Canonical ordering:** joints/vertices/constraints sorted by declared keys; the *authoring* order is never hashed.
- **Skip-work:** unchanged assets/rigs are pure CAS hits (the foundation of incremental builds in V9 and portfolio dedup in V13).
- **Partial recompilation:** editing one joint recomputes that `rig.joint` hash → `rig_hash`; unaffected assets untouched.

---

## 7. Validation & Degrade-Never-Fail Model

**Theorem (Total Substrate).** Any input asset can ALWAYS be normalized and certified into a valid `asset_hash`, and any skeleton into a valid `rig_hash` — possibly a flagged/repaired placeholder, but always structurally valid and content-addressed. V1 has **no content-induced hard-fail path**; the only hard errors are compiler/registry bugs caught in CI.

**Degradation ladders:**
- **Asset:** invalid geometry → deterministic repair (re-orient/re-weld) → flagged placeholder asset.
- **Rig:** cyclic/invalid hierarchy → deterministic repair → generic class skeleton from bounds (flagged).
- **Class/role/socket:** unknown → `clamp_enum` nearest / nearest ancestor (recorded).

**Guarantee:** every asset passing ingestion is a valid, content-addressed substrate leaf that V2–V13 can build on — so "structural correctness" is certified once, here, and trusted everywhere above.

---

## Appendix — V1 Identity Chain (root)

```
external assets
  └─ asset_hash (P1: normalized, content-addressed)
       └─ rig_hash (P2: certified skeleton contract)
            └─ (P3: CAS + Merkle hashing + skip-work) → seeds the entire V1–V13 chain
```

**End of V1 Specification — Rig Correctness & Content-Addressed Substrate.**
