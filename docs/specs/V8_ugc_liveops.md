# V8 SPECIFICATION — UGC, Mod Ecosystem, and Live-Ops Pipelines

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V8 (External Authoring & Evolution)
**Status:** Normative specification
**Depends on:** V1–V7, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V7)

**0.1–0.5** As prior layers. **V8 emits deterministic mod/live-ops specifications and validated entry-point bindings; it does not execute untrusted code as the compiler.** Every field exposed to UGC MUST use `clamp_enum`/`clamp_range` (the primary safety mechanism for untrusted input).

**0.6 The Capability-Boundary Principle (I-CAP — defining rule).** V8 partitions the micro-type graph into **UGC-safe** (exposed) and **UGC-unsafe** (sealed). UGC can only instantiate, parameterize, and compose UGC-safe micro-types through clamp-bounded fields; it can never define new micro-type *kinds*, alter authority models, touch simulation kernels, or supply executable solver code. **A mod is always data over a fixed, trusted vocabulary — never new trusted code.**

**0.7 The Determinism-Inheritance Invariant (I-UGCDET).** Admitted content inherits, not weakens, the determinism of the layer it plugs into (UGC entities obey V3/V4; UGC sim-influence obeys V5 tick-barrier + clamps + hash-seeded RNG; UGC replication obeys V7 authority/command-queue). No new nondeterminism vector — a mod cannot read wall-clock, spawn threads, do unbounded work, or feed IO into the sim outside sanctioned funnels.

**0.8 The Reversibility & Snapshot-Addressability Invariant (I-REV).** Every mod/live-ops change is composable, reversible, snapshot-addressable. `apply(base_build_hash, ordered mod set) → modded_build_hash`; removing returns to `base_build_hash` (byte-identical). The active config is a content-addressed value (`config_hash`).

**0.9 Trust tiers.** `first_party` (full trust), `verified` (signed/curated; standard sandbox), `untrusted` (strict sandbox + tighter clamps). Tier never grants new capabilities (I-CAP holds for all); it tunes clamp tightness, budgets, available UGC-safe types.

---

## 1. V8 Architectural Overview

### 1.1 Purpose
The layer through which the game keeps growing after ship — UGC, mods, live-ops — safely, deterministically (I-UGCDET), reversibly (I-REV).

| System | Produces | Identity |
|---|---|---|
| UGC Micro-Types | exposed authoring vocabulary + authored content | `ugc_hash` |
| Mod Packaging | CAS bundles + manifests + layering | `mod_hash` |
| Sandbox & Validation | structural/behavioral/sim/net safety verdict | `sandbox_hash` |
| Live-Ops Pipeline | events/seasons/rotations/schedules | `liveops_schedule_hash` |
| Versioning & Migration | spec-version compat + schema transforms | `migration_hash` |
| Interoperability | merge/conflict resolution across mods | `merge_hash` |
| UGC→Sim Bridge | bounded influence on V5 | `influence_hash` |
| UGC→Mission Bridge | UGC-driven dynamic missions | `ugc_mission_hash` |

Root: `liveops_hash`; active deployment: `config_hash`.

### 1.2 Consumption of V1–V7 outputs
**Authoring Resolution Table (ART-8):** the registry partitioned UGC-safe/unsafe; V3 entity/scene/mission targets + exposed mission TEMPLATES; V4 state/behaviors/rulesets; V5 sim graph + bounded influence hooks + snapshot; V6 cosmetic surface; V7 replication contract + authority map; V1 CAS (UGC adds leaves, never overwrites). UGC sees only the UGC-safe partition (unsafe types are *absent* from the vocabulary). Exposed entry points are explicit hashed contracts. `ugc_fp := {registry_partition_hash, entity_refs?, scene?, mission?, gameplay_hash?, sim_hash?, net_hash?, asset_refs?}`.

### 1.3 Phase ordering
```
P1 UGC Micro-Type Layer (ugc_hash) → P2 Mod Packaging (mod_hash) → P3 Sandbox & Validation (sandbox_hash) [HARD GATE]
  → P4 Interoperability & Merge (merge_hash) → P5 Versioning & Migration (migration_hash)
  → P6 UGC→Sim Bridge (influence_hash) / P6' UGC→Mission Bridge (ugc_mission_hash) → P7 Live-Ops Pipeline (liveops_hash)
config_hash = apply(base build_hash(V1–V7), ordered+merged+sandboxed mod/liveops set)
```
Sandbox (P3) is a hard gate: nothing reaches the world graph unsandboxed; a failing unit DEGRADES to a safe no-op. I-CAP/I-UGCDET/I-REV hold throughout.

### 1.4 Identity
```
liveops_hash = H("v8.liveops", ugc_hash, mod_hash, sandbox_hash, merge_hash, migration_hash, influence_hash, ugc_mission_hash, liveops_schedule_hash, ugc_fp, authoring_determinism_contract_hash, versions)
config_hash = H("v8.config", base_build_hash(V1–V7), ordered(applied mod_hash…), liveops_hash, active_season_ref)
```
**I-V8-LOCAL**; **I-REV** (empty mod/liveops set ⇒ `config_hash == base_build_hash(V1–V7)`).

---

## 2. UGC Micro-Type Inventory (P1)

### 2.1 UGC-safe / unsafe partition (I-CAP)
| UGC-SAFE (parameterize & compose) | UGC-UNSAFE (sealed) |
|---|---|
| entities (from existing behaviors/assets/params), scenes (placement over existing cells), missions (instantiate exposed templates), behaviors (compose existing micro-types), cosmetics (V6 cosmetic channel), rulesets (clamped modifiers) | simulation kernels (V5 engine/tick-barrier), authority models (V7 map/queue/reconciliation), physics/IK/deform solvers, the determinism contract (seeds/tick/quantization/clamps), net channel semantics, CAS/hash primitives, the registry partition, **new micro-type kinds** |

Everything defining determinism/authority/the trusted vocabulary is sealed; everything that is data over it is exposed.

### 2.2 Micro-types
`ugc.entity/scene_edit/mission/behavior_set/ruleset/cosmetic/sim_influence/asset`.

### 2.3 Clamp-enum model
Every exposed field is `clamp_enum(member set)` / `clamp_range(min,max,quant)` / `ref(UGC-safe target set)`. No free-form numerics, no string-as-code, no raw references. Tighter clamps at lower trust tiers.

### 2.4 UGC → V3/V4/V5 integration
Additive by default (deletion = reversible override-layer); interface-only coupling (V5 only via bounded influence hooks §8); determinism inheritance (a `ugc.entity` is compiled by the same V3/V4 pipeline; a `ugc.sim_influence` is subject to V5 clamps/tick-barrier).

### 2.5 Hashing
```
ugc_unit_hash = H("v8.ugc", ugc_micro_type, version, ugc_fp, canonical(clamped params), sorted(safe target refs), trust_tier, degrade_decisions)
ugc_hash = H("v8.ugcset", sorted(ugc_unit_hash…), registry_partition_hash)
```

---

## 3. Mod Packaging & Distribution (P2)

### 3.1 CAS packaging
A mod = content-addressed bundle (CAS leaves + manifest), `mod_hash` = Merkle root over sorted contents; globally deduplicated.

### 3.2 Manifest
```
mod_manifest { mod_id; mod_version (semver, hashed); contents: sorted set<ugc_unit_hash|asset_hash>;
               dependencies: sorted set<{mod_id, version_range, optional}>; compatibility: {min/max_spec_version, backend set};
               trust_tier: clamp_enum(first_party|verified|untrusted); layering: sorted set<layer_rule>; signature?; declared_caps: sorted set<UGC-safe capability> }
mod_hash = H("v8.mod", mod_manifest, sorted(contents))
```
`declared_caps ⊆ UGC-safe` — a manifest declaring sealed capability is **rejected** (I-CAP breach, the one thing never tolerated).

### 3.3 Layering (non-destructive, reversible)
`layer.add` (new leaves), `layer.extend` (add to collection), `layer.override` (shadow base; base NOT deleted), `layer.patch` (clamped param delta). All non-destructive; base never mutated, only shadowed/extended → composable + reversible (I-REV).

### 3.4 Distribution channels
`local` (untrusted), `workshop` (verified-if-curated/untrusted; `mod_hash` is the id), `remote_registry` (verified, signed). Content-addressed: resolve `mod_id@version → mod_hash`, fetch by hash, verify by recompute. Transport delegated.

---

## 4. Sandbox & Validation Model (P3)

### 4.1 The gate
Every unit passes a layered safety verdict before affecting the world; else DEGRADES to a safe no-op.

### 4.2 Tiers
Structural (clamp/coerce; **caps breach = reject**); Behavioral (no infinite loops/unbounded work; bounded budget; **termination provable** — no Turing-complete UGC); Simulation safety (V5 tick-barrier, clamped/bounded, hash-seeded, no runaway → else zero/clamped influence); Networking safety (respects V7 authority; no out-of-band sim writes; cosmetic stays cosmetic → else local/cosmetic-only); Backend safety (valid on all backends → else nearest safe form).

### 4.3 Behavioral boundedness (decidable)
No executable UGC ⇒ behavioral safety reduces to graph+parameter analysis: composition graphs acyclic-or-damped; every quantity clamp-bounded; every influence rate-limited. **Termination/boundedness decidable** — no halting problem (no general computation).

### 4.4 Isolation
Effects scoped to the mod's layer; a misbehaving mod is isolated (layer popped) deterministically → prior `config_hash` (I-REV); also the conflict-resolution fallback (§7.3).

### 4.5 Hashing
```
sandbox_verdict = { structural, behavioral, sim_safety, net_safety, backend_safety : pass|degrade(reason) }
sandbox_hash = H("v8.sandbox", sorted(per-unit verdict + applied degrade), validation_policy, ugc_fp)
```
Verdict deterministic + hashed into the unit (a sandboxed-degraded mod is a distinct, reproducible artifact).

---

## 5. Live-Ops Content Pipeline (P7)

### 5.2 Micro-types
`lo.season/event/rotation/modifier`.

### 5.3 Schedule
```
schedule { kind: clamp_enum(time_based|trigger_based|worldstate_based); time: calendar windows (V5 calendar ticks);
           trigger: event/condition refs; worldstate: predicate over a V5 snapshot (quantized threshold);
           activation: deterministic fn of (calendar tick, world snapshot) }
```
Activation is a pure fn of `(calendar tick, worldstate_hash@N)` → reproducible active set, part of `config_hash`. No wall-clock. A live-ops change = a scheduled mod-layer apply/pop → reversible (I-REV): "what was live on date D" = `config_hash` at that calendar tick.

### 5.4–5.5 Integration
`lo.event/season` may activate `ugc.sim_influence` hooks (§8) and seed dynamic missions (§9) + apply `ugc.ruleset` modifiers (clamped); all additive/override layers, reversible at event end.

```
liveops_schedule_hash = H("v8.loschedule", sorted(live-event defs + schedules), calendar alignment)
```

---

## 6. Versioning & Migration Rules (P5)

Each layer V1–V8 carries `spec_version`; a mod declares `[min,max]`. Forward-compat: unknown field → ignore; out-of-range/renamed enum → clamp; newer-than-engine → downgrade (drop newer-only → nearest/no-op); older → migrate forward. Guiding rule: **unknown ⇒ ignore/clamp, never crash.**
```
migration { from_spec, to_spec, transforms: ordered list<schema_transform> }
schema_transform: clamp_enum(rename_field|default_new_field|split|merge|drop|remap_enum)
migration_hash = H("v8.migration", sorted(migration defs), from/to spec versions)
```
Pure deterministic schema transforms; missing migration → default new / drop unknown (never fail) — unifies V4 `save.migration` / V5 `world.migration`.

---

## 7. Mod Interoperability & Conflict Resolution (P4)

Merge rules: `merge.additive` (commutative union), `merge.override` (highest-priority layer wins), `merge.patch` (composed in priority order; numeric = declared clamped commutative op or sequenced).
```
conflict := two layers target the same artifact/field with incompatible rules
detection: schema | identity | behavioral (overlapping non-commutative write-sets)
resolution ladder: 1 priority (explicit hashed load order) → 2 fallback (canonical by mod_hash) → 3 isolation (drop lower-priority; never corrupt)
```
**I-MERGE-DET**: identical mod set + priority ⇒ identical `merge_hash`. Floor = isolation (base always intact, I-REV).
```
merge_hash = H("v8.merge", ordered(layer stack w/ priority), sorted(detected conflicts + resolution), isolated layers, ugc_fp)
```

---

## 8. UGC → Simulation Bridge — V8 → V5 (P6)

UGC influences V5 only via **clamped pre-declared influence hooks** the sim reads on the tick-barrier:
| Domain | Exposed hook (clamp_range) | Sealed |
|---|---|---|
| factions | aggression/expansion bias, relation nudge, spawn-budget delta | faction kernel, influence solver |
| ecology | birth/death/migration multipliers, capacity delta | food-web stability engine |
| economy | production/consumption deltas, price-band nudge | market-clearing solver |
| weather | transition-probability bias, hazard cap | field propagation engine |
| emergence | event-threshold nudge, event-weight bias | trigger kernel |

Bounded rules: **clamped** + **tick-barriered** (1-tick-delayed) + **rate-limited & damped** + **hash-seeded** (`H(world_seed, ugc_unit_hash, tick)`, stream-isolated). Sandbox proves statically; violation → zero/clamped. UGC can shape behavior, never destabilize determinism/boundedness.
```
influence_hash = H("v8.influence", sorted(influence bindings: hook ref, clamped delta, rate limit, seed policy, tick-delay), bounded-feedback proofs, ugc_fp, degrade_decisions)
```

---

## 9. UGC → Mission Bridge — V8 → V3 (P6')

UGC instantiates exposed templates `tmpl.reach/rescue/defend/escort/trade/sabotage` with clamped params + `ref`-validated targets, and may register dynamic generation hooks routed through the same **acyclic V5→V3 snapshot-keyed bridge** (V5 §9.2). Compiled by the trusted V3 mission compiler → inherits validation/degrade (unbindable target → template ladder to a stub; never a deadlock). Bounded, snapshot-keyed → no generation feedback loop.
```
ugc_mission_seed_hash = H("v8.ugcseed", template ref, clamped params, sorted(target refs), worldstate snapshot_hash@N, provenance: ugc_unit_hash)
ugc_mission_hash = H("v8.ugcmission", sorted(ugc_mission_seed_hash…), exposed-template set ref, generation-hook bindings, ugc_fp)
```

---

## 10. Multi-Backend Export Rules

Emits content-addressed bundles + declarative schedules + validated binding contracts, never executable mod code (I-CAP). Mod bundle → Addressables catalog/AssetBundle (Unity) / Pak+asset registry (Unreal) / linked library (Blender) / sublayer-payload (USD), by `mod_hash`. UGC content per V3 export. Live-ops schedule → remote-config + catalog versioning (Unity/Unreal real runtime; Blender/USD metadata). Identity sidecar: `config = {base_build_hash (MUST match), config_hash, active_season_ref}`, `trust_tier`, `sandbox = sandbox_hash`. Peers verify the same `config_hash` before sharing a V7 session.

---

## 11. Determinism, CAS, and Skip-Work

**11.1** `liveops_hash` folds eight; `config_hash` composes base + ordered mods + liveops. **11.2** Outermost reversible wrapper (I-REV); session identity (V7) = `H(config_hash, world_seed)` — peers must share `config_hash`. **11.3 Partial recompilation:** mod field→its unit→mod_hash→re-sandbox→liveops/config; mod add/remove→config (base unchanged); schedule edit→schedule_hash; migration edit→only crossing mods; priority change→merge_hash; sandbox policy→re-admit. **11.4 Authoring determinism contract:** UGC clamps per tier, resource budgets, influence bounds, tick-barrier compliance, merge priority + `mod_hash` tie-break, declarative migrations, seed isolation, integer calendar alignment. **11.5 Skip-work:** shared assets stored once; unchanged mod reused; "game as of date D/patch P" = a cached `config_hash`; never invalidates a V1–V7 cache entry (I-REV).

---

## 12. Validation & Degrade-Never-Fail Model

**12.1** **Theorem (Total Authoring).** Any V7-passing base can admit any UGC/mod/live-ops set → valid, deterministic, backend-safe, reversible deployment; the base always remains intact/recoverable (I-REV). No content-induced hard-fail **except** a mod requesting sealed capability (I-CAP breach = rejected, never degraded). Capability-safe, always-reversible, determinism-preserving.

**12.2 Validation:** UGC types (clamp fields; only UGC-safe kinds; acyclic-or-damped composition → termination decidable; targets resolve/no-op); manifests (`declared_caps ⊆ UGC-safe` else reject; deps resolvable; compat range; signature for verified); live-ops (pure activation fn of integer calendar tick + snapshot; sandboxed content); migrations (deterministic; unknown→ignore/clamp; missing→default); integration (only published interfaces/templates/bounded hooks; sim influence clamped+tick-barriered+rate-limited; UGC missions pass V3 validator; no kernel write).

**12.3** Diagnostics: `reject(caps_breach)` is the sole content-level hard stop.

**12.4 Degradation ladders:** UGC unit (clamp → placeholder/no-op → inert; caps breach → reject). Mod (drop optional deps; migrate/downgrade/ignore; demote/isolate unverified; worst → isolated). Sandbox (clamp; no-op; zero/clamped influence; cosmetic/local-only; nearest safe form). Live-ops (event stays off; runs without failed content; no event). Migration (default/drop; identity transform; load at base spec). Merge (priority → `mod_hash` tie-break → isolate). Influence (clamp; damp; zero influence).

**12.5** Every artifact backend-valid, capability-bounded (I-CAP), determinism-preserving (I-UGCDET), reversible (I-REV). "Passes V1–V7" ⇒ admit arbitrary UGC/mods/live-ops → valid, deterministic, network-shareable, fully reversible deployment on Unity/Unreal/Blender/USD; untrusted content can never obtain sealed capability, break determinism, or corrupt the base.

---

## Appendix A — V8 Identity Chain
```
…net (V7) → ugc_fp → ugc │ mod │ sandbox │ merge │ migration │ influence │ ugc_mission │ liveops_schedule → liveops_hash → config_hash (base ⊕ mods ⊕ live-ops; reversible)
```

## Appendix C — Mechanisms
1. Capability-Boundary (I-CAP). 2. Clamp-everything surface. 3. Determinism-Inheritance (I-UGCDET). 4. Reversibility (I-REV). 5. Behavioral boundedness decidable (no executable UGC). 6. Bounded sim influence. 7. Deterministic merge (priority + tie-break; floor = isolate). 8. Content-addressed everything → peers verify identical `config_hash`.

**End of V8 Specification — UGC, Mod Ecosystem, and Live-Ops Pipelines.**
