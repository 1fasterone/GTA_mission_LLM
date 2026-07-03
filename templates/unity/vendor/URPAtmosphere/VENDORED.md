# URP-Atmosphere (vendored)

Source:  https://github.com/sinnwrig/URP-Atmosphere (branch: main, fetched 2026-07-02)
License: MIT (see LICENSE.md — Copyright (c) sinnwrig)

Vendored subset: Assets/Atmosphere/{Runtime, Editor, Atmosphere.asmdef} plus the
DefaultAtmosphereProfile from Examples/Profiles. Examples and the bundled
.unitypackage are intentionally excluded (53 MB of demo content).

Shipped by unity_generator.py into 3D URP projects whose concept implies an
atmosphere (sky/altitude/flight keywords or locomotion=fly):
- Atmosphere/ → Assets/Atmosphere/ (render feature + shaders, .metas preserved
  so the profile→compute-shader GUID reference stays intact)
- Profile/DefaultAtmosphereProfile.asset → Assets/Resources/Atmosphere/
- templates/unity/editor/SetupAtmosphere.cs.j2 registers AtmosphereRenderFeature
  on the URP renderer (run-once, same pattern as SetupRenderPipeline)
- templates/unity/scripts/AtmosphereBootstrap.cs.j2 spawns the AtmosphereEffect
  at scene load (planet center below the scene, sun = brightest directional)
