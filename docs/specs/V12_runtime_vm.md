# V12 SPECIFICATION — Runtime VM, Hot-Reload, and Live Debugging

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V12 (Runtime Execution)
**Status:** Normative specification
**Depends on:** V1–V11, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V11)

**0.1** Requirement language as prior layers.
**0.2 Compiler + reference runtime.** As compiler: `compile(IR_in, policy) → VM_image + identity`. As reference runtime: deterministically executes that image. Both pure w.r.t. hashed semantics (no wall-clock/ambient-RNG/path/thread-timing).
**0.3 Layering invariant.** Consumes V1–V11 as the trusted program it runs; the deterministic abstract machine that executes the compiled world + the operations (hot-reload/debug/play/patch) that mutate a *running* program without breaking determinism.
**0.4 Micro-types + `clamp_enum`.** vm-unit/instruction/evaluator/reload/debug/patch.
**0.5 Determinism floor.** Canonical IR.

**0.6 The Reference-Executor Principle (I-VMDET — defining rule).** **V12's VM is the reference** delegated layers were verified against. Deterministic instruction semantics (quantized values, integer-precise; no instruction reads wall-clock/entropy/address/uninitialized memory). Deterministic scheduling (fibers in fixed canonical order at each tick barrier; logical cooperative concurrency, never preemptive). Hash-seeded RNG (`H(world_seed, unit_id, tick, draw_index)`, stream-isolated). **The VM is the oracle:** native backends are alternate executors that MUST match; V11 verifies them; the VM is also the V7 authority executor and the replay/QA executor.

**0.7 The Mutation-as-Recorded-Transition Principle (I-LIVE).** Hot-reload/patch/inject/scene-swap = **versioned, tick-barrier-aligned, recorded transitions**, never silent in-place changes. A mutation at tick T → a new `build_hash`/`config_hash` + a transition descriptor in the session timeline. The session is piecewise-deterministic and fully replayable: build A `[0,T)`, recorded transition at T, build B `[T,∞)`; reproduces from `(snapshot@T, descriptor, inputs[T..])`. **The build becomes a time-varying recorded input.**

**0.8 The Time-Control Invariant (I-TIME).** Debug/play controls operate only on the tick timeline, never tick content. Pause = stop advancing ticks (zero logical effect — no wall-clock). Step = advance one barrier; step-back = load prior snapshot; re-step = deterministic re-advance. Breakpoint = halt advancement when a condition holds *at a tick boundary* (never mid-tick). Debugging = read-only navigation over the deterministic tick timeline; detaching it is byte-identical.

**0.9 The Play-Mode-Reversibility Invariant (I-PIE).** Entering play mode compiles live editor source → `build_hash` → a VM run in a separate snapshot-addressable sandbox that NEVER mutates the authoring source unless explicitly captured. Exit returns to the exact pre-play source.

---

## 1. V12 Architectural Overview

### 1.1 Purpose
Where the world actually runs: the deterministic reference VM that executes the compiled IR, the hot-reload pipeline, the live debugger, in-editor play mode, and the runtime patch/sandbox — all preserving reproducibility.

| System | Produces | Identity |
|---|---|---|
| Runtime VM | the deterministic abstract machine + image | `vm_hash` |
| Runtime Graph Evaluators | executors for AI/mission/sim/UI graphs | `rgraph_hash` |
| Hot-Reload Pipeline | reloadable transitions + state migration | `reload_hash` |
| Live Debugging | breakpoints/watch/time-control | `debug_hash` |
| In-Editor Play Mode | editor↔runtime bridge + reversible sandbox | `pie_hash` |
| Runtime Patching & Sandbox | bounded reversible live patches | `patch_hash` |
| Replay Integration | VM↔V11 replay hooks | `replayvm_hash` |

Root: `runtime_hash`.

### 1.2 Consumption of V1–V11 outputs
**Runtime Resolution Table (RRT):** `build_hash` (the program), `config_hash`, `qa_hash` (may refuse/flag an unverified build); V3 instantiated; V4 executed; V5 executed on the barrier; V6 driven (read-only); `net_hash` (the VM IS the authoritative executor); input funnels (the VM's only inputs); the VM EMITS `worldstate_hash@N`. `rt_fp := {build_hash, config_hash, world_seed, tick_schedule, qa_hash?, determinism_contract refs}`.

### 1.3 Phase ordering
```
P1 Runtime VM (vm_hash) → P2 Runtime Graph Evaluators (rgraph_hash) → P3 Hot-Reload (reload_hash)
  → P4 Live Debugging (debug_hash) → P5 In-Editor Play Mode (pie_hash) → P6 Runtime Patching/Sandbox (patch_hash) → P7 Replay Integration (replayvm_hash)
runtime_hash = H(vm, rgraph, reload, debug, pie, patch, replayvm, rt_fp, runtime_determinism_contract, versions)
```
VM is the substrate; evaluators run on it; reload/debug/pie/patch are operations over the running VM; replay wires it to V11. I-VMDET/I-LIVE/I-TIME/I-PIE hold.

### 1.4 Identity
```
runtime_hash = H("v12.runtime", vm_hash, rgraph_hash, reload_hash, debug_hash, pie_hash, patch_hash, replayvm_hash, rt_fp, runtime_determinism_contract_hash, versions)
```
**I-RT-LOCAL**: one VM-unit/evaluator/reload-rule change touches only its path. Executing a build doesn't change a V1–V11 content hash; it *produces* `worldstate_hash@N`.

---

## 2. Runtime VM Specification (P1)

### 2.2 Micro-types
`vm.execution_unit/fiber/scheduler/register_file/heap/rng_stream/tick_barrier`.

### 2.3 Instruction model
```
instruction { op: clamp_enum(read_slot|write_slot|eval_graph|emit_event|enqueue_command|apply_influence|spawn|despawn|rng_draw|yield|barrier);
              operands: quantized, typed (no raw pointers/addresses); determinism: integer-precise/quantized-float; no wall-clock/address/uninitialized }
```
**No nondeterministic op exists in the ISA** — determinism is a property of the instruction set, not discipline. All cross-unit communication tick-barriered (read N-1 / write N). Replay-safe by construction (consumes `(seed, schedule, input trace)`, produces `worldstate_hash@N` — this *is* I-REPLAY-EQ).

### 2.4–2.6 Integration & hashing
**→ V4:** executes gameplay units consuming V4 commands from the V7 queue; the canonical physics/AI executor (backends must match, V11-verified). **→ V5:** runs the sim graph on the barrier via the sim evaluator; emits `worldstate_hash@N`; honors temporal LOD. **→ V7:** the authoritative executor; rollback = "load VM snapshot @ N, re-execute to now"; desync = comparing VM-emitted hashes.
```
vm_image_hash = H("v12.vmimage", lowered program from build_hash, ISA version, quantization)
vm_hash = H("v12.vm", vm_image_hash, sorted(vm micro-type defs), instruction-set version, scheduler order policy, rng seed policy, tick-barrier model, rt_fp, degrade_decisions)
```

---

## 3. Runtime Graph Evaluators (P2)

| Evaluator | Executes | Rules |
|---|---|---|
| AI graph | V4 BT/FSM/utility | child order preserved (BT); transitions priority→canonical (FSM); quantized scores + canonical ties (utility); AI cadence |
| Mission graph | V3 mission graph | canonical node/edge order; conditions over snapshot; branch = first-satisfied-guard in canonical order |
| Simulation graph | V5 sim subgraphs | tick-barrier double-buffer; clamped/bounded; hash-seeded; canonical cell order |
| UI graph | V6 bindings | **read-only** (I-PROJ); reads world via bindings; never writes |

Evaluators read N-1 / write N (UI reads only); canonical order → reproducible. Pure interpreters of hashed graphs (no escape to native code — only the ISA). Malformed graph → layer's inert fallback.
```
evaluator_hash = H("v12.evaluator", evaluated-graph kind, evaluation semantics, cadence, read/write scope)
rgraph_hash = H("v12.rgraph", sorted(evaluator_hash…), canonical evaluation order, rt_fp)
```

---

## 4. Hot-Reload Pipeline (P3)

### 4.2 Reloadable vs non-reloadable
RELOADABLE (hot-swap at a barrier): behaviors, AI graphs, mission graphs, UI/presentation, sim rules/tuning, rulesets, cosmetics, adaptive policy. NON-RELOADABLE (cold restart): rig topology, deform/physics solvers, authority model, the determinism contract, the VM ISA. Attempting a non-reloadable hot-reload → DEGRADE to "requires restart" (snapshot → rebuild → resume from snapshot under the new build; still deterministic).

### 4.3 Reload boundary (tick-barrier only)
```
reload(T, buildA → buildB): 1 quiesce (complete T-1; clean snapshot S@T) → 2 diff (reloadable delta; touches only reloadable types)
  → 3 migrate (persisted state via V8/V9 migration) → 4 record (transition descriptor {T, buildA_hash, buildB_hash, migration_hash}) → 5 resume (tick under buildB from S@T)
```

### 4.4 State preservation
Per slot (declared + hashed): `persist` (carried + migrated), `reset` (re-initialized deterministically/seeded), `derive` (recomputed). Missing migration → default new slots. Snapshot-addressable + reversible (I-LIVE): `S@T + descriptor` reproduces post-reload; popping returns to S@T under buildA → a live-coding session is a replayable timeline of builds.
```
reload_hash = H("v12.reload", reloadable/non-reloadable partition, reload-boundary policy (tick-barrier only), sorted(state_disposition defs), migration policy ref, transition-descriptor schema, rt_fp, degrade_decisions)
```

---

## 5. Live Debugging Tools (P4)

```
breakpoint { scope: clamp_enum(entity|mission|ai|simulation|vm); condition: predicate over snapshot/registers, evaluated AT a tick barrier; action: halt tick advancement (NOT mid-tick) }
```
Watch: read-only inspection of V4/V5 state slots, `worldstate_hash@N`, VM registers/fibers, command queue, RNG positions (quantized display). Time controls (I-TIME): pause (zero logical effect), step (one barrier), step-back (load prior snapshot), re-step/play (deterministic re-advance), tick-barrier step (inspect N-1 read vs N write), run-to-breakpoint. **Time-travel debugging is native** (every tick is a reproducible snapshot). Detachable (I-DETACH).
```
debug_hash = H("v12.debug", sorted(breakpoint defs: scope, barrier-evaluated condition), sorted(watch defs), time-control model, rt_fp)
```

---

## 6. In-Editor Play Mode — PIE (P5)

```
enter_play(): 1 compile current editor SOURCE (V9) → build_hash + config_hash (V9 build graph; CAS skip-work) → 2 instantiate VM image in a SEPARATE sandbox → 3 run from seed/schedule (or a chosen snapshot)
exit_play(): discard sandbox state (default) OR capture as a named snapshot (explicit); return to the EXACT pre-play editor source (I-PIE)
```
PIE entry = "compile source → run VM" (cannot produce what the compiler couldn't; cannot mutate authoring source). **Scene hot-swap & entity inject/remove = live mutations = recorded tick-barrier transitions** (I-LIVE): inject/remove via `spawn`/`despawn` ISA ops at a barrier, recorded as exogenous edit events. A play session (incl. reloads/injections/swaps) is a fully replayable timeline.
```
pie_hash = H("v12.pie", editor→runtime bridge policy, scene-hot-swap model, entity inject/remove (recorded transitions), play-sandbox reversibility policy, rt_fp)
```

---

## 7. Runtime Patching & Sandboxing (P6)

Patch micro-types `patch.behavior/mission/ui/tuning/cosmetic`. A patch is a hot-reload scoped to a bounded set, governed by V8 I-CAP. Validation: (1) reloadable-only (else "requires restart"); (2) capability-bounded (sealed cap → **reject**); (3) bounded (no unbounded work; clamped; termination decidable); (4) reversible (recorded transition; popping returns byte-identically); (5) applied at a barrier with deterministic migration. UGC/mod/live-ops patches run in the **V8 sandbox** (trust-tiered, layer-scoped); a misbehaving patch is isolated (transition popped) → prior runtime state.
```
patch_hash = H("v12.patch", sorted(patch defs: micro-type, reloadable-only check, V8 caps ref, bounds, reversibility), sandbox isolation policy (V8 ref), rt_fp, degrade_decisions)
# a patch application → a recorded transition descriptor; reversible
```

---

## 8. Replay Integration — V12 → V11 (P7)

| Hook | Provides V11 |
|---|---|
| VM replay | drive the VM with a replay tuple; emits `worldstate_hash@N` per barrier (the oracle) |
| Graph replay | re-run one evaluator from a snapshot for focused regression |
| Simulation replay | replay from any V5 snapshot forward (fast partial) |
| Record hook | a live session logs its input trace + transition descriptors → a complete replayable timeline |

V11's replay engine executes **on the V12 VM** — I-REPLAY-EQ is realized by I-VMDET. The VM is the canonical producer of `worldstate_hash@N`; backends verified against it. Recorded live mutations are replayable (V11 can regression-test hot-reloading itself). Rollback (V7) = VM replay from snapshot — one mechanism for networking prediction, QA replay, and debugger step-back.
```
replayvm_hash = H("v12.replayvm", VM/graph/sim replay hook specs, record-hook schema, oracle-emission policy (worldstate_hash@N per barrier), rt_fp)
```

---

## 9. Multi-Backend Runtime Mapping

The VM is the reference; backends **embed** it (use directly — guaranteed determinism) or **mirror** it (native execution that MUST match, V11-verified).

| Backend | Integration | Relationship |
|---|---|---|
| Unity | Mono / IL2CPP | mirror (verified) or embed for sim/authority |
| Unreal | Blueprint / C++ (+Iris) | mirror (verified) or embed for authoritative sim |
| Blender / UPBGE | UPBGE runtime / Python | embed (deterministic by construction) |
| USD | simulation layers / Hydra | embed (drive USD as presentation/sim target) |

Headless/server (V7 authority) always embeds the VM. Hot-reload maps to Unity domain reload/IL2CPP limits, Unreal Live Coding/BP hot-reload, UPBGE script reload, USD layer reload; where native hot-reload is limited → DEGRADE to "snapshot → rebuild → resume." Identity sidecar: `backend_mode: embed_vm|mirror_native`, `session = {build_hash, config_hash, transition timeline ref}`, `parity_ref` (mirror backends carry their verified-against-VM proof).

---

## 10. Determinism, CAS, and Skip-Work

**10.1** `runtime_hash` folds seven. **10.2** Executor of the chain: consumes `build_hash`, *produces* `worldstate_hash@N` (the V11 oracle); detachable yet the layer that makes the world exist in time; a running session = `(build_hash, config_hash, seed, schedule, transition timeline)`. **10.3 Partial recompilation:** VM ISA/scheduler edit → vm_hash (content unchanged; V11 re-verifies the executor); evaluator edit → rgraph_hash; reloadable content edit → a **hot-reload transition** at the next barrier (relower only the changed micro-type; migrate state); non-reloadable edit → cold restart; debug/pie config edit → byte-identical execution (detachable); a live mutation → appends a transition descriptor. **10.4 Runtime determinism contract:** no nondeterministic op in the ISA; canonical scheduling; tick barrier; RNG `H(world_seed, unit_id, tick, draw_index)`; quantization; reload at barriers only (recorded transitions); deterministic state migration; tick-timeline-only time controls; source-immutable reversible play; `worldstate_hash@N` emitted each barrier. **10.5 Skip-work:** the VM image is incrementally relowered (hot-reload relowers only the changed reloadable micro-type → fast + deterministic); replay results/snapshots/descriptors are CAS artifacts.

---

## 11. Validation & Degrade-Never-Fail Model

**11.1** **Theorem (Total Execution).** Any verified build (V1–V11) can be executed by a valid V12 runtime that runs/reloads/debugs/patches without breaking determinism (I-VMDET/I-LIVE/I-TIME/I-PIE). No content-induced hard-fail; hard errors: compiler/VM bugs **plus** a non-reloadable hot-reload attempt (→ forced cold restart, not a silent unsafe swap) and a sealed-capability patch (V8 I-CAP). Execution-deterministic, live-mutation-safe, observation-safe, play-safe.

**11.2 Validation:** VM (deterministic ISA — no nondeterministic op; canonical scheduling; hash-seeded RNG; quantized; image lowers totally); reload boundaries (barriers only; reloadable-only else restart; state dispositions declared; migration deterministic); debug (barrier-evaluated breakpoints; read-only watches; tick-timeline-only controls; detachable); patches (reloadable-only; V8 caps — sealed → reject; bounded + reversible; barrier; sandbox-isolated); PIE (entry compiles source → build_hash; source-immutable reversible sandbox; recorded scene-swap/injection).

**11.3** Diagnostics: a runtime fault → recorded fault + snapshot at the faulting tick + DEGRADE to inert fallback (session continues); `reject(noreload_breach)` forces cold restart; `reject(caps_breach)` rejects the patch.

**11.4 Degradation ladders:** VM (unit faults → inert + recorded fault + snapshot; evaluator can't resolve → layer inert fallback). Reload (non-reloadable → cold restart; missing migration → default new slots; bad point → defer to next barrier). Debug (unresolvable breakpoint → disabled; watch on missing slot → "unavailable" not a fake value; step-back beyond retained snapshots → nearest retained). Patch (non-reloadable → cold-restart path; over-budget → no-op; unverified UGC → tighter sandbox/isolate; sealed cap → reject; worst → patch isolated). PIE (source fails to compile → can't enter, never run stale; runtime fault → surface + continue or safely stop sandbox, source untouched). Universal floor: "no-op + recorded + keep running deterministically," or "snapshot + cold restart" for structural changes — never undefined/unrecoverable.

**11.5** Every operation valid, deterministic (I-VMDET), live-mutation-safe (I-LIVE), observation-safe (I-TIME/I-OBS), play-safe (I-PIE); patches capability-bounded (I-CAP); the VM total over verified builds. "V1–V11 deterministic & verified" ⇒ a deterministic reference runtime that executes, hot-reloads, debugs, and patches the world live — the ISA forbids nondeterminism, every live mutation is a recorded reversible tick-transition, debugging is read-only timeline navigation, play mode never corrupts source, and the VM is the oracle V11 verifies backends against.

---

## Appendix A — V12 Identity Chain
```
…(V1–V11) → rt_fp (= build_hash + config_hash + seed + schedule) → vm │ rgraph │ reload │ debug │ pie │ patch │ replayvm → runtime_hash (PRODUCES worldstate_hash@N)
```

## Appendix C — Mechanisms
1. Reference-Executor (I-VMDET; no nondeterministic opcode). 2. Mutation-as-Recorded-Transition (I-LIVE). 3. Time-Control = Tick-Timeline Navigation (I-TIME). 4. Play-Mode-Reversibility (I-PIE). 5. Reloadable/non-reloadable partition. 6. Deterministic state migration. 7. The VM is V11's reference & the V7 authority. 8. Incremental VM image relowering.

**End of V12 Specification — Runtime VM, Hot-Reload, and Live Debugging.**
