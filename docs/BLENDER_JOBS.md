# Blender Job Manager

External **asset-fulfilment** stage for the Unity pipeline. Where
`unity_cli.py` compiles an LLM concept into a Unity *project* (whose meshes are
runtime primitives + Kenney low-poly imports), the Blender Job Manager is the
asset factory that raises package quality: it turns the LLM story into Blender
jobs, runs Blender headless as a **generator + sanitizer**, and returns an asset
manifest you overlay into the generated project.

It follows the same philosophy as the headless Unity `verify` gate: locate the
external tool, run it in batch mode with a timeout, parse the report it drops to
disk, and **degrade gracefully** when the tool is missing — jobs resolve to a
`STUBBED` manifest instead of blocking the pipeline. The whole host-side surface
is unit-tested without Blender installed.

## The job spec (JSON contract)

```json
{
  "name": "golem_01",
  "asset_type": "character",
  "style": "realistic",
  "rig": true,
  "animations": ["walk", "run", "attack"],
  "lods": true,
  "textures": { "resolution": 2048, "bake": true,
                "material_brief": "cracked granite skin, glowing molten veins" },
  "geometry_nodes": "human_base",
  "output": { "fbx": "golem_01.fbx", "textures": "golem_01_textures/",
              "blend": "golem_01.blend" }
}
```

Every enum is clamped to a safe default (same as `src/unity_schema.py`), so a
hallucinated `asset_type`/`style`/animation never breaks the queue.
`textures.resolution` snaps to the nearest power-of-two in `[256, 4096]`.

## Two fulfilment routes

`route_for(asset_type, style)` (deterministic, no LLM) decides per asset:

| Condition | Route | Textures |
|-----------|-------|----------|
| stylized / low-poly / cartoon / pixel / retro | `blender` | procedural node material → baked maps |
| realistic **geometric** surface (terrain, building, foliage, prop) | `blender` | procedural (noise + triplanar) |
| realistic **authored** surface (character, creature, weapon, vehicle) | `diffusion` | LLM-prompted diffusion albedo → Blender bakes the rest |

This is the goal's branch: *"if Blender can procedurally produce textures, use
the LLM story; if not, a second pipeline goes to a diffusion model with a prompt
constructed from the LLM description."* The diffusion prompt is assembled from
`textures.material_brief` / `description` + a style fragment + an albedo guard
(see `src/blender/diffusion.py`). If the diffusion backend is unreachable, the
job falls back to procedural so Blender still textures it.

## Blender as a sanitizer

`src/blender/scripts/run_job.py` runs **inside** Blender and performs, each step
guarded so a missing add-on degrades rather than crashes:

1. Generate (geometry-nodes recipe / primitive base)
2. Mesh cleanup (remove doubles, recalc normals, delete loose)
3. UV unwrap (Smart UV Project)
4. Material (procedural nodes, or assign the diffusion albedo)
5. Texture bake (albedo + normal at the job resolution)
6. Auto-rig (Rigify metarig → generate → automatic weights)
7. Animation retargeting (named clips from the job's `animations`)
8. LOD generation (Decimate copies at decreasing ratios)
9. Export to Unity-friendly FBX (`-Z` forward, `Y` up, applied transforms)

It writes `<name>.manifest.json` next to the outputs, which the manager reads
into an `AssetManifest`.

## CLI

```bash
# Is Blender / a diffusion backend reachable?
python blender_cli.py doctor

# Run one hand-written job spec
python blender_cli.py run-spec jobs/golem_01.json

# Show the diffusion prompt a spec would send
python blender_cli.py prompt jobs/golem_01.json

# Derive a job queue straight from a saved Unity schema and fulfil it
python blender_cli.py from-schema output/NeonFugitive_schema.json
python blender_cli.py from-schema output/NeonFugitive_schema.json --dry-run
```

`from-schema` mines `schema.entities` ("6 rival cars", "a ring of buildings")
and character/enemy scripts into jobs, mapping the project's `visual_style` onto
each so the package stays visually coherent.

## Config (`config.yaml` → `blender:`)

```yaml
blender:
  blender_path: ""        # empty => auto-detect (PATH + standard installs)
  output_dir: output/blender
  timeout: 900            # seconds per job
  max_retries: 1          # extra attempts before a job is FAILED
  diffusion:
    backend: none         # none | auto1111 | comfyui
    base_url: http://127.0.0.1:7860
    timeout: 180
```

## Programmatic use

```python
from src.blender import BlenderJobManager, build_jobs_for_schema

mgr = BlenderJobManager(cfg["blender"])
mgr.submit_all(build_jobs_for_schema(schema, concept))
batch = mgr.run_all()          # → BatchManifest (done / stubbed / failed)
for asset in batch.assets:     # → list[AssetManifest]
    print(asset.fbx, asset.operations)
```

## Live MCP backend (run inside an open Blender)

Besides the `blender --background` subprocess backend, the manager can run a job
**inside the Blender the agent is connected to** over the official Blender Lab
MCP add-on. `src/blender/mcp_backend.py` emits a self-contained, non-destructive
bpy script per job (own `BJM_Assets` collection, spawned clear of the user's
scene, `use_selection=True` export); the manager runs it through an injected
executor and parses the returned `result` dict into an `AssetManifest`.

```python
mgr = BlenderJobManager(cfg["blender"], mcp_executor=run_in_blender)
mgr.live_steps = ["generate", "cleanup", "unwrap", "export"]   # optional subset
mgr.submit(job)
batch = mgr.run_all()        # each job runs in the live Blender
```

`mcp_executor(code: str) -> dict` takes a bpy script and returns what it assigned
to `result` (the host process can't call the MCP tool itself, so the agent
injects this). `mgr.build_live_code(job)` exposes the script for direct
execution.

Live steps (flag-derived via `steps_for`, or override with `mgr.live_steps`):
`generate → cleanup → unwrap → material → rig → animate → lod → export`.

* **rig** — Rigify: enable add-on → add human metarig (roughly fit to the mesh)
  → `rigify_generate` → parent mesh with automatic weights (Armature modifier).
* **animate** — one Action per requested clip, keyframed as placeholder
  locomotion on the rig (a bob + turn per `walk`/`run`/`attack`/…), exported as
  FBX takes (`bake_anim_use_all_actions`). Real motion-library retargeting
  replaces the placeholder later; the clip names + take structure are already
  what Unity's importer expects.

Verified end-to-end against the `golem_01` spec in a live Blender 5.1.2:
generate → mesh_cleanup → uv_unwrap → **rigify_autorig → anim_retarget** →
export_fbx — `rigged: true`, clips `[walk, run, attack]`, mesh bound via Armature
modifier (parent `golem_01_rig`), FBX grew 12 KB → 11.5 MB (armature + 3 takes),
the user's scene left untouched. Caveat: with a placeholder primitive standing in
for the mesh, Rigify's human skeleton isn't anatomically aligned — the mechanics
run correctly but good deformation needs the real geometry-nodes character mesh.

## Procedural recipe library

`src/blender/recipes.py` is a library of named procedural generators the LLM
selects via `asset_type` / `geometry_nodes`. Each is modifier + displacement
based (Subdivision, Displace with a procedural texture, Bevel, Solidify,
Decimate, Skin) — robust across Blender versions and genuinely detailed, the
practical stand-in for a shipped `.blend` of authored Geometry-Nodes groups.

| recipe | makes |
|--------|-------|
| `terrain_noise` | displaced ground plane |
| `rock_kit` | eroded boulder (ico-sphere + voronoi displace + decimate) |
| `crystal_cluster` | faceted crystal shards |
| `foliage_tree` | trunk + layered canopy |
| `building_kit` | beveled block + roof |
| `scatter_kit` | a ring of mixed props |
| `prop_kit` | rounded hard-surface prop (fallback) |
| `humanoid_base` | capsule biped placeholder (characters/creatures) |

Three LLM-facing knobs drive richness:

* **`detail` (1–5)** — scales subdivision level, displacement strength, instance
  counts. Geometry only.
* **`color` (hex)** — threaded from the Unity project palette via the entity's
  `color_role` (job_builder). Drives a palette procedural material
  (`_apply_palette_material`): Principled BSDF tinted to the colour + a
  Noise→ColorRamp two-tone breakup + style-driven roughness.
* **`soft_body`** — adds a Soft Body modifier (jiggle physics). **Caveat:** this
  is a Blender *simulation*; Unity won't run it. Shipping softbody to Unity
  needs a bake to vertex animation / **Alembic (.abc)** — a separate step (TODO).

What the recipes do **not** do: author image-map textures (procedural patterns
only — painted/realistic albedo still comes from the diffusion route or a bake),
or produce convincing characters (humanoid_base is a placeholder; real
characters come from diffusion + imported meshes).

Verified live in Blender 5.1.2: `rock_kit` (5,120 tris, red), `crystal_cluster`
(cyan), `terrain_noise` (green) — distinct procedural geometry + palette
materials, rendered in Material Preview, no warnings.

## Soft-body → Alembic, Unity overlay, diffusion albedo

* **Soft body + Alembic bake** — `soft_body=True` adds a Soft Body modifier and a
  keyframed Cast "jiggle", then `export_alembic` bakes the deforming mesh to a
  `.abc` point cache (`OutputSpec.alembic`). The manifest gains `alembic` +
  `unity_packages=["com.unity.formats.alembic"]`. *Caveat:* in headless Blender
  5.1 the soft-body solver only produced rigid motion in testing, so the baked
  deformation is driven by the deterministic Cast jiggle; true physics export is
  available for interactive use. Verified live: 219 KB `.abc`, width pulsing
  1.5→1.87→1.5.
* **Unity overlay** — `src/blender/unity_overlay.py` + `blender_cli.py
  overlay-unity <project>` copies a batch's FBX/textures/`.abc` into
  `Assets/BlenderAssets/<name>/` and merges required UPM packages into
  `Packages/manifest.json` (merge-don't-clobber). Verified into a real generated
  project (Alembic 2.3.3 added, 43 existing deps preserved).
* **Diffusion albedo (live)** — when the diffusion pre-pass leaves an albedo PNG
  (`job.meta["diffusion_albedo"]`), the live material wires it into Base Color
  (`material:diffusion`) instead of the procedural fallback. Verified live: image
  node `orc_albedo.png` applied.

## Fixed-seed determinism (Phase 1)

`src/blender/seeding.py` fans one master seed into reproducible, independent
sub-seeds (md5 hash, same pattern as `unity_generator._make_uuid`):

```
effective_seed(job)              # job.seed, else derived from job.name
  ├─ recipe_seed(job)    = derive_seed(eff, "recipe")    # procedural geometry RNG
  └─ diffusion_seed(job) = derive_seed(eff, "diffusion") # SD sampler seed
```

* `BlenderJob.seed` (int, optional — `None` ⇒ derived from name, so even un-seeded
  jobs are reproducible; pass a random master to vary).
* The live script pins `random.seed(SEED)` and passes `SEED` into the recipe;
  `crystal_cluster`/`scatter_kit` use it instead of ad-hoc RNG.
* `build_texture_request` defaults its diffusion seed to `diffusion_seed(job)`.
* `build_jobs_for_schema(..., master_seed=N)` stamps each job
  `derive_seed(N, job.name)` — and defaults the master from the Unity theme seed
  in `schema.meta` so the whole prompt→schema→assets chain is reproducible.
* Provenance: `AssetManifest.seed` records the seed that made each asset.

**Verified live:** two same-seed `crystal_cluster` runs produced byte-identical
geometry (vertex-hash `0cb9a71f…`), a different seed produced a different hash.
Caveats: compare via mesh/vertex hash, not file bytes (FBX/Alembic embed
timestamps); physics + LLM sampling are reproducible only on the same toolchain.

**Phase 2 (LLM seed) — done:** `LMStudioClient(seed=…)` adds `seed` to the
chat-completions payload (LM Studio + Ollama both honour it). Plumbed via
`lm_studio.seed` / `ollama.seed` config and `make_chat_client`; both pipelines'
`run(seed=…)` set `self.llm.seed`, so one `--seed` makes the whole
prompt→schema→assets chain reproducible (the theme RNG already used that seed).
Unit-tested (`tests/test_lm_studio_client.py::TestSeed`) and **verified live**
through the real `chat()` path against the pipeline model `qwen3.5-9b` at
temperature 0.85: two seed-1234 completions were byte-identical (hash
`f4745dbc…`, a full vivid sentence) while a seed-4321 run produced a different
sentence — proving the seed, not low temperature, pins the output.

## Export determinism — timestamp normalization (Phase 3)

Identical geometry used to re-export "dirty" (spurious git diff / Unity reimport)
because the exporters embed wall-clock timestamps. Measured against real Blender
5.1 output, those are the *only* non-deterministic bytes:

* **Alembic** `_ai_DateWritten=Mon Jun 15 15:43:16 2026` (C `asctime`, 24 bytes)
* **FBX** `CreationTime` `YYYY-MM-DD HH:MM:SS:mmm` (23 bytes — and Blender's
  "epoch" value is *timezone-shifted*, so it varies by machine).

`src/blender/normalize.py` canonicalizes both with **length-preserving** in-place
byte replacement (so Ogawa/FBX offset tables stay valid) — to epoch
`Thu Jan  1 00:00:00 1970` / `1970-01-01 00:00:00:000`. Idempotent (already-canonical
files are untouched). Runs automatically in the manager after every export
(config `blender.normalize_exports: true`), adds `normalize_fbx`/`normalize_alembic`
to the manifest operations, and is exposed as `blender_cli.py normalize <file|dir>`.

**Verified:** two simulated exports of one geometry differing only in write-date
had different md5s; after normalize both were byte-identical (`283979d6…`, same
length). Tests in `tests/test_blender_jobs.py` (length-preserving, two-export
byte-identity, dispatch, disable flag).

## Backend parity (Micro-Generator Phase 1)

Both backends now run the **same recipe-emitted script** from
`mcp_backend.build_job_code` — identical geometry recipes, palette materials, and
step pipeline — so headless and live output match:

* **live MCP** — `execute_blender_code` returns the script's `result`.
* **subprocess** — `build_job_code(..., write_manifest=True)` appends a tail that
  writes `<name>.manifest.json`; `blender --background --python <name>.run.py`
  leaves it on disk. The manifest tail is the *only* difference between the two
  (proven by test). The old primitive `scripts/run_job.py` is retired.

Each run reports a backend-agnostic **`geo_hash`** (sorted rounded vertex
fingerprint) in the manifest — also the future content-addressed cache key.

**Verified:** the same seed-7 crystal produced `geo_hash 0cb9a71f…` on **both**
the live MCP backend and a real `blender --background` subprocess run — same seed,
different backend, byte-identical geometry. Tests: parity-by-tail + a mocked
subprocess run driving the recipe path.

## Deterministic artifact cache (Micro-Generator Phase 2)

Because the pipeline is reproducible — one seed → a fixed `geo_hash`, exports
normalized to byte-stability — re-running Blender for inputs we've already built is
pure waste. `src/blender/cache.py` implements this as **two layers**, deepest
first.

### Layer 1 — the geometry substrate (`GeoStore`, `<cache>/geo/`)

Keyed on *geometry identity*, not jobs. A content-addressed blob store
(`blobs/<content_hash>`) holds every artifact's bytes exactly once — globally
deduped across all jobs, projects, and sessions. Every result is indexed under its
`geo_hash` (`index/<geo_hash>.json`), grouping all of a mesh's material/colour
**variants** together; each variant references the shared content-hashed blobs:

```
geo/blobs/<md5-of-bytes>           # raw FBX/blend/abc/texture bytes, stored once
geo/index/<geo_hash>.json          # { geo_hash, tris, variants: { <fingerprint>:
                                   #     { manifest, blobs: {fbx,blend,alembic,textures} } } }
```

This is the universal layer later phases (asset packs, cross-project reuse) build
on: given a `geo_hash` you can fetch the mesh and see which jobs produced it,
independent of who asked for it.

### Layer 2 — the skip-work fingerprint cache (`BlenderCache`)

Sits on top and is what the job manager talks to. It maps an **input fingerprint**
→ a `geo_hash` + variant in the substrate, so it indexes *into* layer 1 rather than
keeping its own byte copy.

* **Input fingerprint (the key).** `fingerprint(job, steps)` is a sha256 over
  everything that determines the output: the geometry-affecting job fields
  (`asset_type`, `style`, `detail`, `color`, `geometry_nodes`, `soft_body`, `rig`,
  `animations`, `lods`, `textures`), the resolved `effective_seed`/`recipe_seed`
  (this is how a name-derived seed still moves the key), the chosen `steps`, **and
  an md5 of the recipe + emitted-script source** so editing a recipe correctly
  busts the cache. `job.name`/`meta`/output paths are excluded — they don't change
  the mesh.
* **Hit → skip everything.** The lookup happens *before* the diffusion pre-pass, so
  an identical job restores its stored FBX/blend/Alembic/textures (pulled from the
  substrate) into the job's output dir and returns a `DONE` result with a
  `cache_hit` op — no diffusion, no Blender. (The diffusion albedo is deterministic
  from prompt + seed, both folded into the key, so short-circuiting it is safe.)
* **Store** runs only for real geometry (never stubs): it files the artifacts into
  the substrate under the manifest's `geo_hash` (falling back to the fingerprint
  when a backend reports no `geo_hash`), then writes the fingerprint pointer.
  Default root `<output_dir>/_cache/` (`blender.cache_dir` to override;
  `blender.cache: false` to disable — then every lookup misses and nothing is
  stored). All cache I/O is best-effort: any failure degrades to a normal run.
* **CLI.** `blender_cli.py cache` shows both layers (fingerprints; geo_hashes /
  variants / blobs / size); `--clear` wipes both.

**Verified:** (1) a second identical job on a fresh manager sharing the cache dir
is a hit — the Blender executor is not invoked again, and the restored manifest
carries the original `geo_hash`; (2) two *different* jobs (differing only by colour
→ different fingerprints) that yield the same mesh are filed under one `geo_hash`
with identical bytes stored as a single shared blob, while each fingerprint still
resolves and restores independently. Tests: fingerprint stability/sensitivity,
store→lookup→restore round-trip, end-to-end hit (executor call-count stays 1),
substrate variant-grouping + blob dedup, and the disabled path (no `_cache/`).

## Geometry reuse + rig router (Micro-Generator Phase 3)

Two material/colour variants of the same asset are *different* full fingerprints
(so the skip-work layer misses) but the **same mesh**. To share the expensive
geometry build across them, the geometry stage is decoupled from rigging.

**Pre-rig mesh = the reusable unit.** A cold build saves a mesh-only `.blend`
(clean verts + UVs, **no armature, no material**) right after unwrap, reported as
`manifest.mesh_blend`. It's content-addressed in the substrate's **mesh layer**
(`geo/mesh/<geo-fp>.json` → a shared blob), keyed by a narrower **geometry
fingerprint**:

* `geo_fingerprint(job)` folds in only what shapes the base mesh — recipe source +
  `recipe_seed` + `detail` + `asset_type`/`geometry_nodes` + a geometry-template
  version + **the Blender version** (a `.blend` is version-coupled, so an editor
  upgrade misses and rebuilds). It EXCLUDES colour, material, textures, rig, and
  animations, so every variant maps to one cached mesh.

**Three tiers, over one substrate** (`_run_one`): exact-result hit → skip
everything ▸ skip-work miss but **geo-fp hit** → restore the mesh, append it, run
only the material/rig/export delta (the geometry steps are trimmed from the run) ▸
cold → full build, then populate both the mesh layer and the skip-work entry. The
skip-work fingerprint is computed on the *canonical (full)* step list, so a
cold run and a geometry-reused run of the same job still share that top layer.

**Rig router (`rigging.py`).** Rigging is a deterministic per-job stage (the same
shape as `route_for`) that maps a job to a **versioned rig micro**:

* **Template classes** (authored armatures): `humanoid · quadruped · car · rifle ·
  door · crate`, each a `RigTemplate<Class>V1`.
* **Auto family** (deterministic, code-built): `RigAutoStaticV1` (no skeleton),
  `RigAutoMechanicalV1` (procedural root+body bones — vehicles/doors/mechanical),
  `RigAutoHumanoidV1` (Rigify metarig fit), `RigAutoRagdollV1`.
* `rig_strategy(job, template_root)` routes by asset_type/geometry_nodes, honours
  an explicit `meta.rig_kind="ragdoll"`, and **falls back** template→auto when no
  authored armature ships (the library isn't shipped yet, so everything falls back
  today — same graceful-degradation rule as the rest of the package). The chosen
  micro's versioned id is folded into the *result* fingerprint (so template↔auto or
  a version bump busts the cache), and `needs_armature` decides whether the manager
  emits the rig step at all (a `rig=True` crate → `RigAutoStaticV1` → no skeleton).

**Status / verification.** Host side is unit-tested: router + fallback, geo-fp
(excludes variant axes, tracks detail/Blender version), mesh-layer round-trip,
rig-id in the fingerprint, and the end-to-end colour-variant reuse (cold build
caches the mesh; the variant misses skip-work but reuses geometry and runs the
delta). All five emitted-script variants (cold/reuse/mechanical/static/softbody)
are compile-checked. The **live append-and-rebind path** (`bpy.data.libraries.load`
of the cached `.blend`, mechanical/static rig micros) still needs verification
against a connected Blender, as in prior phases. The authored rig-template armature
library is the next deliverable (templates currently fall back to auto).

## Status

- Host-side service (schema, routing, queue, retry, manifest, diffusion prompt
  construction, graceful stub) is complete and covered by
  `tests/test_blender_jobs.py` (19 tests).
- The in-Blender `run_job.py` sanitizer pipeline is written but only exercised
  when a real Blender is installed; generation currently uses representative
  primitives as geometry-nodes recipe stand-ins. Next step: ship a `.blend`
  asset library of named geometry-nodes generators (`human_base`, `vehicle_base`,
  …) and wire real Rigify retargeting clips.
- Not yet wired: auto-overlay of produced FBX/textures into the generated Unity
  project's `Assets/` (the manifest already carries the paths for it).
