"""Contract tests for the mandatory Unity AAA Visual Atmosphere Pass."""

from src.unity_atmosphere import aaa_contract_failures
from src.unity_generator import UnityProjectGenerator
from src.unity_schema import UnitySchema, UnityScript


def _schema(pipeline="URP"):
    return UnitySchema(
        project_name="AAAProbe",
        company_name="TestCo",
        description="a quiet test scene",
        game_type="3D",
        platform="pc",
        render_pipeline=pipeline,
        unity_version="6000.0",
        color_space="Linear",
        features=[],
        scenes=["Main", "Menu"],
        packages=[],
        scripts=[UnityScript(name="PlayerController", type="player",
                             description="move")],
        meta={},
    )


def test_aaa_pass_is_unconditional_and_scene_scoped():
    schema = _schema()
    files = UnityProjectGenerator("templates/unity").generate(schema, {})

    assert "AAAProbe/Assets/Scripts/AAAAtmosphere.cs" in files
    assert "AAAProbe/Assets/Editor/SetupAAAVisualAtmosphere.cs" in files
    for scene_name in schema.scenes:
        scene = files[f"AAAProbe/Assets/Scenes/{scene_name}.unity"]
        assert "m_Name: AAA Visual Atmosphere" in scene
        assert "m_UsePhysicalProperties: 1" in scene
        assert "m_ColorTemperature: 6000" in scene

    assert aaa_contract_failures(
        files, schema.project_name, schema.scenes, schema.render_pipeline) == []


def test_aaa_pass_is_pipeline_aware_for_hdrp():
    schema = _schema("HDRP")
    files = UnityProjectGenerator("templates/unity").generate(schema, {})
    script = files["AAAProbe/Assets/Scripts/AAAAtmosphere.cs"]

    assert "UnityEngine.Rendering.HighDefinition" in script
    assert "PhysicallyBasedSky" in script
    assert "VolumetricFog" in script
    assert "AAAProbe/Assets/Editor/SetupAAAVisualAtmosphere.cs" in files
    assert "AAAProbe/Assets/Settings/PostProcessProfile.asset" not in files
    assert 'SetParameter(bloom, "threshold", 1.7f);' in script
    assert 'SetParameter(bloom, "intensity", 0.08f);' in script
    assert 'SetParameter(bloom, "scatter", 0.28f);' in script
    assert 'SetParameter(dof, "active", false);' in script
    assert 'SetParameter(exposure, "mode", "Fixed");' in script
    assert 'SetObjectMember(hdrpLight, "lightUnit", "Lux");' in script
    lighting = next(value for key, value in files.items()
                    if key.endswith("/Assets/Scripts/LightingDirector.cs"))
    assert 'fell back to the baked Flat ambient color.' in lighting
