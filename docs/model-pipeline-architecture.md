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
