# V9 SPECIFICATION — Toolchain, Editor, and Authoring UX

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V9 (Authoring Environment)
**Status:** Normative specification
**Depends on:** V1–V8, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V8)

**0.1** Requirement language as prior layers.
**0.2 Compiler, not tool — even the tool layer.** V9 is specified as a **compiler over editing operations**: `compile(edit_ops, prior_state) → new_state + IR_deltas + identity`. The GUI is an exogenous *frontend* emitting edit operations; V9 defines their deterministic *semantics*, not pixels.
**0.3 Layering invariant.** V9 never re-certifies V1–V8; it is how a human (or LLM-as-author) drives the V1–V8 compiler — producing *edits* that become inputs and orchestrating builds.
**0.4 Micro-types + `clamp_enum`.** Editor/graph-editor/scene-editor/import/export/build-node/automation/plugin.
**0.5 Determinism floor.** Canonical IR.

**0.6 The Operation-Determinism Principle (I-OPDET — defining rule).** **Operation determinism (MUST):** every authoring action is a typed, hashed `edit_op` whose effect is a pure `apply(edit_op, state) → state'`; the document = deterministic fold of the ordered op-log over the initial state. **Exogenous frontend (delegated):** mouse coords, panel layout, render timing, drag trajectories, async I/O completion order resolve *into* edit_ops; NOT hashed. Same op log ⇒ same document regardless of window layout.

**0.7 The Edit-as-Compiler-Input Invariant (I-EDIT).** V9 does not mutate V1–V8 artifacts; an edit produces new **source**, and the lower compilers recompile from it (V1–V8 skip-work decides what re-runs). The editor never hand-edits a `mission_hash`; it edits the mission *source*. The editor cannot produce an artifact the compiler couldn't, nor bypass any lower-layer validation.

**0.8 The Snapshot-Reversibility Invariant (I-SNAP).** Editor state is snapshot-addressable + reversible: every state is content-addressed (`editorstate_hash`); undo/redo is op-log/snapshot-DAG navigation; any prior state is byte-recoverable.

**0.9 V9→V8 relationship.** Editor plugins (§10) are UGC-safe extensions governed by V8's I-CAP — data over a sealed toolkit, never trusted code in the compiler.

---

## 1. V9 Architectural Overview

### 1.1 Purpose
The editors, graph tools, importers, build orchestration, and preview that let an author drive the deterministic compiler — while authoring stays deterministic, snapshot-addressable, reversible.

| System | Produces | Identity |
|---|---|---|
| Editor Micro-Types | typed editor surfaces + op vocabularies | `editortype_hash` |
| Toolchain Orchestration | build graph + incremental scheduler | `build_hash` |
| Visual Graph Editors | rig/behavior/AI/mission/sim graph editing | `grapheditor_hash` |
| Scene & World Editors | spatial/placement/partition editing | `sceneeditor_hash` |
| Asset Import/Export | import/export micro-types + validation | `import_hash`, `export_hash` |
| UX State Model | session state + undo/redo + serialization | `editorstate_hash` |
| Multi-Backend Preview | live preview + compatibility checks | `preview_hash` |
| Automation | CLI/batch/CI | `automation_hash` |
| Editor Extensibility | plugin surfaces (V9→V8) | `plugin_hash` |

Root: `editor_hash`. (`build_hash` here = the toolchain build-graph identity; distinct from the project content `build_hash` it composes.)

### 1.2 Consumption of V1–V8 outputs
**Authoring Environment Table (AET):** the full registry + UGC-safe partition; V1–V8 source schemas (what editors edit); V1–V8 root hashes; CAS; per-layer validators (surfaced as live feedback); backend lowering. Edits **source documents**, never artifacts (I-EDIT). Surfaces, never bypasses, lower-layer validation. Plugins admitted through V8 sandbox. `ed_fp := {registry_version, V1–V8 source-schema hashes, target root hashes, ed_policy_hash}`.

### 1.3 Phase ordering
```
P1 Editor Micro-Type Layer (editortype_hash) → P2 UX State Model (editorstate_hash)
  → P3 Visual Graph Editors (grapheditor_hash) / P3' Scene & World Editors (sceneeditor_hash)
  → P4 Asset Import/Export (import_hash, export_hash) → P5 Toolchain Orchestration (build_hash)
  → P6 Multi-Backend Preview (preview_hash) → P7 Automation (automation_hash) → P8 Editor Extensibility (plugin_hash)
editor_hash = H(editortype, editorstate, grapheditor, sceneeditor, import, export, build, preview, automation, plugin, ed_fp, authoring_determinism_contract, versions)
```
State model (P2) underlies all editing; build graph (P5) turns edits into recompiles + multi-backend artifacts. I-EDIT/I-OPDET/I-SNAP hold throughout.

### 1.4 Identity
```
editor_hash = H("v9.editor", editortype_hash, editorstate_hash, grapheditor_hash, sceneeditor_hash, import_hash, export_hash, build_hash, preview_hash, automation_hash, plugin_hash, ed_fp, authoring_determinism_contract_hash, versions)
```
**I-ED-LOCAL**: one editor surface/op-definition change touches only its path; never a V1–V8 *content* hash.

---

## 2. Editor Micro-Type Inventory (P1)

### 2.2 Micro-types
`ed.rig/deformation/entity/scene/mission/ai_graph/simulation/presentation/network/liveops` — each editing the source for the corresponding layer.

### 2.3 Schema
```
editor_micro_type { id, version; edits_schema: ref to lower-layer SOURCE schema; op_vocabulary: sorted set<edit_op_def>;
                    inputs: selection/view context (exogenous, not hashed); outputs: source deltas (hashed) → build graph;
                    constraints: pre/post-conditions in apply(); validators: ref to target layer's validator (surfaced, not duplicated — I-EDIT); degrade }
```

### 2.4 Edit operations
```
edit_op { op_id; editor: ref; kind: clamp_enum(create|delete|set_field|connect|disconnect|move|reparent|group|transform|apply_template);
          target: source node ref; payload: canonical, quantized, clamp-validated; inverse: deterministic inverse op }
```
Every op carries its deterministic inverse (exact undo). `apply()` pure + total (invalid payloads clamp, never throw).

### 2.5 Editor → compiler integration
Editor emits source deltas; the build graph (§3) recompiles affected layers via CAS skip-work. Never writes artifacts (I-EDIT). Live validation = call the target layer's validator on the candidate source, surface DEGRADE inline.

### 2.6 Hashing
```
editortype_hash = H("v9.editortype", sorted(editor_micro_type defs w/ op vocabularies), edits_schema refs, validator refs, ed_fp)
```

---

## 3. Toolchain Orchestration (P5)

### 3.2 Build graph
```
build_graph { nodes: sorted set<build_node>; edges: sorted set<dep edge>; phases: ordered V1→…→V8 → backend export }
build_node { node_id; layer/phase ref; inputs: sorted(source/artifact hashes); output: artifact hash; backend?: clamp_enum(unity|unreal|blender|usd|all|none) }
```
Topology = pure fn of source set + target backends.

### 3.3 Incremental build (CAS-driven)
```
for each build_node in topological order (canonical tie-break): h = H(node spec, sorted(input hashes)); if CAS.contains(h) reuse else run+put
```
The same skip-work the whole stack uses, now orchestrated — the visible form of the per-layer I-*-LOCAL invariants. An edit rebuilds only its downstream nodes.

### 3.4 Multi-backend orchestration
Target = (content roots, backend set); fan out backend-export nodes per backend, each lowering the same IR; parallel; cached per `(content_hash, backend, policy_hash)`.

### 3.5 Hashing
```
build_hash = H("v9.build", build_graph topology, sorted(build_node hashes), target backend set, incremental policy, ed_fp)
# distinct from the PROJECT content build_hash (= H(V1..V8 roots)) it composes/produces
```

---

## 4. Visual Graph Editors (P3)

| Editor | Graph edited | Vocabulary | Validates against |
|---|---|---|---|
| Rig graph | rig hierarchy + constraints + proxies | bones, constraints, proxy bindings | V1 rig correctness |
| Behavior graph | V3 entity behavior closure + wiring | behavior micro-types, wire records | V3 composition |
| AI graph | V4 AI graph | BT/FSM/utility nodes, edges, perception | V4 AI validation |
| Mission graph | V3 mission graph | objective/trigger/condition/event/branch nodes, typed edges | V3 mission validation |
| Simulation graph | V5 sim subgraphs | weather/economy/ecology/faction nodes, couplers, fields | V5 boundedness |

Shared semantics: canonical node/edge ordering (BT child order preserved; others sorted; authoring order not hashed); live constraint enforcement (surface lower-layer DEGRADE at edit time); deterministic op inverses.
```
grapheditor_hash = H("v9.grapheditor", sorted(graph-editor defs: edited-graph ref, node/edge vocabulary, canonicalization policy, validator ref), ed_fp)
```

---

## 5. Scene & World Editors (P3')

`ed.scene` ops over spatial nodes/anchors/spawns/triggers/nav/zones — transforms **quantized on commit** (continuous drag exogenous; committed payload quantized). World partition authored (cell size/origin/streaming volumes); cell assignment is the V3 deterministic function (editor displays, doesn't invent — I-EDIT). Entity placement = V3 instance (quantized transform, whitelisted overrides; out-of-whitelist clamped + warned). Validation surfaced from V3 (overlap/unreachable/missing-coupling flags). Preview delegated to P6.
```
sceneeditor_hash = H("v9.sceneeditor", sorted(scene-editor defs: op vocabulary, quantize-on-commit policy, partition exposure, validator ref), ed_fp)
```

---

## 6. Asset Import/Export Pipeline (P4)

Import micro-types `imp.rig/mesh/deformation/animation/audio/texture`; import = deterministic normalization → CAS, **gated against V1/V2** (no bypass; failing → deterministic auto-fix or flagged placeholder). Same file twice → same hash. Export micro-types `exp.prefab/blueprint/collection/usd_prim` = the identity-preserving lowering already defined per layer; registered as build-graph nodes; cached per `(content_hash, backend, policy_hash)`.
```
import_hash = H("v9.import", sorted(import defs: normalization policy, gate refs, quantization), ed_fp)
export_hash = H("v9.export", sorted(export defs: backend, lowering ref, cache key policy), ed_fp)
```

---

## 7. UX State Model (P2)

```
editor_session { document: the hashed source document; view: panels/selection/camera/active tool (EXOGENOUS, not hashed);
                 op_log: ordered list<edit_op>; cursor: position in op_log/snapshot DAG }
document_N = fold(apply, document_0, op_log[0..N])
undo = cursor back (or nearest snapshot + replay) [exact via inverses]; redo = forward; branch = edit-after-undo forks the snapshot DAG (non-destructive)
```
Document hashed; view NOT (view-state persisted separately, outside the determinism boundary). Every state is `editorstate_hash`; any prior state byte-recoverable. **No nondeterministic UI randomness** in hashed state ("randomize" seeded `H(document_hash, op_id)`, quantized, undoable). Async funneled as ordered ops (I-OPDET).
```
editorstate_hash = H("v9.editorstate", document_hash (view EXCLUDED), op_log canonical form, snapshot-DAG topology, seed policy, ed_fp)
```

---

## 8. Multi-Backend Preview & Validation (P6)

Preview = a build target with `backend=<one>` + presentation enabled, run incrementally; reuses CAS skip-work; **read-only over the document** (visualizes a build; edits flow only through edit_ops). Backend-specific warnings = lower-layer export DEGRADEs + backend caveats, deterministic fn of `(content, backend)`, shown early.
```
preview_hash = H("v9.preview", sorted(preview target defs: backend, build target ref, compat-check policy), ed_fp)
# rendered preview frames are exogenous (V6 contract); only the preview SPEC is hashed
```

---

## 9. Automation & Pipeline Integration (P7)

CLI (a script frontend emitting the same edit_ops + build-graph runs headlessly — I-OPDET), batch builds (per `(content roots, backend set)`; shared CAS skip-work), CI hooks (on source change: build graph + all lower-layer validators + multi-backend compile; gate on a deterministic pass/degrade report). The toolchain expression of the verify-gate moat, generalized to all layers/backends. CI result is hashed (`build_hash` + verdicts).
```
automation_hash = H("v9.automation", CLI op surface, batch target sets, CI gate policy, ed_fp)
```

---

## 10. Editor Extensibility — V9 → V8 (P8)

| Plugin micro-type | Extends | Bounded by |
|---|---|---|
| `plug.panel` | new editor surface over an existing source schema | UGC-safe schemas; emits standard edit_ops |
| `plug.importer` | a new `imp.*` | output passes V1/V2 (no bypass) |
| `plug.node_palette` | new graph nodes = compositions of UGC-safe micro-types | I-CAP: no new kinds |
| `plug.validator` | additional advisory check | advisory only; cannot relax a gate |
| `plug.exporter` | a backend export variant | valid backend artifacts |

A plugin is data over a sealed editor toolkit (I-CAP); composes existing micro-types, emits edit_ops/source (I-EDIT — never artifacts); passes V8 sandbox; carries a trust tier; can only add surfaces, never relax a gate.
```
plugin_hash = H("v9.plugin", sorted(plugin defs: micro-type, extension point, declared UGC-safe caps, sandbox verdict, trust tier), ed_fp)
# plugin caps ⊆ UGC-safe (else rejected, I-CAP)
```

---

## 11. Determinism, CAS, and Skip-Work

**11.1** `editor_hash` folds the sub-hashes. **11.2** Authoring wrapper; editing changes V1–V8 content hashes only through recompilation of source (I-EDIT); `editor_hash` captures tooling config, not content (teams share `editor_hash` while producing different content; identical content is reproducible regardless of which `editor_hash` produced it). **11.3 Partial recompilation:** one edit_op → new document_hash → build graph recompiles only downstream nodes via CAS; tool/plugin config change → editortype/plugin/build hashes (content unchanged); backend set change → only that backend's export nodes; undo → cursor to prior `editorstate_hash` (often a pure CAS hit); import re-run on unchanged file → same hash → full skip-work. **11.4 Authoring determinism contract:** `apply()` pure+total + deterministic inverse; quantize-on-commit; canonical ordering (authoring order not hashed); view exclusion; editor RNG seeded; async funnel; build-graph topology = pure fn; plugin caps ⊆ UGC-safe + emit ops/source. **11.5 Skip-work:** the build graph IS skip-work made first-class; never invalidates a V1–V8 cache entry except by legitimately changing source.

---

## 12. Validation & Degrade-Never-Fail Model

**12.1** **Theorem (Total Authoring Environment).** Any deterministic stack (V1–V8) can be driven by a valid V9 environment in which every edit is reversible, every state snapshot-addressable, and no action produces an artifact the compiler couldn't or bypasses a gate (I-EDIT). No content-induced hard-fail; hard errors: compiler/tooling bugs **plus** the inherited I-CAP rejection (a plugin requesting sealed capability). Always-reversible (I-SNAP), compiler-bounded (I-EDIT), determinism-preserving (I-OPDET).

**12.2 Validation:** editor types (typed op vocabularies; deterministic inverse; total `apply()`; validators reference the target layer's, no divergence); build graph (acyclic; pure topology; resolvable inputs; correct incremental keys); graph editors (canonical order; enforce target rules at edit time; surface DEGRADE); scene/world (quantize-on-commit; V3 cell assignment; whitelist overrides); import/export (deterministic + V1/V2-gated; identity-preserving lowering); UX state (document hashed/view excluded; exact undo; non-destructive DAG; seeded RNG); plugins (caps ⊆ UGC-safe else reject; emit ops/source; advisory validators; pass V8 sandbox).

**12.3** Diagnostics deterministic + hashed in; warnings ARE the build's degrades shown early; `reject(caps_breach)` is the sole content-level hard stop.

**12.4 Degradation ladders:** Edit-op (clamp/quantize → no-op → nearest valid state + warning; always undoable). Build-graph (emit degraded artifact + continue → skip+flag → nearest safe artifact; build succeeds with report). Graph-editor (reject illegal connection w/ reason → force damping/gate → inert placeholder node). Scene-editor (commit+flag → ignore override → default partition). Import/export (auto-fix → flagged placeholder; nearest safe/metadata). UX-state (replay op-log from `document_0`; skip unreplayable op+flag; load nearest valid snapshot). Plugin (tighter sandbox/advisory → migrate/ignore → isolate; caps breach → reject). Universal floor: degrade toward "untested/under-covered/safe," never an unrecoverable state.

**12.5** Every action valid, reversible (I-SNAP), compiler-bounded (I-EDIT), determinism-preserving (I-OPDET); plugins capability-bounded (I-CAP); build graph total over valid sources. "V1–V8 deterministic" ⇒ a deterministic, reversible, snapshot-addressable authoring environment driving the full stack to valid multi-backend builds.

---

## Appendix A — V9 Identity Chain
```
…liveops/config (V8) → ed_fp → editortype │ editorstate │ grapheditor │ sceneeditor │ import │ export │ build │ preview │ automation │ plugin → editor_hash
```

## Appendix C — Mechanisms
1. Operation-Determinism (I-OPDET). 2. Edit-as-Compiler-Input (I-EDIT). 3. Snapshot-Reversibility (I-SNAP). 4. Quantize-on-commit. 5. Build graph = first-class skip-work. 6. Editor RNG hash-seeded. 7. Plugins V8-bounded (I-CAP). 8. Verify gate generalized.

**End of V9 Specification — Toolchain, Editor, and Authoring UX.**
