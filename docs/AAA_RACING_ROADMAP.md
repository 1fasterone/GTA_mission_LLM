# AAA Motorcycle Racing Roadmap

## Goal

Repeatedly generate a playable 3D motorcycle racing scene whose deterministic
rendering target is the supplied racing reference: a low trailing chase view,
bright outdoor lighting, readable asphalt and red/white curbing, layered safety
barriers, dense grandstands and vegetation, and distant atmospheric scenery.

This is a visual target, not a request to copy the reference image, its HUD,
sponsors, rider liveries, or metadata into the generated project.

## Existing foundation

The repository already provides:

- deterministic racing scaffolding in `src/unity_racetrack.py`;
- `drive` and `vehicle_bike` controller routing;
- authored model and transform binding through the Scene Compiler;
- an unconditional AAA atmosphere pass for generated scenes;
- deterministic layout, asset resolution, playability, compile, and play-mode
  validation seams.

The remaining work is camera quality, richer track dressing and materials,
reference-image concept plumbing, and repeated-run validation.

## Contract boundaries

- The reference image may be supplied to Step 2, Concept, only.
- Step 3 receives Concept text, not the image.
- Steps 4–7 remain deterministic and LLM-free.
- No new schema fields, rendering systems, custom shaders, custom physics, or
  pipeline stages are introduced.
- A different seed may vary Concept/Schema content, but deterministic fixers
  must preserve the racing contract.
- Asset catalogs, imported binaries, and external lookup results must be frozen
  or cached when repeatability is required.

## Implementation map

### 1. Reference-image plumbing

Add `--reference-image <path>` to `unity-create`. Encode and send the image to
the Concept call only. Extract camera height, horizon framing, palette,
lighting tone, track composition, and environment density. Do not persist the
image path or derived image metadata in the schema.

Add tests proving that the image is attached to Step 2 and absent from Step 3
and all post-schema stages.

### 2. Racing camera

Upgrade the existing `vehicle_chase` camera template using its current target,
offset, smoothing, and FOV surface:

- low trailing motorcycle position;
- forward-biased look target;
- wide deterministic FOV interpolation;
- horizon stabilization;
- lean-linked visual roll;
- fixed speed-based shake and FOV curves;
- existing collision avoidance.

The reference is a trailing chase shot. A front-wheel-mounted view should be
an optional alternate preset, not the primary visual target.

### 3. Track geometry and race playability

Use the existing racing path, including `race_lap`, `path_points`,
`path_width`, elevation, and bank values. Extend the deterministic scaffold
with existing `EntitySpec` placement and model fields for:

- asphalt ribbon;
- curbs and runoff;
- inner and outer barriers;
- checkpoints and starting grid;
- banked curves and supported ramps;
- deterministic trackside dressing.

The motorcycle must remain a singleton `Player`, rivals must be separate
entities, and race completion must come from lap progression rather than a
free-running timer.

### 4. Atmosphere and materials

Reuse the existing AAA atmosphere contract for sky, sun, fog, shafts, bloom,
filmic grading, LUT, depth of field, exposure, ambient lighting, and skybox
reflections.

Add richer asphalt, curb, barrier, vegetation, grandstand, and distant-terrain
assets through the existing catalog, material, model-hint, and authored-scene
paths. Use physical sky or an existing HDRI/skybox primitive as a deterministic
choice; do not create a second rendering system.

Motion blur, explicit HDRI binding, and reflection-probe placement must first
be verified against the pinned Unity pipeline. If an existing primitive cannot
support one of them, defer it rather than inventing a custom substitute.

### 5. Depth-layered environment

Dress the course in fixed depth bands:

1. Immediate: curbs, barriers, tire stacks, signs, and marshal posts.
2. Midground: fences, grandstands, towers, trees, and service buildings.
3. Background: mountains, distant structures, and atmospheric silhouettes.

Use fixed ordering and seeds. Do not rely on unseeded runtime scatter.

### 6. Repeated-run validation

For the same prompt and seed, verify stable schema, geometry, asset identity,
generated files, and screenshots. For different seeds, verify that variation is
allowed only in Concept/Schema-controlled content and never breaks:

- racing genre and `drive` locomotion;
- motorcycle controller selection;
- player/rival identity;
- continuous track geometry;
- race objective and reachable checkpoints;
- atmosphere contract;
- C# compilation and play-mode startup.

Run the existing Python tests, C# syntax gate, Unity compile/play-mode smoke
test, screenshot capture, render-health checks, and playability gate.

## Required implementation inputs

The plan can proceed without more images. Before authored-scene implementation,
the most useful inputs are:

- a clean top-down track image or route file showing corners, start/finish,
  ramps, elevation, and runoff;
- the actual motorcycle FBX/GLB, preferably with wheel bones and correct scale;
- optional environment references for specific grandstands, barriers, or
  mountain silhouettes.

The top-down reference controls route geometry. The bike model controls authored
identity and final camera framing. Neither replaces deterministic compilation.

## Definition of done

The feature is complete only when a fresh Unity project repeatedly produces a
playable motorcycle race, renders a stable reference-like composition, passes
all deterministic contracts, and remains valid across approved seed variation.

Open a GitHub Pull Request immediately. No feature is considered complete until
it is PR'd.
