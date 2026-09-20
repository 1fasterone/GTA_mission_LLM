# Mandatory AAA Visual Atmosphere Pass

Every Unity project emitted by Step 6 receives the same deterministic visual
foundation before gameplay scripts run. It is not gated on genre, locomotion,
scene mood, or atmosphere keywords.

## Generated surface

- `Assets/Scripts/AAAAtmosphere.cs` runs at execution order `-10000`.
- Every generated scene contains an `AAA Visual Atmosphere` GameObject.
- URP/HDRP projects also receive `Assets/Editor/SetupAAAVisualAtmosphere.cs`.
- URP keeps the serialized `PostProcessProfile.asset`; the runtime bootstrap
  repairs or creates every required override when the project opens.
- HDRP creates the profile and pipeline-specific overrides at runtime because
  HDRP serialized override identities differ from URP.

The profile includes Bloom, Vignette, low Chromatic Aberration, Film Grain,
ACES filmic tonemapping, Color Adjustments, a generated LUT, Depth of Field,
and automatic exposure (with a deterministic URP fallback when the selected
URP revision has no Exposure override).

The scene foundation also configures a 6000K soft-shadow directional light,
contact-shadow hooks, ambient occlusion, procedural/physical sky, exponential
and height fog, light-shaft proxies, three procedural ground texture layers,
dirt/crack/foliage decals, ambient dust/pollen particles, and a 32mm physical
camera.

## Contract and repair behavior

`src/unity_atmosphere.py` validates the emitted files before `generate()`
returns. A missing script, scene object, editor setup hook, or required visual
marker fails loudly with a regeneration remedy. Runtime validation logs the
same remedy if a user deletes or corrupts a pipeline component after codegen.

The pass is deterministic: no LLM calls, random seeds, network fetches, or
schema invention are involved. The Pipeline LLM remains limited to Steps 2–3.
