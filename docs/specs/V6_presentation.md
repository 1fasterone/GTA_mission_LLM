# V6 SPECIFICATION — Presentation, Camera, and UI/HUD Systems

**Project:** Deterministic Multi-Backend Game-Assembly Compiler
**Layer:** V6 (Presentation)
**Status:** Normative specification
**Depends on:** V1–V5, Retarget & Animation-Clip Mapping

---

## 0. Normative Conventions (inherited from V1–V5)

**0.1–0.5** As prior layers. **V6 emits a deterministic presentation specification; it does not render frames.**

**0.6 The Presentation Determinism Contract.** **Spec determinism (MUST):** emitted config (camera graphs, UI layouts, binding tables, feedback rules, LOD policy, accessibility profiles) is a pure, fully-hashed function of inputs. **Pure-projection determinism (CONTRACTED):** presentation state = pure fn of `(world state, view parameters, exogenous input trace)`, where view parameters (resolution, aspect, DPI, safe-area, applied accessibility profile) and camera input (player look/zoom) are **exogenous**, not hashed. Frame timing, GPU rasterization, audio mixing delegated.

**0.7 The Read-Only Projection Invariant (I-PROJ — defining V6 rule).** **V6 MUST NOT write gameplay or simulation state.** Presentation reads V1–V5 via `read`/`subscribe` only; never a `write/drive/publish` binding to a gameplay/sim hook. A follow camera reads the player transform; combat aim is a V4 input the camera *visualizes*, not produces. A HUD reads `health`, never sets it.

**0.8 The Headless Strippability Invariant (I-STRIP).** Removing V6 leaves the V1–V5 identity chain byte-identical. A headless server and a full client share an identical world `build_hash` for V1–V5. Presentation is the one fully-detachable layer — structurally incapable of perturbing the world.

---

## 1. V6 Architectural Overview

### 1.1 Purpose
How a human perceives the world: camera framing, UI/HUD surfacing state, feedback (VFX/SFX/indicators), accessibility — all read-only projections that never alter the world.

| System | Produces | Identity |
|---|---|---|
| Camera | rigs + camera graph + transitions + wiring | `camera_hash` |
| Presentation Binding | read-only projection table | `binding_hash` |
| UI / HUD | layouts + widgets + diegetic UI | `ui_hash` |
| Feedback Channels | VFX, SFX, screen-space indicators | `feedback_hash` |
| Accessibility & Readability | profiles transforming the spec | `a11y_hash` |
| Presentation LOD & Composition | visibility/priority/z-order/budget/compositor | `present_lod_hash` |

Root: `presentation_hash`.

### 1.2 Consumption of V1–V5 outputs
**Presentation Resolution Table (PRT):** V3 sockets/anchors/landmarks/camera-bounds/zones/mission state+dialogue; V4 state slots (health/ammo/stamina), event channels, AI alert, combat targets, prompts, aim input; V5 world state (faction control/weather/economy/map), emergent events; V1 UI sprites/fonts/VFX/SFX assets. References by hash; binds via `read`/`subscribe` only (I-PROJ). View params exogenous, not hashed. `pres_fp := {scene, mission, gameplay_hash, sim_hash, entity_refs?, asset_refs?}`.

### 1.3 Phase ordering
```
P1 Camera (camera_hash) → P2 Presentation Binding (binding_hash) → P3 UI/HUD (ui_hash)
  → P4 Feedback (feedback_hash) → P5 Accessibility (a11y_hash) → P6 LOD & Composition (present_lod_hash)
presentation_hash = H(camera, binding, ui, feedback, a11y, present_lod, pres_fp, presentation_determinism_contract, versions)
```
No downstream, no upstream write; I-PROJ + I-STRIP hold. Camera precedes UI/Feedback (screen-space projection needs the camera transform). Accessibility (P5) is a deterministic transform over P1–P4 specs.

### 1.4 Identity
```
presentation_hash = H("v6.presentation", camera_hash, binding_hash, ui_hash, feedback_hash, a11y_hash, present_lod_hash, pres_fp, presentation_determinism_contract_hash, versions)
```
**I-PRES-LOCAL**: one widget/camera/binding change touches only its path to `presentation_hash`; never a V1–V5 hash (I-STRIP).

---

## 2. Camera System (P1)

### 2.1 Purpose
Deterministic camera rigs + a **camera graph** (prioritized state machine with blended transitions); read-only over the world (I-PROJ).

### 2.2 Micro-types
`cam.follow/first_person/rail/tactical/cinematic/dialogue/free/spectator/fixed`, binding targets by entity socket / scene anchor / region.

### 2.3 Schema
```
camera_graph { cam_id; cameras: sorted set<camera_node>; transitions; default: ref; collision: policy; bindings: read/subscribe only }
camera_node { node_id; micro_type; params(quantized); priority:int; activation_condition }
cam_transition { from; to; kind: clamp_enum(cut|blend); curve: clamp_enum(linear|ease_in|ease_out|ease_in_out|custom); duration(quantized); priority_resolution: highest_priority_active }
```
Active camera = highest-priority active node, ties by canonical `node_id`; blends by quantized ease curve/duration. Reproducible given world state + camera-input trace.

### 2.4 Camera → gameplay/AI/mission/sim (read/subscribe only)
Follow reads player transform/socket + V4 aim (visualizes); shake subscribes to combat/feedback events (seeded `H(event_hash, sim_tick)`; perturbs only camera transform); cinematic/dialogue subscribe to mission events; strategic reads V5 fields. Missing target → disable node → fall through to `default` (never no camera).

### 2.5 Collision/occlusion
`collision_policy` (layers vs V4/V2 proxies, quantized pushout, transparency fades) — hashed; runtime pushout is a pure fn (contracted).

### 2.6 Hashing
```
camera_node_hash = H("v6.camnode", micro_type, canonical(params), priority, activation_condition, target refs)
camera_hash = H("v6.camera", sorted(camera_node_hash…), sorted(cam_transition records), default ref, collision_policy, cam_wiring_hash, pres_fp, degrade_decisions)
```

---

## 3. Presentation Binding Layer (P2)

### 3.1 Purpose
The read-only firewall; projects V3/V4/V5 state/events into presentation values (the only sanctioned read path).

### 3.2 Micro-types
`bind.pull` (poll a state slot), `bind.push` (subscribe to an event), `bind.derived` (quantized expression), `bind.spatial` (world pos → screen via active camera), `bind.collection` (iterate a list slot, canonical order).

### 3.3 Schema & display quantization
```
binding { binding_id; mode: clamp_enum(pull|push|derived|spatial|collection); source: {hash, slot|channel|socket, layer: v3|v4|v5} (read/subscribe ONLY); transform: ordered list<display_op> (clamp→map→quantize→format→unit); target: {presentation node ref, property}; refresh: clamp_enum(per_frame|on_change|on_event) }
```
Display quantization (distinct from sim quantization) → stable hashable display states. Direction fixed to read (I-PROJ, compile-enforced). Unresolved source → placeholder/default DEGRADE.

### 3.4 Hashing
```
binding_hash = H("v6.binding", sorted(binding records w/ canonical transform chains), pres_fp, degrade_decisions)
```

---

## 4. UI / HUD System (P3)

### 4.1 Purpose
Responsive UI/HUD layouts + widgets, fed by bindings (P2) and (for diegetic/screen-space) the camera (P1). Layout = pure fn of widget tree + exogenous view params.

### 4.2 Micro-types
`ui.health/stamina/shield/ammo/crosshair/interaction_prompt/objective_tracker/quest_log/compass/minimap/map/notification/toast/dialogue_box/subtitle/inventory_screen/menu/diegetic`, each binding read-only to V3/V4/V5.

### 4.3 Layout
```
ui_layout { root: widget_tree (canonical child order; z within layer by index); anchors: constraint-based (quantized); scaling: clamp_enum(constant_pixel|scale_with_screen|physical_size); safe_area; density: clamp_enum(full|minimal|off) }
```
Resolved pixels = pure fn of (tree, anchors, resolution, aspect, DPI, safe-area, scale mode); layout spec hashed; solver deterministic (fixed pass order, quantized). Diegetic placement world-space (V3 socket); screen appearance pure fn of camera.

### 4.4 Data-binding
Every dynamic property driven by a P2 binding; static props are hashed literals; unresolved binding → default/placeholder (never broken widget).

### 4.5 Hashing
```
widget_hash = H("v6.widget", micro_type, canonical(static props), sorted(binding refs), layout constraints)
ui_hash = H("v6.ui", sorted(widget_hash…), layout_root_order, scaling, safe_area_policy, diegetic attach refs, pres_fp, degrade_decisions)
```

---

## 5. Feedback Channels (P4)

### 5.1 Purpose
Event-driven read-only feedback making world events legible; zero gameplay effect (I-PROJ).

### 5.2 Micro-types
VFX (`vfx.particle/decal/post_process/world_effect`), SFX (`sfx.oneshot/cue/spatial/ambient_bed`), Screen-space indicators (`ind.damage_direction/offscreen_marker/waypoint/hitmarker/threat/objective_ping/interaction_highlight`).

### 5.3 Wiring & determinism
Triggers = `bind.push` subscriptions to V4/V5 events (fired at a sim tick) or `bind.pull` thresholds; trigger reproducible, playback delegated. Screen-space indicators = `bind.spatial` (world→screen via camera; off-screen clamp to quantized edge ring). Seeded variation `H(event_hash, sim_tick, index)`. No gameplay coupling.

### 5.4 Hashing
```
feedback_node_hash = H("v6.fbnode", category, micro_type, canonical(params), trigger binding ref, asset ref)
feedback_hash = H("v6.feedback", sorted(feedback_node_hash…), screen-space placement policy, seed_policy, pres_fp, degrade_decisions)
```

---

## 6. Accessibility & Readability (P5)

### 6.1 Purpose
Deterministic transforms over P1–P4 specs; `apply(profile, spec) → spec'`; first-class, and the safe direction of every degrade.

### 6.2 Micro-types
`a11y.subtitles/colorblind/text_scale/high_contrast/reduced_motion/photosensitivity/audio_visualization/input_display/readability`.

### 6.3 Profile model
Available profiles part of `a11y_hash`/`presentation_hash`; an applied profile yields a distinct `presentation_variant_hash`; profiles compose in a fixed canonical order (colorblind→contrast→scale→motion→photosensitivity).

### 6.4 Safety floor
`a11y.photosensitivity` + `a11y.reduced_motion` define a mandatory safe presentation (flash-limited, motion-reduced, high-contrast) — the target of the accessibility degrade ladder (§10.4); uncertain → degrade *toward* accessible.

### 6.5 Hashing
```
a11y_profile_hash = H("v6.a11yprofile", micro_type, canonical(params), transform_spec)
a11y_hash = H("v6.a11y", sorted(a11y_profile_hash…), composition order policy, safety floor spec, pres_fp, degrade_decisions)
presentation_variant_hash = H("v6.presvariant", presentation_hash, sorted(applied profile hashes))
```

---

## 7. Presentation LOD & Composition (P6)

### 7.1 What-to-show-when
Visibility states (always/contextual/transient/hidden); HUD density (full/minimal/off; cinematic auto-hide); diegetic distance LOD (quantized buckets, no hysteresis); VFX/SFX budget (cull by priority then canonical id); context gating.

### 7.2 Composition (z-order)
Fixed compositor: world render → world-space/diegetic UI → screen-space indicators → screen-space HUD → menus/overlays → accessibility overlays (topmost). Same-layer overlap → higher priority wins (offset/cull by priority→canonical id); critical info (health/objective/subtitles) reserved priority, never clutter-culled.

### 7.3 Hashing
```
present_lod_hash = H("v6.presentlod", sorted(visibility/state defs), density-mode policy, distance-bucket policy, budget policy, z-order layer order, real-estate arbitration policy, pres_fp, degrade_decisions)
```

---

## 8. Multi-Backend Export Rules

Emits declarative data + refs to standard presentation systems, never bespoke code. **Camera (most portable):** Cinemachine / CineCamera+Camera Manager / camera objects+markers / `UsdGeomCamera`; graph/transitions → native blends or portable metadata. **UI (least portable):** uGUI+UI Toolkit / UMG (Slate) / metadata-storyboard / custom schema metadata. **Feedback:** VFX Graph/Niagara/particles/Volume metadata; AudioSource/MetaSounds/speakers/custom; indicators → UI widgets or metadata. **Bindings/events:** UI Toolkit binding+UnityEvents / UMG bindings+dispatchers / drivers / USD relationships — read-only direction preserved. Identity sidecar adds `view_independent: true`. Backend GUIDs derive from content hashes.

---

## 9. Determinism, CAS, and Skip-Work

**9.1** `presentation_hash` folds six sub-hashes. **9.2** Pure leaf consumer above `sim_hash`; I-STRIP (headless `build_hash` omits `presentation_hash`, equals client for V1–V5). **9.3 Partial recompilation:** widget edit→ui_hash; camera blend→camera_hash; binding source→binding_hash + dependent ui/feedback; a11y profile→variant only; upstream world change→only referencing bindings/widgets; **resolution/aspect change → NO recompile** (view params runtime). **9.4 Determinism contract (pinned):** display quantization (fills/numbers/screen-pos `q_screen`/colors `q_color`), quantized ease curves/durations, camera shake/feedback RNG seeded `H(event_hash, sim_tick, index)`, canonical z-order + arbitration, quantized distance LOD, fixed a11y composition order; **exogenous (not hashed):** resolution/aspect/DPI/safe-area/frame-timing/camera-input. **9.5 Skip-work:** standard; V6 never invalidates a V1–V5 cache entry (I-STRIP).

---

## 10. Validation & Degrade-Never-Fail Model

**10.1** **Theorem (Total Presentation).** Any V5-passing world gets a valid V6 presentation — at minimum a working camera + clean readable screen — always viewable/backend-safe/read-only. No content-induced hard-fail; only compiler/registry bugs **plus a write-binding to a gameplay/sim hook (I-PROJ violation, caught statically)** are hard errors. **Never-no-camera** and **never-broken-UI** guarantees.

**10.2 Validation:** camera (reachable default, unambiguous selection, quantized transitions, targets resolve-or-disable); binding (every direction read/subscribe — **write-binding = hard error**; sources resolve-or-placeholder; type-consistent); UI (well-formed tree, solvable for any resolution, every dynamic prop bound/defaulted, critical widgets present); feedback (deterministic trigger, assets resolve-or-no-op, valid projection path); accessibility (pure composable transforms, realizable safety floor); LOD (total z-order, reserved critical priority, bounded budgets); integration (resolvable hooks, only published hooks, **no edge into V1–V5**).

**10.3** Diagnostics deterministic + hashed in.

**10.4 Degradation ladders:** Camera (disable node → graph default → scene establishing → engine default). UI (hide widget → placeholder value → priority-cull/reflow → minimal HUD/clean canvas). Feedback (silent no-op → cull by priority → no feedback). Accessibility (uncertain → **safety floor** → nearest safer transform → maximally readable/motion-reduced/flash-safe). Binding (placeholder → coerce; write-binding → **reject**).

**10.5** Every artifact valid for every backend, read-only (I-PROJ), detachable (I-STRIP). "Passes V1–V5" ⇒ viewable, deterministic, read-only presentation on Unity/Unreal/Blender/USD (≥ working camera + clean screen) that can be stripped without changing the world.

---

## Appendix A — V6 Identity Chain
```
…sim (V5) → pres_fp → camera │ binding │ ui │ feedback │ a11y │ present_lod → presentation_hash (read-only, strippable)
```

## Appendix C — Determinism mechanisms
1. Read-Only Projection (I-PROJ). 2. Headless Strippability (I-STRIP). 3. Pure projection (view params exogenous). 4. Resolution-independent spec. 5. Quantized display + ease curves. 6. Seeded cosmetic RNG. 7. Canonical z-order + quantized distance buckets. 8. Accessibility composes in fixed order; degrade toward the accessible floor.

**End of V6 Specification — Presentation, Camera, and UI/HUD Systems.**
