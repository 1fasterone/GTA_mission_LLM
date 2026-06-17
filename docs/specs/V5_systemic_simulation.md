# V5 SPECIFICATION — Systemic Simulation & Emergent World Logic

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V5 (Systemic Simulation)
**Status:** Normative specification
**Depends on:** V1–V4, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V4)

**0.1–0.5** As prior layers (MUST/SHOULD/MAY/DEGRADE; pure compiler; layering invariant — consumes V1–V4 hashes as trusted leaves; micro-types + `clamp_enum`; canonical-IR determinism floor). **V5 emits a deterministic simulation specification; it does not run the simulation.**

**0.6 The Simulation Determinism Contract.** **Spec determinism (MUST):** emitted config (graphs, rules, rates, cadences, propagation order, seeds, state schema, thresholds) is a pure, fully-hashed function of inputs. **Simulation determinism (CONTRACTED):** given `(world_seed, tick_schedule, ordered exogenous input trace)`, world evolution is logically bit-reproducible at the quantized-state level, via five pinned mechanisms (§11.4): fixed tick cadences; hash-seeded per-`(system,tick,subject)` RNG; the tick-barrier double-buffer (§2.5); clamped/bounded state with damped feedback (§0.7); quantized propagation order. Backend float bit-exactness delegated; all *inputs* pinned/hashed. Corollary: the world is **snapshot-addressable** — `worldstate_hash@N` = pure fn of `(seed, schedule, inputs[0..N])`.

**0.7 Boundedness / termination guarantee (MUST).** Every quantity (population, stock, influence, hazard, price) range-clamped; every feedback loop damped (quantized decay); every subsystem under a fixed per-tick budget. No unbounded growth/overflow. A config that could diverge is rejected at validation and DEGRADED to a damped/clamped variant.

---

## 1. V5 Architectural Overview

### 1.1 Purpose
Make the world itself live: factions contend, ecologies cycle, economies flow, weather turns, emergent events arise — feeding new missions back to the player.

| System | Produces | Identity |
|---|---|---|
| Simulation Graph substrate | scheduler, tick model, world blackboard, propagation engine | `simgraph_hash` |
| Faction & Influence | goals, hostility, diplomacy, influence fields | `faction_hash` |
| Ecology & Population | species, food chains, birth/death/migration | `ecology_hash` |
| Economy & Resources | resource nodes, production/consumption, markets | `economy_hash` |
| Weather & Environment | weather state, hazards, fields | `weather_hash` |
| Emergent Events | threshold-triggered events | `emergence_hash` |
| Persistent World State | global/entity/system state schema + snapshots | `worldstate_hash` |
| Dynamic Mission Generation | sim-event → V3 mission seeds | `dynmission_hash` |

Root: `sim_hash`.

### 1.2 Consumption of V1–V4 outputs
**Simulation Resolution Table (SRT):** V3 scene partition/cells/spawn-points/zones/nav-nodes/landmarks; V3 mission compiler + templates; V4 AI graphs (modulated), physics modifiers, state slots, event bus; V3 entities (spawnable species); V1 assets. **V3 partition is the spatial substrate for all V5 fields** (influence/weather/population/resource over cells → streaming-aware/cell-localizable). V5 modulates V4 only by publishing to subscribed channels. The dynamic-mission bridge (§9) is the one feedback edge, kept acyclic. `sim_fp := {scene, mission, gameplay_hash, partition_scheme_ref, entity_refs?, asset_refs?}`.

### 1.3 Phase ordering (build DAG; runtime is tick-barriered)
```
P1 Simulation Graph Substrate (simgraph_hash) → P2 Weather (weather_hash) → P3 Ecology (ecology_hash)
  → P4 Economy (economy_hash) → P5 Faction (faction_hash) → P6 Emergent Events (emergence_hash)
  → P7 Persistent World State (worldstate_hash) → P8 Dynamic Mission Generation (dynmission_hash)
sim_hash = H(simgraph, weather, ecology, economy, faction, emergence, worldstate, dynmission, sim_fp, determinism_contract, versions)
```
Runtime tick-barrier makes mutually-referential systems (economy↔faction) commutative/parallelizable. No back-edges except the acyclic snapshot-keyed mission bridge.

### 1.4 Identity
```
sim_hash = H("v5.sim", simgraph_hash, weather_hash, ecology_hash, economy_hash, faction_hash, emergence_hash, worldstate_hash, dynmission_hash, sim_fp, determinism_contract_hash, versions)
```
**I-SIM-LOCAL**: one rule/rate change touches only its path to `sim_hash`.

---

## 2. Systemic Simulation Graph (P1)

### 2.1 Purpose
The spine: deterministic scheduler + propagation engine + shared world blackboard over which all subsystems run.

### 2.2 Inventory
Driver (`sim.source`, `sim.clock`), State field (`sim.scalar_field`, `sim.vector_field`, `sim.aggregate`, `sim.stock`, `sim.flow`), Rule (`sim.rate`, `sim.threshold`, `sim.transfer`, `sim.diffuse`, `sim.coupler`), Cadence (`sim.cadence`, `sim.lod`), Sink (`sim.publish`, `sim.event_emit`).

### 2.3 Schema
```
sim_graph { sim_id; blackboard: sorted set<world_slot>; fields: sorted set<field_def>; nodes; edges; cadences; lod_policy }
sim_node { node_id; type; micro_type; params(canonical, quantized); cell_scope }
sim_edge { from; to; kind: clamp_enum(propagate|couple|diffuse|publish); weight(quantized); delay_ticks }
```
All rates/flows clamped; cross-subsystem edges `delay_ticks ≥ 1`; intra-subsystem cycles damped or DEGRADE.

### 2.4 Time-scaled simulation
Calendar (`sim.clock`): tick→(hour,day,week,season). Subsystems declare update cadence (integer tick stride); scheduler runs nodes whose cadence divides N (modular schedule, no float time) = **temporal LOD**.

### 2.5 The tick-barrier double-buffer rule (determinism core)
```
At tick N: read = immutable snapshot @ N-1 (buffer A); write = fresh buffer N (buffer B);
every subsystem reads ONLY A, writes ONLY B; cross-subsystem coupling is 1-tick-delayed; swap at the barrier.
```
Per-tick order irrelevant → commutative/parallelizable/reproducible. RNG = `H(world_seed, system_id, N, subject_hash)`, stream-isolated (**I-SIM-RNG-ISOLATION**).

### 2.6 Spatial simulation LOD (agent ↔ statistical)
Near = agent-level (real V4 entities); far = statistical (per-cell aggregate). Handoff deterministic: **quantized distance buckets** (no hysteresis); demotion = order-independent reduction; promotion = hash-seeded instantiation `H(world_seed, cell, N, index)` over canonical eligible entities; **conservation invariant** (counts/resources preserved across handoff within quantization).

### 2.7 Hashing
```
field_hash = H("v5.field", type, cell_scope, clamp_range, default, diffusion_params)
sim_node_hash = H("v5.snode", type, micro_type, canonical(params), cell_scope, cadence_ref)
simgraph_hash = H("v5.simgraph", sorted(world_slot defs), sorted(field_hash…), sorted(sim_node_hash…), sorted(sim_edge records), sorted(cadence_def), lod_policy, calendar_def, sim_fp, degrade_decisions)
```

---

## 3. Faction & Influence System (P5)

### 3.1 Purpose
Factions pursue goals, control territory via influence fields over scene cells, maintain diplomacy/hostility, seed conflict.

### 3.2 Micro-types
`fac.faction/goal/hostility_matrix/diplomacy_rule/order`, `inf.field/pressure/threat`.

### 3.3 Influence maps
Per-cell scalar/vector fields (control/pressure/threat) computed by `sim.diffuse` from home cells, quantized distance decay, blocked by impassable nav/terrain. Cell-localized → streaming-aware skip-work. Contested cells (|control_A−control_B| < tol) flagged for P6.

### 3.4 Faction decision logic
Deterministic utility over world state (quantized inputs; highest score; canonical ties). Goals emit `fac.order`s (1-tick-delayed) consumed by V4. Reads only the N-1 snapshot.

### 3.5 Faction → mission seeds
State transitions produce P8 seeds (war→defend/sabotage in contested cells; expansion→reach/escort). Pure fn of faction snapshot, with provenance. Unbacked seed → no-seed DEGRADE.

### 3.6 Hashing
```
faction_def_hash = H("v5.faction", identity, home_cell, sorted(goal defs), clamp(stocks), traits)
influence_hash = H("v5.influence", sorted(field_hash…), diffusion_params, blocker_source_ref)
faction_hash = H("v5.factions", sorted(faction_def_hash…), hostility_matrix_hash, sorted(diplomacy_rule), influence_hash, sorted(order bindings), sim_fp, degrade_decisions)
```

---

## 4. Ecology & Population Simulation (P3)

### 4.1 Purpose
Species populations cycling birth/death/migration under food-chain/resource constraints, integrated with V3 spawns + V4 AI.

### 4.2 Species micro-types
`eco.predator/prey/neutral/ambient/resource_node`, each binding an `entity_hash` + per-region `sim.stock` (clamped).

### 4.3 Population rules (per region, per cadence, quantized/clamped)
birth `+rate·pop·resource`, death (natural+predation+scarcity), migration (flow to neighbors along V3 nav-bound paths when pressure>threshold, conserved), scarcity (suppress birth + trigger migration, damped).

### 4.4 Food chain & dependency graph
`resource_node → prey → predator` with quantized conversion efficiency; boundedly stable (cycles damped, stocks clamped). Unstable web → DEGRADE to clamped logistic dynamics.

### 4.5 Ecology → scene
Far = aggregate; near = promoted to V3 spawn points / V4 AI via LOD handoff (§2.6, conserved). Migration paths over V4 nav tiles; spawn budgets published (1-tick-delayed).

### 4.6 Hashing
```
species_hash = H("v5.species", micro_type, entity_ref, clamp(pop params), sorted(diet edges))
ecology_hash = H("v5.ecology", sorted(species_hash…), foodweb_graph_hash, sorted(migration path refs), sorted(region stock defs), spawn binding refs, sim_fp, degrade_decisions)
```

---

## 5. Economy & Resource Simulation (P4)

### 5.1 Purpose
Resources produced/consumed across settlements; prices by supply/demand; scarcity propagating along trade routes; shortages/surpluses seeding missions.

### 5.2 Resource micro-types
`res.raw/refined/consumable/strategic/node/market`.

### 5.3 Production/consumption graphs
Recipe graph `inputs→output` with quantized ratios + per-cadence throughput gated by labor (ecology@N-1). Consumption draws from `res.market` by demand. All stocks/flows clamped.

### 5.4 Supply/demand
Price = clamped deterministic fn of stock vs demand (quantized inverse-stock curve, bounded `[min,max]`). Updated per cadence from N-1.

### 5.5 Market events & scarcity propagation
Scarcity propagates along trade routes (V4-nav-bound, 1-tick-delayed, damped); surplus inversely. Events `crash/boom/embargo` at quantized thresholds → P6.

### 5.6 Economy → mission
Shortage→trade/escort/raid; surplus→trade; strategic contention→defend/sabotage. Seeds carry provenance.

### 5.7 Hashing
```
resource_hash = H("v5.resource", micro_type, tier, clamp(params), sorted(recipe edges))
market_hash = H("v5.market", cell_ref, sorted(stock defs), price_curve, clamp range)
economy_hash = H("v5.economy", sorted(resource_hash…), production_graph_hash, sorted(market_hash…), sorted(trade route refs), event thresholds, sim_fp, degrade_decisions)
```

---

## 6. Weather & Environmental Simulation (P2)

### 6.1 Purpose
Weather states cycling over zones; hazards propagating as fields; modulating scene presentation, physics, AI.

### 6.2 Micro-types
`wx.state/transition/field/season_bias`, `haz.fire/flood/radiation/contamination`.

### 6.3 Hazards
Deterministic propagation fields: fire by quantized flammability (scene material tags) + wind; flood by elevation; radiation/contamination by diffusion + quantized decay; intensities clamped `[0,1]`; order-independent (N-1 snapshot). Emit events at thresholds.

### 6.4 Weather → scene
Published (1-tick-delayed) to V3/V4 hooks: lighting zone params, audio zones, physics modifiers (wind force, wet friction, flood buoyancy). Writes existing hooks only.

### 6.5 Weather → AI
Writes AI blackboard inputs (shelter-seeking, hazard avoidance via published `nav.area_cost`, reduced perception in low visibility). Missing slot → DEGRADE (no modifier).

### 6.6 Hashing
```
wx_zone_hash = H("v5.wxzone", zone_ref, sorted(state defs), transition_matrix, season_bias)
hazard_hash = H("v5.hazard", type, propagation_params, clamp_range, source_tag_ref)
weather_hash = H("v5.weather", sorted(wx_zone_hash…), sorted(wx_field defs), sorted(hazard_hash…), scene/ai/physics publish bindings, sim_fp, degrade_decisions)
```

---

## 7. Emergent Event System (P6)

### 7.1 Purpose
Discrete world events from subsystem interactions — deterministic as pure functions of threshold crossings over the N-1 snapshot, with canonical firing order and full provenance.

### 7.2 Micro-types
`evt.conflict/disaster/discovery/migration/scarcity/faction_escalation`.

### 7.3 Triggers
`sim.threshold` predicate over quantized world state, edge-triggered with quantized hysteresis. Multiple firings ordered canonically by `(event_type, source_cell, provenance_hash)`. Each event carries a hashed **provenance record** (snapshot hash, crossing quantity, contributing subsystem hashes).

### 7.4 Event → mission generation
Fixed hashed `event_type → candidate templates` table (disaster→rescue/evacuate; conflict→defend/sabotage; scarcity→trade/escort/raid). Seed includes provenance, targets, difficulty hint.

### 7.5 Event → scene
Publishes (1-tick-delayed) spawn budgets, hazard activation, faction orders, NPC redeployment via existing hooks.

### 7.6 Hashing
```
trigger_hash = H("v5.trigger", event_type, threshold_predicate(quantized), hysteresis, source_refs)
emergence_hash = H("v5.emergence", sorted(trigger_hash…), event→template table_hash, scene publish bindings, canonical firing-order policy, sim_fp, degrade_decisions)
```

---

## 8. Persistent World State (P7)

### 8.1 Purpose
Authoritative deterministic save/loadable world state + snapshot model (hashes schema/layout; runtime values are snapshots keyed by `(seed, schedule, inputs[0..N])`).

### 8.2–8.4
Global (calendar/tick/seed, per-cell fields, faction stocks/relations, global flags). Entity (V4 state + systemic attributes) keyed by `(entity_hash, instance_hash)`. Faction/ecology/economy persistent state.

### 8.5 Save/load micro-types
`world.snapshot/checkpoint/delta/migration`. Persistence boundary = union of `persist=true` slots, canonical order → reproducible portable save. **I-WORLD-REPLAY** (extends I-STATE-REPLAY): a save ≡ a snapshot OR `(seed, schedule, input trace)` — both reproduce the world. Missing migration → default new slots.

### 8.6 Hashing
```
worldstate_hash = H("v5.worldstate", sorted(global slot schema), sorted(entity slot schema), faction/ecology/economy/weather state schema, sorted(save micro-type defs), schema_version, sim_fp)
# schema/layout only; runtime values addressed by worldstate_value_hash@N
```

---

## 9. Dynamic Mission Generation — V5 → V3 Bridge (P8)

### 9.1 Purpose
Emergent events + world state → new V3 missions, deterministically + content-addressed. The single feedback edge.

### 9.2 Seeds & acyclic bridge
```
mission_seed { seed_id = H(provenance_hash, template_id, snapshot_hash@N); template_ref; target:{cells,entity_refs,faction_refs,resource_refs}; difficulty; provenance }
```
A dynamic mission is produced by invoking the **V3 mission compiler** as a pure function of `(seed, snapshot_hash@N)` → a new `mission_hash`. V5 never mutates an existing mission. Edge = `V5 → (snapshot) → V3-compiler → new mission_hash` (DAG, not cycle).

### 9.3 Difficulty scaling
Quantized fn of snapshot quantities (faction strength, hazard intensity, scarcity, player progression) → clamped buckets → template params.

### 9.4 Templates
`tmpl.reach/rescue/defend/escort/trade/sabotage` — parametric V3 mission-graph skeletons. Unbindable target → DEGRADE down a fixed ladder to a trivially-completable stub.

### 9.5 Hashing
```
template_hash = H("v5.tmpl", template_id, version, parametric skeleton)
mission_seed_hash = H("v5.seed", provenance_hash, template_ref, snapshot_hash@N, target refs, difficulty)
dynmission_hash = H("v5.dynmission", sorted(template_hash…), seed→template binding policy, difficulty model, sim_fp, degrade_decisions)
# a generated mission is a normal V3 mission_hash, keyed in CAS by mission_seed_hash
```

---

## 10. Multi-Backend Export Rules

Emits declarative data + refs to standard runtimes, never bespoke sim code. Sim graph → SO graph (Unity) / Data Asset/Mass processor config (Unreal) / datablock (Blender) / metadata subtree (USD); fields → Texture2D/3D / render targets-data layers / images / volume attrs. Faction/influence → grid assets + data assets. Weather → data + existing presentation hooks (post-process/sky/world/lights). Dynamic mission → exactly a V3 mission export + seed/provenance metadata. Event bus → native eventing. Identity sidecar adds `determinism_contract_hash`, optional `snapshot`, `provenance`. GUIDs from content hashes.

---

## 11. Determinism, CAS, and Skip-Work

**11.1** `sim_hash` folds 8 sub-hashes (Merkle). **11.2** Extends the V1 chain above `gameplay_hash`; same CAS. Generated missions are normal `mission_hash` CAS entries keyed by `mission_seed_hash`.
**11.3 Partial recompilation:** weather transition prob→weather_hash (runtime values differ on replay; spec recompile local); faction home cell→affected influence cells only; resource recipe→economy_hash; mission template→dynmission_hash (prior generated missions stay valid); scene cell change→only that cell's V5 fields; schema slot→worldstate_hash.
**11.4 Determinism contract (pinned/hashed):** integer tick cadences (modular schedule); world seed + RNG `H(world_seed, system_id, tick_N, subject_hash)` (stream-isolated); coupling `delay_ticks ≥ 1`; propagation order canonical (cell index then field id, fixed iterations); LOD distance buckets + conservation; quantization (integerized/`q_param`/`q_rate`/`q_field`, round-half-even); clamps/damping (boundedness); quantized hysteresis bands.
**11.5 Skip-work:** spec reuse + runtime transition memo `(worldstate_value_hash@N, input_delta) → @N+1` + generated mission `(mission_seed_hash) → mission_hash`.

---

## 12. Validation & Degrade-Never-Fail Model

**12.1** **Theorem (Total World).** Any V4-passing layer gets a valid V5 layer (sim graph, factions, ecology, economy, weather, events, state, dynamic missions), possibly simplified to a stable steady-state, always bounded/backend-safe/deterministic. No content-induced hard-fail; only compiler/registry bugs (incl. an undampable loop) are hard errors (CI-caught).

**12.2 Validation:** sim graphs (all clamped; cross-subsystem `delay_ticks≥1`; cycles damped; cadences integer; LOD conservation; **boundedness static check** — failing → DEGRADE to logistic/clamped); faction (matrix well-formed, goals have predicates, fields cell-scoped, orders bind/no-op); ecology/economy/weather (stable+clamped, refs resolve, hooks exist/degrade); events (quantized+hysteresis, canonical firing, complete provenance, total event→template table); dynamic missions (seed→template, binds real cells/entities or degrades, passes V3 validator); integration (resolvable hashes, only V3/V4-published hooks, acyclic snapshot-keyed bridge).

**12.3** Diagnostics deterministic + hashed in.

**12.4 Degradation ladders:** Sim graph (undampable loop → clamp+decay; unstable subsystem → freeze steady-state). Faction (drop goal/order; freeze borders; single neutral faction). Ecology (clamped logistic; no migration; static populations/ambient). Economy (fixed prices; drop recipe; static stocks). Weather (fixed climate; logic-only; static clear). Emergence (drop trigger; widen hysteresis; no events). Dynamic mission (template ladder → `tmpl.reach` stub; no dynamic missions). World state (default new slots; namespace; minimal global state).

**12.5** Every artifact valid + provably bounded; lowering total. "Passes V1/V2/V3/V4" ⇒ loadable, deterministic, bounded, snapshot-replayable living world on Unity/Unreal/Blender/USD; every emergent system can freeze to a steady-state and still build.

---

## Appendix A — V5 Identity Chain

```
asset/rig/deform/anim → entity/scene/mission (V3) → gameplay (V4) → sim_fp →
  simgraph │ weather │ ecology │ economy │ faction │ emergence │ worldstate │ dynmission → sim_hash
```

## Appendix B — Phase I/O

| Phase | Consumes | Produces | Hash |
|---|---|---|---|
| P1 Sim Graph | V3 partition, V4 hooks, calendar | scheduler/blackboard/fields/double-buffer | `simgraph_hash` |
| P2 Weather | scene zones, materials, calendar | weather states + hazard fields | `weather_hash` |
| P3 Ecology | weather@N-1, resources@N-1, spawns, AI | populations + migration | `ecology_hash` |
| P4 Economy | ecology@N-1, faction@N-1, anchors, nav | resources/markets/routes | `economy_hash` |
| P5 Faction | economy@N-1, territory@N-1, cells, AI | factions/diplomacy/influence/orders | `faction_hash` |
| P6 Emergence | P2–P5 N-1 snapshots | events + provenance + seeds | `emergence_hash` |
| P7 World State | aggregate + V4/V3 state | world schema + snapshots + save | `worldstate_hash` |
| P8 Dyn Mission | events + snapshot, V3 compiler | generated V3 missions | `dynmission_hash` |

## Appendix C — Determinism mechanisms
1. Integer tick cadences (temporal LOD). 2. Hash-seeded stream-isolated RNG. 3. Tick-barrier double-buffer. 4. Bounded/clamped + damped feedback. 5. Quantized everything + canonical propagation order. 6. Quantized-distance LOD with conservation. 7. Snapshot-addressable world.

**End of V5 Specification — Systemic Simulation & Emergent World Logic.**
