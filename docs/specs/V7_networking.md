# V7 SPECIFICATION — Networking, Replication, and Authority

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V7 (Networking)
**Status:** Normative specification
**Depends on:** V1–V6, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V6)

**0.1–0.5** As prior layers. **V7 emits a deterministic networking specification (a replication contract); it does not run a server or transmit packets.**

**0.6 The Networking Determinism Contract.** **Spec determinism (MUST):** emitted replication contract is a pure, fully-hashed function of inputs. **Lockstep equivalence (CONTRACTED, the payoff):** because V5 guarantees `worldstate_hash@N = f(world_seed, tick_schedule, input trace)`, two machines that agree on seed + ordered input trace compute the identical world — no state need be sent, only *inputs*. V7's job: make the input trace agree (ordering + authority), and bridge gaps (snapshot replication + rollback). Wire transport, sockets, NAT, encryption, bandwidth, clock sync delegated; the logical model (tick alignment, channel semantics, authority, reconciliation) is pinned/hashed.

**0.7 The Two-Truths Principle (I-AUTH — defining rule).** Every replicated datum has exactly one **authority** and zero-or-more **replicas**. A replica MUST NOT silently become truth; conflict resolves only via reconciliation (§5), always converging on the authority. Ownership is a compile-time, hashed property of each field.

**0.8 The Determinism-Preservation Invariant (I-NETDET).** Network arrival order/latency/loss MUST NOT feed the sim except through the **single funnel** — the per-tick **input command queue** (§4.3), deterministically ordered before the V5 tick-barrier consumes it. Late/lost inputs handled by rollback/prediction, never out-of-band.

**0.9 The Offline-Equivalence Invariant (I-OFFLINE).** Stripping V7 yields a valid single-player/offline build whose V1–V6 `build_hash` is byte-identical to the networked build's. Single-player = the zero-peer case.

---

## 1. V7 Architectural Overview

### 1.1 Purpose
Make the world safely shared across machines without breaking determinism: what's replicated vs recomputed, who owns each truth, how time aligns, how divergence reconciles.

| System | Produces | Identity |
|---|---|---|
| Replication Model | what replicates, on which channel, with which reliability | `replication_hash` |
| Authority Model | server-auth/peer-auth/hybrid ownership map | `authority_hash` |
| State Channels | gameplay/simulation/cosmetic | `channel_hash` |
| Time & Tick Alignment | net tick aligned to V5 tick-barrier | `nettime_hash` |
| Reconciliation | conflict resolution + convergence | `reconcile_hash` |
| Rollback / Prediction | prediction envelopes + rollback windows | `rollback_hash` |

Root: `net_hash`.

### 1.2 Consumption of V1–V6 outputs
**Network Resolution Table (NRT):** V3 interface descriptors (replication candidates), partition cells (interest culling), mission state; V4 state slots, input-driven systems, event bus; V5 world_seed/tick schedule/snapshot model/RNG policy; V6 cosmetic state (READ-ONLY, never authoritative); V1 asset hashes (spawn by hash, not transfer). **Content never transmitted** — both machines share the same CAS (identical V1–V6 `build_hash`). `net_fp := {entity_refs, scene, mission, gameplay_hash, sim_hash, asset_refs}`.

### 1.3 Replicated vs derived (the central economy)
```
classify(datum): deterministic fn of (seed, schedule, agreed inputs) → DERIVED (recompute);
                 originates from a machine's local input/decision → REPLICATED (send from authority);
                 presentation-only → COSMETIC (best-effort, non-authoritative)
```
Sim: all derived; only exogenous inputs replicated (lockstep). Gameplay: player input commands; authoritative spawns/damage (server-auth). V6: cosmetic-only. The split is a hashed per-slot property; "DERIVED" is the bandwidth payoff.

### 1.4 Phase ordering
```
P1 Time & Tick Alignment (nettime_hash) → P2 State Channels (channel_hash) → P3 Authority (authority_hash)
  → P4 Replication (replication_hash) → P5 Reconciliation (reconcile_hash) → P6 Rollback/Prediction (rollback_hash)
net_hash = H(nettime, channel, authority, replication, reconcile, rollback, net_fp, networking_determinism_contract, versions)
```
**I-NET-LOCAL**: one field's policy change touches only its path; never a V1–V6 hash (I-OFFLINE).

---

## 2. Time & Tick Alignment (P1)

```
net_time { net_tick_rate: quantized Hz; alignment: net_tick ≡ V5 sim tick (1:1 or fixed integer ratio);
           input_delay: quantized ticks (lockstep buffer); epoch: session start tick + world_seed (hashed);
           clock_policy: clamp_enum(authoritative_server_clock | shared_epoch_lockstep | half_rtt_estimate) }
```
Integer alignment (no fractional time). `input_delay` buffers commands so all machines consume the same ordered set at the barrier. Session epoch (`world_seed` + start tick + full `build_hash`) exchanged once; mismatch = hard "incompatible build" rejection.
```
nettime_hash = H("v7.nettime", net_tick_rate, alignment ratio, input_delay, clock_policy, net_fp)
```

---

## 3. State Channels (P2)

| Channel | Consistency | Carries | Authority |
|---|---|---|---|
| **gameplay** | strong, ordered, reconciled | input commands, authoritative events (spawn/damage/score), mission progress | full (I-AUTH) |
| **simulation** | lockstep-derived (inputs only) or periodic snapshot | V5 exogenous inputs; periodic `worldstate` snapshot (late-join/resync) | full |
| **cosmetic** | best-effort, eventually-consistent | V6 presentation cues, non-gameplay VFX/SFX | **none** (never reconciled) |

A field is on exactly one channel; misassignment → DEGRADE to the stronger channel. Cosmetic firewalled by V6 read-only (divergence is correct).

**Replication micro-types:** `rep.reliable_ordered/reliable_unordered/unreliable/snapshot/event/delta`.
```
channel_hash = H("v7.channel", sorted(channel defs), field→channel assignment table, net_fp, degrade_decisions)
```

---

## 4. Authority Model (P3)

### 4.1 Models
server-auth / peer-auth / hybrid — all the same mechanism: an **authority map** `field/entity → owner role`; the model name is a preset filling the map; the map is hashed.

### 4.2 Input command queue (the I-NETDET funnel)
```
input_command { origin: authority role; net_tick: scheduled (issue+input_delay); command: a V4/V5 INPUT (never a state value); order_key: (net_tick, origin_id, sequence) }
```
All network input enters the sim **only here**: each tick, the queue is sorted by `order_key` (total order) and handed to the V5 tick-barrier identically on every machine. Commands carry inputs, not results (preserves the DERIVED economy).

### 4.3 Authority → reconciliation handoff
Per-field reconciliation policy: server-auth → "authority overwrites"; peer-auth lockstep → "must agree (else desync)"; cosmetic → "no reconciliation".
```
authority_hash = H("v7.authority", model_preset, authority_map (sorted), input_command schema, per-field reconciliation policy refs, net_fp, degrade_decisions)
```

---

## 5. Replication Model (P4)

```
replication_record { source: {hash, slot|channel, layer}; disposition: clamp_enum(derived|replicated|cosmetic);
                     channel: ref; authority: owner role ref; reliability: ref; quantization: net_quant_spec; interest: policy; cadence: quantized ticks }
```
DERIVED records carry no transport (document the no-send decision). Wire quantization (distinct from sim quantization) applied before hashing the replicated value → no drift. **Interest management:** scoped to V3 partition cells (owning + adjacent, streaming-aware), quantized buckets (no hysteresis); out-of-interest → derived (lockstep) or snapshotted on re-entry.
```
replication_record_hash = H("v7.reprec", source, disposition, channel ref, authority ref, reliability ref, net_quant_spec, interest_policy, cadence)
replication_hash = H("v7.replication", sorted(replication_record_hash…), derived/replicated/cosmetic partition, net_fp, degrade_decisions)
```

---

## 6. Reconciliation & Conflict Resolution (P5)

### 6.2 Micro-types
`rec.authority_overwrite` (server-auth), `rec.lockstep_assert` (peer-auth; mismatch → desync event), `rec.last_writer_authority` (hybrid single-owner), `rec.merge_commutative` (CRDT-style associative+idempotent), `rec.snapshot_resync` (drift/late-join), `rec.cosmetic_ignore`.

### 6.3 Convergence
Each non-cosmetic field MUST be convergent + deterministic: given authority's value at acked tick N, every replica resolves to it, canonical order, bounded ticks. Non-convergent rule rejected → DEGRADE to `rec.authority_overwrite`.

### 6.4 Desync detection
Periodic `worldstate_hash@N` checksum exchange (cheap — V5 already computes it). Mismatch = provable desync, attributed to a tick, handled by `rec.snapshot_resync` or termination — never silent divergence. Doubles as a cheat/bug tripwire.
```
reconcile_hash = H("v7.reconcile", sorted(field → reconciliation micro-type), convergence proofs / desync-checksum policy, canonical resolution order, net_fp, degrade_decisions)
```

---

## 7. Rollback / Prediction (P6)

```
prediction_envelope { predicted_fields: set<field>; predict_horizon: quantized ticks; rollback_window: quantized ticks;
                      resim_scope: clamp_enum(full_world|interest_cells|predicted_entities_only); determinism_basis: V5 snapshot @ (N - rollback_window) }
```
Replica predicts locally-owned/input-driven fields forward using the *same deterministic core* (no special prediction code). On authoritative input/state for a past tick → roll back to the V5 snapshot, re-simulate forward deterministically with corrected inputs (exact, no approximation). **Cheap here** because V5 is snapshot-replayable: rollback = "load snapshot @ N, replay inputs N→now." Horizons/windows clamped (bounded resim); beyond window → `rec.snapshot_resync`.
```
rollback_hash = H("v7.rollback", prediction_envelope, snapshot-anchor policy, bounded-resim limits, net_fp, degrade_decisions)
```

---

## 8. Multi-Backend Export Rules (Unity, Unreal, Custom/Headless)

Emits declarative replication contracts + refs to standard transports, never bespoke netcode. Blender/USD = contract metadata only (no runtime networking).

| Concept | Unity (Netcode) | Unreal (Replication/Iris) | Custom/Headless | Blender/USD |
|---|---|---|---|---|
| Replicated field | NetworkVariable / Ghost field | Replicated UPROPERTY / Iris state | server struct + delta codec | metadata |
| Authority | OwnerClientId | Role/RemoteRole/HasAuthority | server owns all | metadata |
| Input command | NetworkInput | input replication / RPC | command queue ingest | metadata |
| Tick alignment | NetworkTickSystem | Net frequency / fixed tick | fixed-tick loop (V5 cadence) | metadata |
| Rollback | client prediction + reconcile | predicted movement / Iris | deterministic resim from snapshot | metadata |
| Interest | distance/observer filter | net relevancy/dormancy | cell-based (V3 partition) | metadata |
| Desync checksum | state hash exchange | crc/state hash | `worldstate_hash@N` exchange | metadata |

**Custom/Headless** is first-class: it runs the same V1–V5 core with no V6, making it the canonical authority with minimal code. Identity sidecar: `session = { build_hash (MUST match peers), world_seed, epoch tick }`; net-ids (ghost/NetGUID) derive from content hashes.

---

## 9. Determinism, CAS, and Skip-Work

**9.1** `net_hash` folds six sub-hashes. **9.2** Detachable wrapper above the stack (I-OFFLINE); **Session identity** = `H(build_hash(V1–V6), net_hash, world_seed)` — peers MUST match `build_hash(V1–V6)`. **9.3 Partial recompilation:** field channel/authority→its record→replication/authority_hash; tick-rate→nettime_hash; reconciliation rule→reconcile_hash (+rollback if shared); upstream slot added→only referencing records; server↔peer switch→authority_hash + dependent records (core identical). **9.4 Determinism contract:** integer tick alignment to V5; input delay; command total order `(net_tick, origin_id, sequence)` (I-NETDET); channel semantics; wire quantization before value hashing; interest buckets; clamped rollback bounds; desync checksum cadence; seed sharing once. **9.5 Skip-work:** standard; never invalidates a V1–V6 cache entry (I-OFFLINE).

---

## 10. Validation & Degrade-Never-Fail Model

**10.1** **Theorem (Total Networking).** Any V6-passing world gets a valid V7 contract AND is always a valid single-player/offline build (I-OFFLINE). No content-induced hard-fail; hard errors: compiler bugs **plus** a datum with zero/≥2 authorities (I-AUTH), a non-convergent reconciliation rule, or a network input bypassing the command queue (I-NETDET). Defining guarantee: **single-player-safe / offline-safe.**

**10.2 Validation:** replication (every datum classified, on one channel, net-quantized, interest scoped); authority (**exactly one** per datum → else DEGRADE to server/host; commands carry inputs only); channels (consistency-correct; cosmetic firewalled); time (integer alignment; epoch/seed defined; `build_hash` match at connect); reconciliation (convergent+deterministic; checksum cadence); rollback (clamped bounds; predicted fields locally-owned; valid snapshot anchors); integration (network input reaches sim only via the ordered command queue; no out-of-band sim write; offline == networked `build_hash` for V1–V6).

**10.3** Diagnostics deterministic + hashed in.

**10.4 Degradation ladders:** Authority (ambiguous→server/host; unreliable peer-auth→server-auth; worst→local single authority = single-player). Replication (expensive→DERIVED; unresolvable→drop; worst→replicate only the command queue = pure lockstep). Channel (misassigned→promote; unreliable unavailable→reliable; worst→single reliable-ordered channel). Reconciliation (non-convergent→authority_overwrite; frequent desync→snapshot_resync; worst→periodic full snapshot). Rollback (costly→narrow scope; beyond window→snapshot_resync; worst→disable prediction = confirmed-state only). Time (unreliable clock→shared_epoch_lockstep; build mismatch→reject session; worst→offline).

**10.5** Every contract valid for every backend, never perturbs the V1–V6 core (I-NETDET), fully detachable (I-OFFLINE). "Passes V1–V6" ⇒ network-safe, authority-clean, determinism-preserving multiplayer on Unity/Unreal/custom-headless — and the identical content is always a valid single-player build.

---

## Appendix A — V7 Identity Chain
```
…gameplay (V4) │ sim (V5) │ presentation (V6) → net_fp → nettime │ channel │ authority │ replication │ reconcile │ rollback → net_hash (detachable via I-OFFLINE)
```

## Appendix C — Determinism mechanisms
1. Lockstep equivalence (replicate inputs, derive the rest). 2. Two-Truths (I-AUTH). 3. Determinism-preservation (I-NETDET command-queue funnel). 4. Offline-equivalence (I-OFFLINE). 5. Integer tick alignment. 6. Wire quantization before value hashing. 7. Cheap rollback = VM replay from snapshot. 8. Desync = mismatched `worldstate_hash@N`.

**End of V7 Specification — Networking, Replication, and Authority.**
