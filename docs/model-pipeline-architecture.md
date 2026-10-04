# Model Pipeline Architecture — Design Note

Status: draft · Scope: 3D/2D asset generation feeding the deterministic resolver/compiler

Extends the existing nondeterministic-source → frozen → deterministic-resolver pattern
(established for the audio pipe) to 3D/2D model assets.

---

## Problem

Asking an LLM to *procedurally author* organic geometry (e.g. Blender Python for a
"red high heel shoe") produces primitive soup — plausible structure (sole primitive,
heel primitive) with no authentic surface, because the model reasons about geometry
blind (no viewport feedback, no depth). This is the geometric form of the
spec-escalation failure: the generator fakes the scaffold of the artifact without
executing the artifact.

## Core principle

**The LLM does not author geometry.** Same split already used in the audio pipe:
the nondeterministic *instrument* produces the artifact; the LLM only handles
naming, theming, and parameter selection (MAP-Elites framing).

For organic geometry the "instrument" is a **generative mesh model**, not a script.

## Decision 1 — Route by asset class

| Asset class | Method | Why |
|---|---|---|
| Mechanical / hard-surface (brackets, machinable parts, feature-describable props) | Parametric codegen via **CADQuery** or **build123d** (FreeCAD host) | Geometry is feature-describable; LLM emits real B-rep features, stays deterministic |
| Organic / designed (shoe, character, creature) | Generative mesh → retopo → rig | Surface authenticity lives in spatial info a script can't access |

Never use a parametric script for organic shapes. Never use generative mesh for
parts that need exact engineered dimensions.

## Decision 2 — Image-to-3D is the organic path, NOT image-to-parametric

- Single-image-to-3D diffusion infers silhouette/shading/depth and hallucinates the
  occluded volume into a **mesh**. This is the "predicted 3D space."
- Output is a **mesh, not parameters.** Image → parametric CAD does not exist for
  organic shapes (STEP/solid-body reconstruction is a manual reverse-engineering pass).
- Riggability comes from **quad topology**, not from parametric conversion:
  `mesh → clean quads → bones`.

## Decision 3 — Backend tool routing

API-backed backends bypass the local 8GB VRAM limit. Already wired into the Blender MCP.

- **Rodin / Hyper3D Gen-2** (`generate_hyper3d_model_via_images`) — default for
  riggable assets. Clean quad topology, enforces T/A-pose, multi-image fusion. API-backed.
- **Tripo v3.1 (quad mode)** — strong alternative for anything bound for rigging.
- **Hunyuan3D** (`generate_hunyuan3d_model`) — open/free, but self-host wants a beefy
  GPU; 8GB is marginal. Keep local for the LLM tier; offload mesh-gen to API.

Local GPU stays the LLM tier. Mesh generation offloads to API backends.

## Decision 4 — Determinism boundary (the integration contract)

Generative mesh-gen is **nondeterministic** — it sits on the **same side of the frozen
line as audio**, NOT inside the deterministic compiler.

- Do **not** try to make generation reproducible. Freeze the **output**.
- Pin the seed. Capture the generated **GLB as the immutable artifact**.
- The hash-emitting determinism test runs over the **frozen GLB**, never over the
  generation call.

```
[ nondeterministic ]            | frozen |        [ deterministic resolver ]
 LLM concept/theme/params       |        |   ingest GLB
 generative mesh (seed pinned) ─┼─ GLB ──┼─> validate (manifold/scale/topology + hash)
                                |        |   retopo → quad
                                |        |   auto-rig (Rodin skeleton / Mixamo / Rigify)
                                |        |   wire into project
```

## Pipeline stages (deterministic half)

1. Ingest frozen GLB.
2. Validate: manifold check, scale check, topology check, emit hash gate.
3. Retopologize to quad.
4. Auto-rig: Rodin skeleton, or Mixamo/Rigify for humanoids.
5. Wire into project.

## Open work

- Define the GLB-freezing contract (seed + artifact capture) and the validation/hash gate
  so model assets drop into the resolver the same way audio artifacts do.
- Apply the "one rig class at a time, determinism-and-mutation test per class" discipline
  to rig archetypes (same generalization test pattern as the vstack fork, applied to
  rigs instead of primitives).

## Sketchfab (found, not generated)

Same frozen-artifact contract as Decision 4, applied to a **discovered** mesh
instead of a **generated** one. The "instrument" is a live Sketchfab search +
download rather than a diffusion/mesh-gen model, but the determinism boundary
is identical: search ranking, third-party availability, and rate limits make
the fetch itself nondeterministic, so it never runs inside the deterministic
resolver — it runs once, its result is **frozen** to
`lookups/unity/sketchfab_catalog.json`, and the resolver
(`src/unity_entity_asset_resolver.py`) only ever reads that frozen file.

```
[ nondeterministic ]                       | frozen |     [ deterministic resolver ]
 Sketchfab search + license filter          |        |  read sketchfab_catalog.json —
 + download + Blender headless GLB→FBX      ┼─ FBX + ┼─  pure lookup, no network, no LLM,
 (network, third-party ranking)             |  .json |  exactly like asset_catalog.json
```

Two differences from the generative-mesh case worth calling out:

- **No retopo/rig step.** Sketchfab meshes are already artist-authored (unlike
  a diffusion mesh, which needs cleanup to be usable) — the Blender conversion
  (`src/blender/sketchfab_convert.py`) is a pure format-shift, import glTF →
  export FBX, nothing else. This also keeps the CC-BY-ND "derivative work"
  question as narrow as possible: format conversion alone, no geometry edits.
- **The instrument can run from two places.** The safe, always-deterministic
  path is a manual pre-fetch (`python main.py sketchfab-fetch`), same shape as
  `unity-catalog` — populate the frozen cache ahead of time, the resolver never
  touches the network. `sketchfab.auto_fetch: true` remains available to
  explicit asset-management workflows and direct resolver integrations, where
  the caller consciously accepts a live cache miss. The production
  `UnityPipeline` and `unity-validate` force it off after the schema boundary:
  Steps 4-7 are network-free, and only the explicitly enabled Step-3.5
  collection may add frozen models before that boundary. `auto_fetch: false`
  (the shipped default) keeps every ordinary resolver invocation a pure lookup.

### Style coherence — one marketplace, many unrelated artists

Sketchfab is a marketplace of independent freelancers, not a curated pack —
searching each entity in isolation ("knight", "sword", "castle wall") each
independently lands on whichever artist's upload happens to rank highest for
that one word, with no relationship to any other entity's pick. Left alone,
a project reads as "constructed chaos": a realistic PBR knight next to a
cartoon sword next to a toy-store wall. Two deliberately separate, composable
mitigations, both empirically tuned against the live API (2026-07):

1. **`src/unity_sketchfab_translator.py`** — a concept-translator pass (same
   shape and pipeline placement as `src/audio/concept_translator.py`, run
   while the project LLM is still loaded) that derives ONE shared style tag
   for the whole project plus a plain query per entity, filling
   `EntitySpec.sketchfab_query`. **Sketchfab's search is literal keyword
   match, not semantic** — verified live that a descriptive multi-word style
   sentence ("grim dark medieval siege setting, wet stone and rusted iron,
   muted palette...") returns **zero** results, while a 1-2 word style label
   ("medieval", "low poly", "stylized") plus a 1-3 word concrete noun phrase
   reliably returns 15-25+ results. The prompt hard-caps style+query at 4
   words total for exactly this reason — it's a search-engine constraint, not
   a taste preference.
2. **Author-affinity biasing** (`SketchfabClient.search(..., user=...)`,
   threaded through `fetch_and_freeze(..., preferred_author=...)`) — once
   Tier S resolves ANY entity to a real Sketchfab pick (cache hit or live
   fetch), that model's author (`username`, not `displayName`) becomes the
   project's "preferred author" for every later entity in the same resolve
   call: their own catalog is searched FIRST, falling back to the open
   marketplace only if nothing of theirs clears `min_score`. Set once, never
   overwritten — no drift toward whichever artist a later fallback happens to
   land on. Measured hit rate is real but modest: of ~17 authors matching
   "medieval knight", exactly one also had a "medieval soldier" upload, and
   none had a castle wall or barrel — closely related object types (two
   characters) overlap far more often than unrelated ones (character vs.
   environment vs. prop). The mechanism costs nothing when it misses (clean
   fallthrough to the global search, same as any other Tier S miss).

### Visual QC — a same-word coincidence isn't a semantic match

Keyword-overlap scoring (`min_match_score`, both mitigations above) has one
structural blind spot neither closes: it cannot tell a CORRECT object from
one that merely shares a tag. Live regression — `LeadSkydiverJumpsuit`
(`model_hint: "lead skydiver jumpsuit helmet"`) resolved to a baseball
sports-asset, because the query search's own marketplace ranking for the
surviving bare word "helmet" is dominated by far more common upload
categories, and every candidate scored identically on that one shared token.
Text scoring can only ever pick the best of the pool Sketchfab hands back; it
has no way to notice the pool itself is off-target.

`src/unity_sketchfab_vision.py` closes this the only way text can't: it
shows the top keyword-ranked candidates' own thumbnails to the Pipeline LLM
and asks whether the pick actually looks like the entity, before
`fetch_and_freeze`'s download + Blender-convert loop spends real time on a
keyword-plausible but visually-wrong one. Wired as an optional
`vision_select_fn` hook on `fetch_and_freeze` (same shape as `score_fn`) that
only ever REORDERS candidates `min_match_score` already accepted — it can
promote a lower-ranked keyword match to the front, never admit one the
keyword pass rejected outright.

Two conditions gate it, both required: `sketchfab.vision_select: true` (off
by default) AND a live `llm` reaching `resolve_entity_assets` — threaded
from `UnityPipeline.run()`'s `self.llm` through
`UnityProjectGenerator.write()`/`generate()`/`_resolve_all_models()`, all
`None`-defaulted so every caller before this parameter existed is
unaffected. `unity-validate`'s no-LLM replay never passes one, so a saved
schema recompiles exactly as it did before this feature existed. The model
itself must be a VISION-capable load — a base GGUF paired with its own
`mmproj` file (LM Studio auto-pairs one dropped in the same model folder,
e.g. `qwen3.5-9b` + a co-located `mmproj-Qwen3.5-9B-*.gguf`); a text-only
load is simply never asked, since nothing about this feature can detect
vision capability up front — it degrades to "no opinion" the same way a
`None` pick, an unreachable thumbnail, or any network/parse failure already
does (`unity_sketchfab_vision.py`'s own docstring covers every fallback
path). `LMStudioClient.chat(images=[...])` sends the OpenAI multimodal
`image_url` content-part shape; thumbnails are fetched through
`SketchfabClient.fetch_image_bytes` (the same cold-Chromium transport as
everything else in that client, not a plain HTTP GET — a thumbnail CDN host
has no proven reason to be less WAF-finicky than the API host is).
