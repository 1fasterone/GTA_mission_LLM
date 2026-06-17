# V2 SPECIFICATION — Full Deformation Stack

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V2 (Deformation Certification)
**Status:** Normative specification
**Depends on:** V1 (Rig Correctness + CAS)

> **Provenance:** authored to complete the V1–V13 set; V2 was the assumed foundation referenced by
> every later spec (proxies, IK chains, deform_hash). Written here in the shared format and consistent
> with all downstream references.

---

## 0. Normative Conventions (inherited from V1)

**0.1 Requirement language.** `MUST`/`MUST NOT` = hard invariants. `SHOULD` = overridable only by a hashed policy flag. `MAY` = permitted. `DEGRADE` = mandatory reproducible fallback.

**0.2 Compiler, not tool.** V2 is a pure transform `compile(rig + deform_source, policy) → certified_deform + identity`. No DCC, no wall-clock, no ambient RNG, no path/host/order dependence.

**0.3 Layering invariant.** V2 never re-certifies V1 facts. It consumes `rig_hash` and `asset_hash` as trusted, immutable leaves. **V2 certifies deformation correctness**: that the rig deforms its mesh correctly through a fixed, ordered stack, and publishes the IK chains and collision proxies that V4/V5/V6/V7/V12 depend on.

**0.4 Micro-type model + `clamp_enum`.** Composable, versioned deformation micro-types; immutable per `(id,version)`; every enum field uses `clamp_enum`.

**0.5 Determinism floor.** Canonical IR: NFC strings, fixed-width ints, quantized floats (round-half-even), canonically-ordered collections, no paths/timestamps/host ids.

**0.6 The Ordered-Stack Principle (I-DEFORM — the defining V2 rule).** Deformation is certified as a **fixed, ordered evaluation stack**: `scaling → twist → correctives → IK → proxies`. The order is normative and hashed; correctness is defined *relative to that order*. Because the order is fixed and every operator is deterministic over quantized inputs, the deformed result is a pure function of `(rig_hash, deform_source, pose)`. Downstream layers consume the certified outputs (IK chains, proxies) and the certificate `deform_hash`; they never re-derive deformation.

---

## 1. V2 Architectural Overview

### 1.1 Purpose

V1 certified that a rig is *structurally* correct; V1 says nothing about whether it *deforms* correctly. V2 certifies the full deformation stack — that scaling, twist distribution, corrective shapes, IK, and collision proxies all evaluate correctly and deterministically — and emits `deform_hash` plus the IK-chain and proxy contracts the rest of the stack relies on.

| System | Produces | Identity |
|---|---|---|
| Deformation Stack | certified scaling/twist/correctives/IK/proxies | `deform_hash` |
| IK Contract | named IK chains (consumed by V4 AI/physics, V6 camera) | (part of `deform_hash`) |
| Proxy Contract | collision/hit proxies (consumed by V4 physics/combat, V6 camera) | (part of `deform_hash`) |

### 1.2 Consumption of V1 outputs

```
inputs := { rig_hash (V1 certified skeleton), asset_hash (mesh + skin data), deform_source (weights, twist, correctives, IK defs, proxy defs) }
```

V2 references V1 by hash; it binds deformation to the certified joints/sockets and never edits the rig.

### 1.3 Phase ordering and dependencies (the stack is the order)

```
   V1 rig_hash + asset_hash + deform_source
        │
   P1 ── Scaling ───────► canonical scale resolution (non-uniform, per-joint)
        │
   P2 ── Twist ─────────► twist distribution along chains (forearm/upperarm/spine)
        │
   P3 ── Correctives ───► pose-driven corrective shapes (PSD / blendshapes)
        │
   P4 ── IK ────────────► IK chain definitions + solver order (foot/hand/look-at)
        │
   P5 ── Proxies ───────► collision/hit proxies derived from the deformed body
        │
   deform_hash = H(scaling, twist, correctives, ik, proxies, rig_hash, asset_refs, stack_order)
```

The phase order **is** the deformation evaluation order (I-DEFORM). No back-edges: correctives may read scaled/twisted pose; IK reads the corrected pose; proxies derive from the final deformed body.

### 1.4 Deterministic identity

```
deform_hash = H("v2.deform",
    scaling_hash, twist_hash, correctives_hash, ik_hash, proxy_hash,
    rig_hash, sorted(asset_refs), stack_order, quantization_policy)
```

Invariant **I-V2-LOCAL**: changing one corrective/IK chain/proxy changes exactly the hashes on its path to `deform_hash`; `rig_hash` is untouched (V2 never edits the rig).

---

## 2. The Deformation Stack (P1–P5)

### 2.1 Scaling (P1)

| Micro-type | Role |
|---|---|
| `def.scale_uniform` / `def.scale_nonuniform` | per-joint scale resolution (quantized) |
| `def.scale_compensation` | child-compensation to prevent shear |

Scaling resolves deterministically to a canonical per-joint scale set; non-uniform scale is compensated to avoid skew. Output feeds twist (P2).

### 2.2 Twist (P2)

| Micro-type | Role |
|---|---|
| `def.twist_chain` | distributes roll along a chain (forearm, upper-arm, spine, neck) |
| `def.twist_weight` | per-joint twist weights (quantized) |

Twist distribution is a fixed function of the scaled pose; weights are quantized. Output feeds correctives (P3).

### 2.3 Correctives (P3)

| Micro-type | Role |
|---|---|
| `def.corrective_psd` | pose-space-deformation corrective shapes |
| `def.blendshape` | named blendshape targets |
| `def.driver` | pose → corrective activation (quantized curves) |

Correctives are driven by the scaled+twisted pose via deterministic, quantized driver curves (no runtime float ambiguity). Output feeds IK (P4).

### 2.4 IK (P4)

| Micro-type | Role |
|---|---|
| `def.ik_chain` | a named IK chain (arm_r, leg_l, look_at) with fixed solver + iteration count |
| `def.ik_pole` | pole/target definitions |
| `def.ik_blend` | FK/IK blend weights (quantized) |

IK chains are **named and published** as the IK contract consumed by V4 (gameplay IK), V6 (camera look-at) and V12 (the VM's deterministic IK solver — the reference). Solver order + iteration count are fixed and hashed (so runtime IK is deterministic). Output feeds proxies (P5).

### 2.5 Proxies (P5)

| Micro-type | Role |
|---|---|
| `def.proxy_capsule` / `def.proxy_box` / `def.proxy_sphere` | collision/physics proxies |
| `def.proxy_hitbox` | per-region hit proxies (head/torso/limb) for V5 combat |
| `def.proxy_attach` | grip/attach proxies (hand_grip, wheel mounts) |

Proxies are derived deterministically from the fully-deformed body (quantized bounds). They are **the physical truth** consumed by V4 physics, V5 combat hitbox/hurtbox, V6 camera collision, and V7/V12 networked physics. A missing proxy downstream is the trigger for the *synthetic proxy* DEGRADE used throughout the stack (capsule/box from quantized rig bounds, `synthetic=true`).

---

## 3. Multi-Backend Export Rules (Unity, Unreal, Blender, USD)

| Concept | Unity | Unreal | Blender | USD |
|---|---|---|---|---|
| Skin/weights | SkinnedMeshRenderer | Skeletal Mesh skinning | Armature deform/vertex groups | `UsdSkelBindingAPI` |
| Twist/correctives | blendshapes + rig | morph targets / AnimBP | shape keys + drivers | `BlendShape` + custom attrs |
| IK chains | IK rig / Animation Rigging | Control Rig | rig constraints | `UsdSkel` + custom IK attrs |
| Proxies | Colliders | Physics Asset bodies | rigid-body/collision objects | `UsdPhysics` colliders |

Export is identity-preserving lowering; IK and proxy contracts lower to each engine's native systems, with backend solver differences delegated (inputs pinned) and audited later by V11.

---

## 4. Determinism, CAS, and Skip-Work

- **Stack order is fixed and hashed** (I-DEFORM): scaling→twist→correctives→IK→proxies.
- **Quantize-before-hash:** weights, blendshape activations, IK params, proxy bounds all quantized.
- **Skip-work:** an unchanged deformation reuses `deform_hash` from CAS; editing one corrective recomputes only `correctives_hash` → `deform_hash`.
- **Partial recompilation:** a proxy edit recomputes `proxy_hash` only; IK/correctives untouched. A `rig_hash` change (upstream) re-runs the deform stack for that rig only.

---

## 5. Validation & Degrade-Never-Fail Model

**Theorem (Total Deformation).** Any rig passing V1 can ALWAYS be certified into a valid `deform_hash` — possibly with simplified twist/correctives or synthetic proxies, but always a valid deformation contract with usable IK chains and proxies. V2 has **no content-induced hard-fail path**; the only hard errors are compiler bugs caught in CI.

**Degradation ladders:**
- **Scaling/twist:** invalid → identity scale / no twist (rig still deforms via base skin).
- **Correctives:** invalid driver/shape → drop the corrective (base skin preserved).
- **IK:** unsolvable chain → disable that chain (FK fallback), recorded; missing chain downstream → coupling disabled (V4 rule).
- **Proxies:** missing/invalid → **synthetic proxy** from quantized bounds (`synthetic=true`) — the canonical fallback reused by V3 §3.5.3, V4, V5, V6 throughout.

**Guarantee:** every rig passing V1/V2 yields a valid, deterministic deformation contract — so "deformation correctness" is certified once, here, and the IK/proxy contracts are trusted by every layer above.

---

## Appendix — V2 Identity Chain

```
V1 rig_hash + asset_hash
  └─ scaling_hash → twist_hash → correctives_hash → ik_hash → proxy_hash
       └─ deform_hash (certifies the ordered stack; publishes IK + proxy contracts)
```

**End of V2 Specification — Full Deformation Stack.**
