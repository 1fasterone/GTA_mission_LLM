# V11 SPECIFICATION — Automated QA, Fuzzing, and Deterministic Replay

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V11 (Verification & Replay)
**Status:** Normative specification
**Depends on:** V1–V10, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V10)

**0.1–0.5** As prior layers. **V11 emits deterministic test/replay/fuzz/scenario specifications and verdicts; it does not itself run a flaky test rig.**

**0.6 The Verify-Don't-Perturb Principle (I-VERIFY — defining rule).** A QA run *drives* the deterministic core with a controlled exogenous input trace (replay) and *reads* deterministic outputs (snapshots, event traces). It MUST NOT inject state out-of-band, patch the sim, or write any V1–V10 content; the only inputs V11 supplies are inputs through sanctioned funnels (V4 commands, V5 influence, V7 queue) + configuration (seed, schedule). Inherited I-DETACH: stripping V11 leaves V1–V10 byte-identical.

**0.7 The Replay-Equivalence Principle (I-REPLAY-EQ — the core claim).** A *replay* is `(build_hash, world_seed, tick_schedule, input trace)`; replaying it MUST reproduce the identical `worldstate_hash@N` sequence + deterministic event trace — **byte-identical, on every backend and machine**. V11 measures this and fails loudly when it breaks. Replay equivalence is V11's primary assertion primitive and the stack's master regression signal.

**0.8 The Reproducible-Experiment Invariant (I-EXP).** Every QA artifact is content-addressed; its verdict bears no dependence on wall-clock/machine/scheduler/thread timing. Fuzzing is seeded; a discovered failure is captured as a **minimal, replayable seed** that reproduces the bug exactly — never a one-off.

**0.9 The Divergence-Localization Invariant (I-DIVLOC).** A divergence is binary-searchable in time (which tick first diverged via `worldstate_hash@N`) and structure (which sub-hash first diverged) → an exact, hashed, reproducible bug coordinate.

---

## 1. V11 Architectural Overview

### 1.1 Purpose
Prove (or disprove) the determinism every layer asserted — and weaponize it: a replay engine that reproduces any session byte-for-byte, fuzzers that hunt inputs that break determinism/invariants, scenario generators, regression detectors.

| System | Produces | Identity |
|---|---|---|
| QA Micro-Types | test + assertion vocabulary | `qatype_hash` |
| Deterministic Replay Engine | replay schema + execution contract | `replay_hash` |
| Fuzzing Harness | seeded fuzzers + shrinkers | `fuzz_hash` |
| Scenario Generator | parametric test-world generators | `scenario_hash` |
| Regression Detection | snapshot/event/replay divergence detectors | `regress_hash` |
| Multi-Backend Verification | cross-backend replay parity | `backendverify_hash` |
| Automation Pipeline | batch replay, continuous QA, fuzz-farm | `qa_automation_hash` |

Root: `qa_hash`.

### 1.2 Consumption of V1–V10 outputs
**Verification Resolution Table (VRT):** `build_hash` (the SUT), `world_seed`/`tick_schedule`, `worldstate_hash@N` (the snapshot oracle), `event_hash` (secondary oracle), input funnels (V4/V5/V7), `net_hash` (authority + desync checksum), `config_hash`, per-layer sub-hashes (structural oracles), validators. SUT named by `build_hash`+`config_hash`. Drives only via sanctioned funnels (I-VERIFY). **Oracles are the stack's own hashes** (no hand-maintained golden files). `qa_fp := {build_hash(SUT), config_hash, world_seed, tick_schedule, oracle hashes}` — note `qa_fp` CONTAINS `build_hash(V1–V10)` (the SUT is V11's input oracle).

### 1.3 Phase ordering
```
P1 QA Micro-Types (qatype_hash) → P2 Deterministic Replay Engine (replay_hash)
  → P3 Scenario Generator (scenario_hash) / P3' Fuzzing Harness (fuzz_hash)
  → P4 Regression Detection (regress_hash) → P5 Multi-Backend Verification (backendverify_hash) → P6 Automation (qa_automation_hash)
qa_hash = H(qatype, replay, scenario, fuzz, regress, backendverify, qa_automation, qa_fp, verification_determinism_contract, versions)
```
Replay (P2) is the universal currency; scenarios/fuzzers produce replay inputs; regression compares replay outputs; backend verification cross-checks; automation orchestrates. I-VERIFY/I-DETACH/I-EXP/I-DIVLOC hold.

### 1.4 Identity
```
qa_hash = H("v11.qa", qatype_hash, replay_hash, scenario_hash, fuzz_hash, regress_hash, backendverify_hash, qa_automation_hash, qa_fp, verification_determinism_contract_hash, versions)
```
**I-QA-LOCAL**: one test/scenario/fuzzer change touches only its path; never a V1–V10 hash (I-DETACH).

---

## 2. QA Micro-Types (P1)

### 2.2 Test micro-types
`test.unit/integration/scenario/regression/property/parity`.

### 2.3 Assertion micro-types
| Micro-type | Asserts | Oracle |
|---|---|---|
| `assert.state` | `worldstate_hash@N == expected` (or field within tolerance) | V5 snapshot |
| `assert.event` | event trace matches | V10 `event_hash` |
| `assert.timing` | logical-tick budget/ordering (sim ticks, never wall-ms) | V5 tick / V10 logical_timer |
| `assert.simulation` | a V5 invariant (boundedness/conservation/convergence) | V5 sub-hashes |
| `assert.validates` | a layer's validator passes / degrades within policy | per-layer validator |
| `assert.invariant` | a named stack invariant (I-AUTH, I-PROJ, boundedness…) | sub-hashes/verdicts |
| `assert.no_divergence` | a replay reproduces its reference byte-identically | snapshot + event |

Timing assertions are logical (sim ticks), never wall-clock — keeps verdicts reproducible (I-EXP).

### 2.4 Hashing
```
qatype_hash = H("v11.qatype", sorted(test defs), sorted(assertion defs: oracle ref, tolerance, logical-only timing), qa_fp)
```

---

## 3. Deterministic Replay Engine (P2)

```
replay { replay_id; sut: build_hash + config_hash; seed: world_seed; schedule: tick_schedule;
         input_trace: ordered list<exogenous input>; oracle: reference snapshot/event hashes (optional); scope: clamp_enum(full_world|interest_cells|subsystem) }
exogenous input := { net_tick, origin, command }  # a V4/V5/V7 input through a sanctioned funnel
```
Captures **only** `(SUT, seed, schedule, input trace)` — sufficient because everything else is derived. Recording = logging the input trace (already flowing through the funnels); replay = feeding it back (no special record mode — I-VERIFY).
- **→ V4:** feed recorded commands into the V4/V7 queue in deterministic total order; same commands → same results.
- **→ V5:** supply exogenous sim inputs at the barrier; oracle = `worldstate_hash@N` sequence; can start from a mid-run snapshot (fast partial replay).
- **→ V7:** reproduce a multi-machine session; desync checksum is the built-in assertion; can reproduce a reported desync exactly; respects I-AUTH (supplies inputs, not state).
```
replay_hash = H("v11.replay", sut, seed, schedule, input_trace (canonical), scope, oracle refs?, qa_fp)
```

---

## 4. Fuzzing Harness (P3')

| Dimension | Fuzzes | Constraint |
|---|---|---|
| Input | controls, AI decision inputs, mission triggers, interaction sequences | inputs only via V4/V7 funnels |
| Simulation | weather/economy/ecology/faction exogenous inputs + bounded influence | within V5/V8 clamps (explores the bounded space) |
| Network | latency, loss, reorder, authority shifts, join/leave | exercises V7 reconciliation/rollback; MUST converge |

```
fuzz_campaign { strategy: clamp_enum(random_seeded|coverage_guided|property_directed); seed: fuzz_seed; budget: quantized;
                oracles: the assertions/invariants that MUST hold; shrinker: deterministic input-trace minimizer }
```
Seeded exploration `H(fuzz_seed, iteration)` (same campaign every run — I-EXP); coverage-guided by the deterministic event/coverage trace. On failure: deterministically shrink to the smallest reproducer, emit as a `replay` (a permanent regression seed → becomes a `test.regression`). Property oracles = the stack's invariants (no replay diverges, boundedness never violated, reconciliation always converges, no validator hard-fails).
```
fuzz_hash = H("v11.fuzz", sorted(campaign defs), fuzz domain set, qa_fp)
# a fuzz FINDING is a normal replay_hash (the shrunk reproducer), cached as a regression seed
```

---

## 5. Scenario Generator (P3)

Micro-types `scn.combat/traversal/mission/simulation/network/stress`. A scenario is a deterministic generator: given clamped params + a scenario seed → a V3 scene/entity/mission set (via the same V3 compiler — cannot build what V3 couldn't), an initial worldstate, a scripted input trace → a replay. Same scenario spec → same test world every time (I-EXP). Reuses V8/V5 mission-template + influence machinery.
```
scenario_hash = H("v11.scenario", sorted(scenario defs: micro-type, clamped params, scenario_seed, generator ref), produced SUT-config refs, qa_fp)
# a generated scenario instance → a replay_hash
```

---

## 6. Regression Detection (P4)

| Oracle | Compares | Granularity |
|---|---|---|
| snapshot | `worldstate_hash@N` sequences | per-tick → first-diverging tick |
| event-stream | deterministic `event_hash` sequences | per-event → first-diverging event |
| structural | per-layer sub-hashes at the diverging tick | per-subsystem → which system broke |
| replay-divergence | a stored replay re-run vs its oracle | end-to-end + coordinate |

**Divergence localization (I-DIVLOC):** binary-search `worldstate_hash@N` → first diverging tick T → diff sub-hashes at T → first diverging subsystem S → (optional) diff S's child hashes → emit a bug coordinate `{ref vs cand build_hash, tick T, subsystem S, sub-hash diff, minimal replay}`. References are CAS artifacts pinned to a `build_hash`; **updating a reference is explicit + provenance-stamped** (intended balance change ≠ accidental determinism break).
```
regress_hash = H("v11.regress", sorted(detector defs: oracle, granularity, tolerance, reference build_hash pin), localization policy, reference-update provenance policy, qa_fp)
```

---

## 7. Multi-Backend Verification (P5)

```
parity_run { replay: replay_hash; backends: set ⊆ {unity, unreal, blender, usd}; oracle: logical worldstate_hash@N + deterministic event_hash; compare: per-tick, per-subsystem }
```
Compares the **logical, quantized state** (not raw floats); quantization-before-hashing absorbs sub-tolerance float divergence → cross-backend bit-identity of the logical state is achievable; surviving divergence is a real bug. Parity runs **audit the "delegated, inputs-pinned" boundary** (V4 physics solvers, V5 field math, navmesh bakes). Divergence classified deterministically: **bug** (must fix), **acceptable-backend-variance** (blessed per-backend reference within documented bounds), or **degrade-equivalent** (a backend that DEGRADED a feature per its export rules) — each a hashed verdict with provenance.
```
backendverify_hash = H("v11.backendverify", sorted(parity_run defs), divergence classification + per-backend reference pins, qa_fp)
```

---

## 8. Automation Pipeline (P6)

Batch replay (a corpus vs a candidate `build_hash`; CAS skip-work skips unchanged replays); continuous QA (on every source change via the V9 build graph → rebuild affected layers → run affected replays + invariant fuzz checks → pass/divergence report; the verify gate generalized to behavior); fuzz-farm (parallel seeded campaigns; deterministic sharding `H(fuzz_seed, shard)`; findings auto-shrunk to replay seeds → filed as regressions). Incremental + exact: only replays whose `qa_fp` overlaps changed hashes re-run. CI verdict is hashed (`qa_hash` + per-test verdicts + build_hash).
```
qa_automation_hash = H("v11.qaautomation", batch corpus refs, continuous-QA gate policy, fuzz-farm sharding + budget, qa_fp)
```

---

## 9. Determinism, CAS, and Skip-Work

**9.1** `qa_hash` folds seven. **9.2** Detachable verification wrapper (I-DETACH); `qa_fp` CONTAINS `build_hash(V1–V10)` (SUT = its oracle) — a verdict is permanently bound to the build it judged. **9.3 Partial recompilation:** test/assertion edit → qatype_hash (no SUT recompile); SUT changes → only replays/tests whose `qa_fp` references the changed hashes re-run (rest cached); new blessed reference → regress_hash (prior verdicts valid for prior builds); fuzz finding → new replay_hash added to corpus; backend added → only that backend's replay executes. **9.4 Verification determinism contract:** replay tuple complete/sufficient; drive via funnels only (no out-of-band); oracles = the stack's own hashes (logical/quantized state); logical-tick timing only; fuzz seeded + deterministic shrink; scenario deterministic generation; divergence localization; explicit provenance-stamped reference updates; backend parity over logical quantized state. **9.5 Skip-work:** a replay verdict is cacheable by `(replay_hash, build_hash)`; references/coverage maps/parity results are CAS artifacts; never invalidates a V1–V10 cache entry (I-DETACH).

---

## 10. Validation & Degrade-Never-Fail Model

**10.1** **Theorem (Total Verifiability).** Any V1–V10 stack can be equipped with a valid V11 layer that verifies determinism without perturbing it (I-VERIFY/I-DETACH), every artifact reproducible (I-EXP). No content-induced hard-fail **in the compiler sense**; hard errors: compiler/registry bugs **plus** a QA unit that would drive the SUT out-of-band (I-VERIFY breach). **Crucial distinction:** a **test FAILURE is not a V11 error — it is V11 succeeding** (it found a divergence/bug). V11's degrade-never-fail applies to the *machinery* (malformed test → no-op test), not the *verdicts* (a real divergence MUST be reported loudly, never degraded away).

**10.2 Validation:** test/assertion (well-typed; oracles resolve; logical-tick-only timing — wall-clock timing in a determinism test = reject; references real hooks); replay inputs (complete tuple; valid commands via funnels — out-of-band injection = reject; deterministic total order); fuzzing (seeded; bounded budget; explores only the bounded space; deterministic shrink; findings as replays); scenarios (deterministic; compiler-valid configs; clamped params); regression (oracles pinned to a build_hash; localization defined; reference updates require provenance); backend verification (logical quantized state; classification + per-backend pins).

**10.3** Diagnostics: `FAIL(divergence)` is a first-class output (bug coordinate + minimal replay), not a fault; `reject(verify_breach)` is the sole content-level hard stop.

**10.4 Degradation ladders (machinery only — findings never degraded):** Test/assertion (skip with recorded `info` = under-coverage, not a false pass → disable on unresolved oracle → reject non-reproducible timing). Replay (replay valid prefix + flag → "untested" on missing SUT, never a fake pass → reject out-of-band). Fuzz (fall back to `random_seeded` → reduced budget + recorded reduced coverage → keep unshrunk reproducer). Scenario (emit degraded scenario + flag → nearest valid). Regression (record "no baseline" → coarsest safe coordinate). Backend-verify (verify available subset + record "N unverified"). **Universal floor:** report reduced coverage *honestly* — degrade toward "untested/under-covered," never a false pass.

**10.5** Every artifact valid, reproducible (I-EXP), non-perturbing (I-VERIFY), detachable (I-DETACH); every verdict bound to an exact `build_hash`; every finding localized (I-DIVLOC) + replayable (I-EXP). "V1–V10 deterministic" ⇒ a deterministic verification layer that proves (or disproves) determinism by reproducible, byte-identical, cross-backend replay — the tester never perturbs the SUT, every result is reproducible, every regression localized to tick+subsystem+minimal replay, and reduced coverage reported honestly rather than as a false pass.

---

## Appendix A — V11 Identity Chain
```
…(V1–V10) → qa_fp (= contains build_hash(V1–V10), the SUT-as-oracle) → qatype │ replay │ scenario │ fuzz │ regress │ backendverify │ qa_automation → qa_hash (detachable)
```

## Appendix C — Mechanisms
1. Verify-Don't-Perturb (I-VERIFY). 2. Replay-Equivalence (I-REPLAY-EQ). 3. Reproducible-Experiment (I-EXP). 4. Divergence-Localization (I-DIVLOC). 5. Oracles = the stack's own hashes (no golden files; provenance-stamped refs). 6. Cross-backend parity over logical quantized state (audits the delegated boundary). 7. Detachable. 8. Fails loud, never false-green.

**End of V11 Specification — Automated QA, Fuzzing, and Deterministic Replay.**
