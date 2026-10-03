"""Lossy Unity schema normalization must remain visible in the canonical IR."""

from src.unity_schema import (NORMALIZATION_FORMAT, UnitySchema,
                              normalization_issues)


def _raw_schema(**overrides) -> dict:
    data = {
        "project_name": "NormalizationCheck",
        "game_type": "3D",
        "platform": "pc",
        "locomotion": "walk",
        "render_pipeline": "URP",
        "unity_version": "6000.0",
        "color_space": "Linear",
        "visual_style": "stylized",
        "features": ["audio"],
    }
    data.update(overrides)
    return data


def test_unknown_root_values_are_repaired_and_recorded():
    schema = UnitySchema.model_validate(_raw_schema(
        game_type="sideways", platform="toaster", locomotion="teleport",
        render_pipeline="raytrace", unity_version="nine", color_space="neon",
        visual_style="impossible", features=["audio", "magic"],
    ))

    assert (schema.game_type, schema.platform, schema.locomotion) == ("3D", "pc", "walk")
    report = schema.meta["normalization"]
    assert report["format"] == NORMALIZATION_FORMAT
    assert report["status"] == "repaired_with_loss"
    assert {issue["field"] for issue in normalization_issues(schema)} == {
        "game_type", "platform", "locomotion", "render_pipeline", "unity_version",
        "color_space", "visual_style", "features",
    }
    feature_issue = next(issue for issue in report["issues"]
                         if issue["field"] == "features")
    assert feature_issue["dropped"] == ["magic"]


def test_aliases_are_not_reported_as_lossy_repairs():
    schema = UnitySchema.model_validate(_raw_schema(
        game_type="isometric", platform="Android", locomotion="driving",
        render_pipeline="high definition", unity_version="Unity 6.2",
        color_space="linear", visual_style="low-poly", features=["audio"],
    ))

    assert schema.game_type == "isometric"
    assert schema.platform == "mobile"
    assert schema.locomotion == "drive"
    assert schema.render_pipeline == "HDRP"
    assert schema.unity_version == "6000.4"
    assert normalization_issues(schema) == []
    assert "normalization" not in schema.meta


def test_saved_lossy_report_survives_canonical_replay():
    original = UnitySchema.model_validate(_raw_schema(platform="toaster"))
    replayed = UnitySchema.model_validate(original.model_dump(mode="json"))

    assert replayed.meta["normalization"] == original.meta["normalization"]


def test_gamma_color_space_is_reported_when_the_render_pipeline_overrides_it():
    schema = UnitySchema.model_validate(_raw_schema(color_space="Gamma"))

    assert schema.color_space == "Linear"
    assert normalization_issues(schema) == [{
        "field": "color_space",
        "input": "Gamma",
        "normalized": "Linear",
        "reason": "URP/HDRP requires Linear color space",
    }]
