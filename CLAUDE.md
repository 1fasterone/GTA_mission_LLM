# Core Identity

This repository is a domain-agnostic, multi-backend compiler for procedural
game content — a game-o-matic. A swappable LLM provides the imagination;
deterministic Python provides the truth. All post-schema steps are
deterministic and must remain LLM-free.

Backends: Unity and GTA5.

# The Two Roles (Read This First)

Two different intelligences touch this system. Conflating them is how evals
dissolve and determinism erodes.

**Pipeline LLM** — the swappable model (currently qwen3.5-9b via LM Studio,
configured in config.yaml) that runs INSIDE the pipeline. It executes Steps
2-3 only: it writes briefs and fills schemas. It is a component and can be
replaced without touching the compiler.

**Compiler Engineer** — Claude in Cowork/chat sessions, building and
maintaining the system from OUTSIDE the pipeline. The engineer writes Python,
tests, docs, and PRs. The engineer NEVER does the Pipeline LLM's job.

**Primary engineer constraint: never fill the schema yourself.** If a session
hand-authors or pre-formats pipeline input or intermediate artifacts, the
pipeline was not tested — the engineer was. See Eval Discipline.

# Pipeline Contract

The Pipeline LLM participates only in Steps 2-3.

1. Prompt
2. Concept — Pipeline LLM writes a short, clear English brief
3. Schema — Pipeline LLM fills the strict Pydantic schema (IR). Once the
   schema is valid, the Pipeline LLM stops.
4. Resolver — deterministic ID lookup (Python)
5. Fixer — deterministic consistency + naming
6. Generator — deterministic backend codegen
7. Playability — deterministic structural evaluation (unity_playability.py)

Never add LLM calls to Steps 4-7. Never introduce nondeterminism.

# Eval Discipline

- **Natural-language briefs are held-out test inputs. Structured briefs are
  production inputs.** The two must never blur.
- Any session that transforms a brief before the pipeline sees it MUST
  declare the transformation, in the session and in the PR. An undeclared
  transformation dissolves the test. Canonical incident: a model pre-formatted
  an eval brief into parser format — the run measured the model, not the
  pipeline.
- When judging a pipeline run, verify from logs and intermediate artifacts
  WHOSE hand wrote each artifact. If schema content was authored outside
  Step 3, the run proves nothing about Step 3.
- The engineer may write fixtures and tests that contain schemas. Those are
  labeled test data under tests/, never passed off as pipeline output.

# Atoms (Required Mental Model)

Atoms are the smallest deterministic actions the compiler can execute:
primitive verbs, engine-valid, schema-valid, RAG-grounded, indivisible (no
branching, loops, or multi-step logic), safe building blocks for
micro-generators and scenes.

Atoms live in atoms/ (definitions + registry) and micro/ (compositions).

Both roles must: never invent new atom types, only use atoms defined in the
schema, keep atom outputs deterministic, keep atom sequences valid for the
Unity/GTA backends. Atoms are the foundation of all microgens.

# How the Engineer Should Behave

- Treat the repo as a compiler, not a chat chain
- Never modify code unless explicitly asked
- Prefer deterministic, reproducible transformations
- Never invent new schema fields or change schema structure; the Pipeline
  LLM's outputs must stay concise, unambiguous schema fills
- Use Artifacts for diagrams, schema visualizations, and file edits
- When editing files, produce full-file replacements, not patches
- Repair deterministically where ground truth exists; record where it does
  not; never raise in a way that discards an otherwise valid schema
- Abstain loudly, never silently default: a wrong silent fallback is harder
  to diagnose than a loud refusal with an explicit remedy

# Where Things Live (For Cowork Navigation)

Core
- main.py — all commands; new features must be added here
- config.yaml — model, RAG, Blender, Unity paths

Backends
- GTA5 — pipeline.py, mission_schema.py, code_generator.py, resolver.py
- Unity — unity_pipeline.py, unity_schema.py, unity_generator.py,
  unity_resolver.py, templates/unity/

Spatial + QA (Unity)
- unity_layout_archetype.py — derives (motion_axis, medium) axes from the
  schema and selects one of four spatial primitives: ArenaPlane (planar),
  VerticalCorridor (descent), BoundedVolume (fly/swim), VerticalSurface
  (ascent). Abstains loudly via [layout.strict] errors with explicit
  remedies — never silently defaults. docs/LAYOUT_ARCHETYPES.md is the
  reference.
- unity_layout.py — compute_layout() dispatches to the chosen primitive
  (_layout_arena_plane, _layout_vertical_corridor, _layout_bounded_volume,
  _layout_vertical_surface) and packs within it.
- unity_spatial_facts.py — deterministic sidecar extracting the brief's
  numeric distances before schema-fill; consumed tiered (explicit schema
  field, then sidecar match, then named default with warning).
- unity_sceneinput.py / unity_scenebind.py — hand-authored scene layouts from
  the Scene Compiler (see Authored Scenes below); docs/SCENE_COMPILER.md
- unity_playability.py — Step 7 evaluator. Deterministic structural checks
  over the final schema answering "is it a game" (avatar, controller,
  objective, reachable content, brief-entity coverage). Prints a scorecard,
  writes playability.txt, gated by playability.gate in config.yaml.
  CLI: python main.py unity-playability <schema.json> [--strict]
- unity_objective_targets.py — Step 5 pass filling every blank
  `MissionObjective.target_ref` from the objective's own description, scored
  against entities the schema already declares (shared words, +2 where a
  locative preposition marks a destination and the entity is a place, +1 where
  the side matches what the objective TYPE needs). A tie ABSTAINS, naming both
  candidates. Runs last of the Step 5 content passes, because every pass above
  can still add an entity. Blank refs are why SEAM 8a, `commands` and
  `requires` all abstain at once.

Rules + mechanic (UI-first games)
- micro/rules.py — the briefing, the step the player is on, and the hint bar
  that names the next action. Composed once per PROJECT, one progression
  spanning EVERY mission (`steps_from_missions`) — SEAM 8a binds objectives
  from all of them, so a step list stopping at the first told the player about
  a fraction of what was live. Owns what the player is TOLD.
- micro/board.py — the domain model and the legal-move logic behind it: every
  piece, every zone, where each piece is, and whether a move is legal
  (`TryPlace`, which refuses by name). Drag and a quote-aware command parser
  reach the same move core, so the two surfaces cannot disagree. Adopts the
  plates EntitySpawner built; never spawns its own. Composed for
  locomotion='none' only. Its events are what SEAM 8a binds a mission
  objective to. `board_domain_for_missions` derives the whole domain
  deterministically from two sources — the MISSIONS carry the rules (`accepts`
  from piece/place name affinity, `commands` from each objective's opening
  imperative, `requires` from the objective order), the BRIEF carries the names
  (`items` per piece, `command_prefix`) — and abstains loudly rather than
  half-restricting a board. It reads EVERY mission, in step with
  micro/rules.py, so what the board enforces and what the player is shown stay
  two readings of one list. An objective about a READOUT binds to
  OnCommand (a drag into one is refused); everything else keeps its piece/zone
  event. docs/PLAYABLE_GAME_GAP.md is the reference.

Always reference these paths when generating or modifying files. See also
docs/model-pipeline-architecture.md.

# Model Pack v1 (Unity)

Preserve this chain:

1. default_models.json — catalog (explicit kinds)
2. asset_catalog.json — full-library catalog (generated:
   python main.py unity-catalog; scans config assets.roots, merges
   kenney_catalog.json curated overrides)
3. unity_model_resolver.py — resolves to Models/Defaults/<kind>/
4. unity_entity_asset_resolver.py — per-entity tiering: Tier A authored
   (a Scene Compiler layout, see Authored Scenes below), then explicit kind,
   then catalog semantic match (name + kind + model_hint, deterministic
   variety rotation), then strict hard-fail (assets.strict) / logged prop
   fallback
5. unity_generator.py (_resolve_all_models — single source of truth for
   generate() AND write()) — copies binaries + Kenney Textures/ + writes .meta
6. ModelSwap.cs.j2 / EntitySpawner.cs.j2 — runtime swap

Wiring uses entity_model_paths. Never break this chain.

# Authored Scenes (Unity)

A hand-arranged Scene Compiler layout (the browser tool in
Documents/Blender/) may supply BOTH the models and their exact transforms.
The author's hand outranks every generated choice about assets and placement.
It reaches wiring in exactly one place, and only on request: a ROLE PREFIX on
the model's filename (`player_monobike.fbx`, `enemy_drone.fbx`,
`pickup_scrap.fbx` — unity_sceneinput.ROLE_PREFIXES) declares what the object
IS, binds ahead of every keyword signal, vetoes a conflicting bind, and
becomes the bound entity's `kind`/`tag`. Scripts and micros stay untouched.

- unity_sceneinput.py — layout JSON -> canonical scene file: three.js Y-up ->
  Unity (x, y, -z) and yaw -> -yaw, one group per (file, scale), GLB -> FBX
  via headless Blender (cached, unit-guess rescale disabled). Fails loudly on
  a missing model; warns loudly when the layout predates `base_scale` (the
  viewer's hidden 0.01 centimeter correction).
- unity_scenebind.py — Step 5 binding: role prefix first, then deterministic
  keyword match of each authored group to one entity, then bakes
  instance_positions/instance_yaws/count/scale. Entities the layout does not
  cover are NAMED in the log (schema.meta.uncovered_entities) rather than
  silently falling through to the catalog. A multi-instance group also spills one instance each to
  still-unbound SINGLETON entities that scored for it (a starting grid feeds
  the player and each named rival; the player must keep count==1 or the
  generator's player-merge silently stops firing). Unmatched groups are
  injected as entities; entities the layout does not cover keep resolving
  through the normal catalog tiers. Stamps schema.meta.authored_models (the
  Tier A table) + authored_scene.
- CLI: python main.py unity-scene <layout.json> [-o scene.json]
       python main.py unity-create "<prompt>" --scene <layout-or-scene.json>
- Runs AFTER normalize_paths and BEFORE compute_layout, which then treats
  authored groups as preplaced. docs/SCENE_COMPILER.md is the reference.

# Procedural 3D Models

The engineer may generate {base, params} mappings, Blender scripts (bmesh
builders), and FBX export instructions — but must not modify Blender
internals unless asked.

# Conventions to Enforce

- Steps 4-7 remain deterministic

- Config-driven behavior (config.yaml)
- Unity canonical script names must not change
- Never break template references
- Atoms remain single-action, deterministic, engine-valid

# Required Workflow Rule

After every new feature, remind the user:

"Open a GitHub Pull Request immediately. No feature is considered complete
until it is PR'd."

This is part of the workflow contract.

# Artifacts

Use Artifacts for diagrams (Mermaid/SVG), file rewrites, schema
visualizations, and code generation previews. Artifacts must be
self-contained and runnable when possible. Prefer Mermaid for architecture
diagrams and full-file rewrites for code edits.

# Output Style

Be concise. Use lists, tables, and diagrams. Avoid speculation. Never
hallucinate schema fields. Always follow the compiler mental model, with
atoms as the primitive layer.
