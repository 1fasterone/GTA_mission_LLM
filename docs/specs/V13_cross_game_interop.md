# V13 SPECIFICATION — Cross-Game Interop & Multi-Project Asset Graphs

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V13 (Cross-Project Interoperability)
**Status:** Normative specification
**Depends on:** V1–V12, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V12)

**0.1–0.5** As prior layers. **V13 emits deterministic cross-project asset-graph + compatibility + port specifications; it does not move files between machines** (transport delegated, §8).

**0.6 The Content-Address Universality Principle (I-CAU — defining rule, and the dividend of the whole stack).** Since V1 every artifact is identified by a content hash over canonical IR + dependency closure, with **no project/path/machine**. Therefore: a content hash is **project-independent** (the CAS is global); interop is **shared reference, not copying** (two titles sharing a rig both reference the same `rig_hash`, stored once); the only real work is **context reconciliation** (content referencing project-local context must have those references *rebound* to the target's equivalents). Content-addressing — a determinism/skip-work mechanism — *already is* a universal cross-project namespace.

**0.7 The Port-as-Pure-Rebinding Principle (I-PORT).** Porting A→B is a pure function `port(content, ctx_map: A→B) → content'`: **identity-port** (no A-local context) → `content' == content` (same hash, zero transformation); **rebinding-port** (context-bound) → a new artifact `H(source_hash, canonical(ctx_map))`. Reversible + snapshot-addressable.

**0.8 The Dependency-Closure Portability Invariant (I-CLOSURE).** Content ports only with its full dependency closure (or compatible substitutes resolvable in the target). No dangling cross-project references; unresolved dependency → local fallback (I-LOCAL).

**0.9 The Degrade-to-Local Invariant (I-LOCAL — the V13 degrade floor).** Any cross-project reference that cannot be reconciled (no compatible class, unfillable role, unsupported backend, missing dependency) DEGRADES to a safe local-project fallback. **The importing project always remains valid, self-contained, and deterministic** even with all interop stripped — a project never depends on another for its own validity.

---

## 1. V13 Architectural Overview

### 1.1 Purpose
Content flows between projects: losslessly where self-contained (I-CAU), safely-rebound where context-bound (I-PORT), never at the cost of either project's determinism or self-sufficiency (I-LOCAL).

| System | Produces | Identity |
|---|---|---|
| Project Identity & Namespacing | project roots + label scoping | `project_hash` |
| Multi-Project Asset Graph | the global cross-project dependency DAG | `assetgraph_hash` |
| Cross-Game Compatibility | class/schema compatibility + rebinding contracts | `compat_hash` |
| Shared Content Libraries | curated portable CAS bundles | `library_hash` |
| Cross-Title Portability | role-abstracted portable missions/sims | `port_hash` |
| Conflict Resolution & Merge | cross-project merge/override/patch | `interopmerge_hash` |
| Multi-Backend Export | cross-project backend compatibility | `backendinterop_hash` |

Root: `interop_hash`.

### 1.2 Consumption of V1–V12 outputs
**Interop Resolution Table (IRT)** spanning multiple projects: per-project `build_hash`; the global CAS (already project-independent — I-CAU); V1/V2 substrate (most portable); V3 entities (portable) + scenes/missions (context-bound); V4 behaviors (portable over shared vocabulary); V5 sim graphs (context-bound); the shared micro-type registry (the lingua franca); V8 packaging/merge (libraries REUSE this); V11 qa_hash (ported content re-verified in the target). `ugc_fp := {source project_hash, target project_hash, referenced content hashes, ctx_map?, registry_version}`.

### 1.3 Phase ordering (compile; project identity logically first)
```
P1 Project Identity & Namespacing (project_hash) → P2 Multi-Project Asset Graph (assetgraph_hash)
  → P3 Cross-Game Compatibility (compat_hash) → P4 Shared Content Libraries (library_hash)
  → P5 Cross-Title Portability (port_hash) → P6 Conflict Resolution & Merge (interopmerge_hash) → P7 Multi-Backend Export (backendinterop_hash)
interop_hash = H(project, assetgraph, compat, library, port, interopmerge, backendinterop, ugc_fp, interop_determinism_contract, versions)
```
I-CAU/I-PORT/I-CLOSURE/I-LOCAL hold throughout. (Document body follows the requested section order, with project identity at §6.)

### 1.4 Identity
```
interop_hash = H("v13.interop", project_hash(s), assetgraph_hash, compat_hash, library_hash, port_hash, interopmerge_hash, backendinterop_hash, ugc_fp, interop_determinism_contract_hash, versions)
```
**I-IO-LOCAL**: one port/compat rule change touches only its path. **Identity-ported content changes no hash at all** (I-CAU) — same artifact, two projects.

---

## 2. Multi-Project Asset Graph (P2)

### 2.2 Asset-graph micro-types
`ag.rig/mesh/asset/anim` (fully/largely portable), `ag.behavior` (portable over shared vocabulary), `ag.entity` (context-light), `ag.scene` (context-bound → rebinding-port), `ag.mission`/`ag.simulation` (context-bound → role-abstracted port §5).

### 2.3 Cross-project dependency graph
```
asset_graph { nodes: sorted set<ag_node>; edges: sorted set<dep edge>; projects: sorted set<project_hash>; version_constraints: per-edge {min, max, exact?} }
ag_node { content_hash, micro_type, portability_class, providing_project(s), closure_root }
```
A global Merkle DAG keyed by content hash; shared nodes appear **once**, referenced by N projects (I-CAU global dedup). Closure-complete by construction (I-CLOSURE — no node with a dangling dependency). Unsatisfiable version constraint → nearest satisfiable / local fallback.
```
assetgraph_hash = H("v13.assetgraph", sorted(ag_node by content_hash), sorted(dep edges), sorted(version constraints), project membership map, ugc_fp)
```

---

## 3. Cross-Game Compatibility Rules (P3)

### 3.2 Micro-types
`compat.rig_class/behavior_class/mission_schema/simulation_schema/asset_format`.

### 3.3 Clamp-enum compatibility
Exact match → identity-port (hash unchanged where context-free); nearest-compatible → rebind to the closest target class (humanoid→biped; `combat.ranged_hitscan`→`combat.ranged_projectile`), recorded DEGRADE → new rebound hash; no compatible class → DEGRADE to local fallback (placeholder of nearest local kind, I-LOCAL).

### 3.4 Forward/backward compatibility (= V8 §6, cross-project)
unknown field → ignore; out-of-range/renamed class → clamp; newer-than-target → downgrade; older → migrate forward. Unknown ⇒ ignore/clamp/local, never crash.
```
compat_hash = H("v13.compat", sorted(compatibility rule defs), source↔target class maps, ugc_fp, degrade_decisions)
```

---

## 4. Shared Content Libraries (P4)

Library micro-types `lib.animation_pack/behavior_pack/mission_pack/simulation_pack/asset_pack` — structurally V8 mod bundles, cross-project + first-party-curated.
```
library_manifest { library_id, library_version (semver, hashed); contents: sorted set<content_hash> (closure-complete);
                   dependencies: sorted set<{library_id, version_range}>; compatibility: registry/spec range + backend set; portability_class: per-content }
library_hash = H("v13.library", library_manifest, sorted(contents))   # Merkle root, global CAS
```
Content-addressed + dependency-closed (I-CLOSURE), globally deduplicated (I-CAU). Versioning + packaging reuse V8 §3 (a project consumes a library exactly as a mod layer).

---

## 5. Cross-Title Mission & Simulation Portability (P5)

Abstract over context: reference targets by *role/class*, not concrete hash; porting **binds role-slots** to the target's concrete content.
```
portable_mission { template: role-abstracted mission graph over ROLE-SLOTS; role_slots: sorted set<role>; bindings: ctx_map: role → target concrete content (by compat §3) }
portable_simulation { modules: role-abstracted V5 sim subgraphs over ROLE-SLOTS; hook_slots: sorted set<world_hook_role>; bindings: ctx_map: hook_role → target V5 hooks; bounds: inherited V5 clamps }
```
Unfillable role → template ladder → trivially-completable stub (mission) / zero influence (sim) (I-LOCAL). A ported sim inherits V5 boundedness — cannot destabilize the target. Identity-portable (no role-slots) ports by hash (I-CAU); role-abstracted port → `H(template_hash, canonical(bindings))` (I-PORT). Ported content re-verified by the target's V11.
```
port_hash = H("v13.port", sorted(portable_mission/simulation defs), ctx_map resolution policy, degrade ladder refs, ugc_fp, degrade_decisions)
# a concrete ported mission/sim → a normal V3 mission_hash / V5 sim_hash in the target, keyed by H(template, bindings)
```

---

## 6. Project-Level Identity & Namespacing (P1)

Real identity is the content hash (globally unique by I-CAU); namespacing is only for human-facing labels and stable references.
```
project { project_id; project_hash = H(project_id, sorted(root content hashes), config_hash, registry_version); namespace: project_id:: prefix; registry_ref }
```
Labels namespaced (`projectA::guard_alpha`); hashes are not (already globally unique). Label collisions resolve by deterministic project priority (hashed load order), tie-broken by `project_hash`. **Content collisions do not exist** (same hash = same content → CAS dedup). A cross-title reference uses the namespaced label for readability but resolves to the global content hash for identity.

---

## 7. Conflict Resolution & Merge Rules (P6)

Content collisions impossible (I-CAU) → conflicts are namespace/label + context-binding, resolved by the V8 §7 model.
```
iomerge.add | override | extend | patch   (priority-ordered; commutative where possible)
conflict := two sources bind the same namespaced label / rebind the same role incompatibly
resolution ladder: 1 priority (hashed project/library load order) → 2 fallback (canonical by (project_hash, content_hash)) → 3 isolation (drop lower-priority; consuming project stays valid, I-LOCAL)
```
**I-IOMERGE-DET**: identical source set + priority ⇒ identical `interopmerge_hash`. Floor = isolate-to-local.
```
interopmerge_hash = H("v13.interopmerge", ordered(source priority: projects + libraries), sorted(detected conflicts + resolution), isolated contributions, ugc_fp)
```

---

## 8. Multi-Backend Export Rules

Emits content-addressed references + rebinding maps + compatibility verdicts; the content lowering is the per-layer export (V1–V12); transport of CAS content delegated. **Cross-project backend compatibility:** porting A (Unity+Unreal) → B (Unity-only) checks backend intersection (A-only-backend features degrade per the owning layer's rules), format compatibility (target import gates), runtime parity (shared backend carries V11 parity; else re-verified in B). Identity sidecar: `port_kind: clamp_enum(identity_port|rebinding_port|local_fallback)`, `ctx_map` (for rebinding), `backend_set` + per-feature degrade record.
```
backendinterop_hash = H("v13.backendinterop", target backend sets, per-feature degrade record, ugc_fp)
```

---

## 9. Determinism, CAS, and Skip-Work

**9.1** `interop_hash` folds the sub-hashes. **9.2** The cross-project wrapper *spanning* multiple `build_hash`es, anchored by the global CAS that has existed since V1 — content-addressing each project used internally for determinism *is* the addressing that makes content shared across projects. An identity-ported artifact has **one hash in the global CAS, referenced by N projects** (maximal dedup). **9.3 Partial recompilation:** identity-port into a new project → **no recompile** (add a reference to the existing `content_hash`); rebinding-port → only the rebound artifact (`H(source, ctx_map)`; closure reused); compat rule edit → only ports that used it; library version bump → only projects whose constraint resolves differently (shared closure reused); namespace/priority edit → project/interopmerge hashes (content unchanged); source project edits content → its hash changes → dependent ports re-resolve (or pin the old hash). **9.4 Interop determinism contract:** project-independent content addressing (I-CAU); pure `port` (identity ⇒ same hash; rebind ⇒ `H(source, ctx_map)`); closure portability (I-CLOSURE); `clamp_enum` compatibility (unknown ⇒ ignore/clamp/local); labels namespaced, identity = hash; merge priority + `(project_hash, content_hash)` tie-break, floor = isolate-to-local; port restricted to target backend set; reversibility. **9.5 Skip-work:** shared content across an entire portfolio computed/stored **once** (a rig used by 12 titles = one CAS entry, 12 references); identity-ports are pure CAS hits (zero work); the studio's whole catalog deduplicates against one global store.

---

## 10. Validation & Degrade-Never-Fail Model

**10.1** **Theorem (Total Interop).** Any set of V1–V12-passing projects can interoperate through a valid V13 layer in which content moves losslessly where self-contained (I-CAU), safely-rebound where context-bound (I-PORT), and **every importing project remains valid, self-contained, deterministic even if all interop is stripped** (I-LOCAL). No content-induced hard-fail **except** the inherited I-CAP rejection (a ported UGC library requesting sealed capability); everything else degrades to local. Lossless where possible, safe where rebound, self-sufficient always.

**10.2 Validation:** asset graphs (acyclic Merkle DAG; closure-complete — I-CLOSURE; version constraints resolvable-or-degrade); compatibility (clamp_enum class/schema; fwd/back-compat; every source class → target class or local fallback); library packaging (well-formed; closure-complete; deps resolvable; compat range; UGC libraries enforce V8 I-CAP); portability (role-slots all bind or degrade; ported sim inherits V5 bounds; re-verified by target V11); project identity (well-formed `project_hash`; labels namespaced; identity → content hash; collisions by priority); merge & backend (deterministic; isolate-to-local; backend intersection checked; unsupported features degrade per owning layer).

**10.3** Diagnostics: `reject(caps_breach)` (UGC libraries) is the sole content-level hard stop; everything else → local fallback.

**10.4 Degradation ladders:** Asset-graph (resolve from a library / global CAS → **local fallback stub**; unsatisfiable version → nearest → local). Compatibility (nearest-compatible rebind → **local equivalent**). Library (drop optional dep; migrate/downgrade/ignore; tighter sandbox/isolate unverified UGC; sealed cap → reject). Portability (template ladder → stub / zero influence; failed target V11 → isolate that port). Merge (priority → canonical tie-break → **isolate-to-local**). Backend (degrade per the owning layer's rules → metadata/nearest-safe). **Universal floor (I-LOCAL):** any unreconcilable interop → a safe local-project fallback; the importing project is always a valid, self-contained, deterministic build on its own.

**10.5** Every artifact valid for every target backend, deterministic (porting is pure rebinding; identity-ports preserve hashes — I-CAU/I-PORT), closure-complete (I-CLOSURE), degrades to self-sufficient local fallbacks (I-LOCAL). "V1–V12 deterministic per project" ⇒ a deterministic cross-project interoperability layer where content moves between titles losslessly by shared content-hash, context-bound content rebinds safely or degrades to local stubs, a studio's entire catalog deduplicates against one global CAS, and every project remains a valid self-contained deterministic build even with all interop stripped. **The content-addressing that guaranteed determinism since V1 is the mechanism that makes cross-game interop nearly free.**

---

## Appendix A — V13 Identity Chain
```
            global CAS (project-independent content hashes — I-CAU, since V1)
Project A: V1..V12 → build_hash_A     Project B: V1..V12 → build_hash_B   ...
                       \              /
                        ugc_fp (source+target project_hash, refs, ctx_map)
                          ├ project_hash │ assetgraph_hash │ compat_hash │ library_hash │ port_hash │ interopmerge_hash │ backendinterop_hash
                          └ interop_hash
```

## Appendix C — Mechanisms
1. Content-Address Universality (I-CAU) — the hash is a universal namespace; CAS is global. 2. Port-as-Pure-Rebinding (I-PORT). 3. Dependency-Closure Portability (I-CLOSURE). 4. Degrade-to-Local (I-LOCAL). 5. Clamp-enum compatibility. 6. Role abstraction for missions/sims. 7. Labels namespaced, identity is the hash. 8. Portfolio-wide dedup (one store for the whole catalog).

**End of V13 Specification — Cross-Game Interop & Multi-Project Asset Graphs.**
