"""Deterministic contract for the mandatory Unity AAA visual atmosphere pass.

This module deliberately contains no Unity or LLM calls.  The generator emits
the pass and this contract verifies the emitted artifact before it can leave
Step 6.  Runtime repair lives in ``templates/unity/scripts/AAAAtmosphere.cs.j2``
because a generated project may be opened in an editor whose scene was later
hand-edited or whose pipeline asset was recreated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


AAA_SCRIPT_PATH = "Assets/Scripts/AAAAtmosphere.cs"
AAA_EDITOR_SCRIPT_PATH = "Assets/Editor/SetupAAAVisualAtmosphere.cs"

# These are intentionally semantic markers rather than a fragile exact-output
# snapshot.  They describe the public contract of the pass and make a missing
# section a loud generator error instead of a visually blank project.
AAA_SCRIPT_MARKERS: tuple[str, ...] = (
    "AAA Visual Atmosphere",
    "ConfigureVolumeProfile",
    "Bloom",
    "Vignette",
    "ChromaticAberration",
    "FilmGrain",
    "DepthOfField",
    "ConfigureAutomaticExposure",
    "ConfigureDirectionalLight",
    "ConfigureGlobalIllumination",
    "ConfigureAmbientOcclusion",
    "ConfigureSkyAndFog",
    "Volumetric Fog",
    "Height Fog",
    "Light Shafts",
    "Ground Texture Layer",
    "Dirt Decal",
    "Crack Decal",
    "Foliage Decal",
    "Ambient Particles",
    "ApplyLutGrade",
    "usePhysicalProperties",
    "focalLength",
)

AAA_BUILTIN_MARKERS: tuple[str, ...] = (
    "AAA Visual Atmosphere",
    "ConfigureDirectionalLight",
    "ConfigureSkyAndFog",
    "ConfigureCamera",
    "mandatory URP/HDRP AAA pass",
    "usePhysicalProperties",
    "focalLength",
)

AAA_EDITOR_MARKERS: tuple[str, ...] = (
    "SetupAAAVisualAtmosphere",
    "m_SupportsHDR",
    "ScreenSpaceAmbientOcclusion",
)


def aaa_contract_failures(
    files: Mapping[str, str],
    project_name: str,
    scenes: Sequence[str],
    render_pipeline: str,
) -> list[str]:
    """Return missing mandatory-atmosphere pieces in generated Unity files."""

    failures: list[str] = []
    script_key = f"{project_name}/{AAA_SCRIPT_PATH}"
    script = files.get(script_key, "")
    if not script:
        failures.append(script_key)
    else:
        required_markers = (AAA_SCRIPT_MARKERS if render_pipeline in ("URP", "HDRP")
                            else AAA_BUILTIN_MARKERS)
        failures.extend(
            f"{script_key}: marker {marker!r}"
            for marker in required_markers
            if marker not in script
        )

    if render_pipeline in ("URP", "HDRP"):
        editor_key = f"{project_name}/{AAA_EDITOR_SCRIPT_PATH}"
        editor = files.get(editor_key, "")
        if not editor:
            failures.append(editor_key)
        else:
            failures.extend(
                f"{editor_key}: marker {marker!r}"
                for marker in AAA_EDITOR_MARKERS
                if marker not in editor
            )

    for scene_name in scenes:
        scene_key = f"{project_name}/Assets/Scenes/{scene_name}.unity"
        scene = files.get(scene_key, "")
        if "m_Name: AAA Visual Atmosphere" not in scene:
            failures.append(f"{scene_key}: AAA Visual Atmosphere GameObject")
        if "aaa_atmosphere_guid" in scene:
            failures.append(f"{scene_key}: unresolved aaa_atmosphere_guid")

    return failures


def assert_aaa_contract(
    files: Mapping[str, str],
    project_name: str,
    scenes: Sequence[str],
    render_pipeline: str,
) -> None:
    """Fail loudly if a generated project does not carry the AAA pass."""

    failures = aaa_contract_failures(files, project_name, scenes, render_pipeline)
    if failures:
        details = "\n  - ".join(failures)
        raise RuntimeError(
            "AAA Visual Atmosphere Pass contract failed; regenerate the Unity "
            "output after restoring the mandatory pass:\n  - " + details
        )
