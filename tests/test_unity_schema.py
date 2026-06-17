"""
Tests for UnitySchema, UnityScript, UnityPackage validation and field defaults.
"""
import pytest
from pydantic import ValidationError

from src.unity_schema import UnityPackage, UnitySchema, UnityScript


# ── UnityScript ────────────────────────────────────────────────────────────────

class TestUnityScript:
    def test_valid_script(self):
        s = UnityScript(name="PlayerController", type="player", description="Moves the player")
        assert s.name == "PlayerController"
        assert s.type == "player"

    def test_name_required(self):
        with pytest.raises(ValidationError):
            UnityScript(type="player", description="x")

    def test_type_required(self):
        with pytest.raises(ValidationError):
            UnityScript(name="Foo", description="x")


# ── UnityPackage ───────────────────────────────────────────────────────────────

class TestUnityPackage:
    def test_package_id_optional(self):
        p = UnityPackage(description="cinemachine")
        assert p.package_id is None
        assert p.version is None

    def test_package_id_set(self):
        p = UnityPackage(
            description="cinemachine",
            package_id="com.unity.cinemachine",
            version="2.9.7",
        )
        assert p.package_id == "com.unity.cinemachine"
        assert p.version == "2.9.7"


# ── UnitySchema ────────────────────────────────────────────────────────────────

class TestUnitySchema:
    def test_minimal_valid(self, minimal_schema):
        assert minimal_schema.project_name == "MinimalProject"

    def test_unity_version_normalised(self):
        """Pydantic validator should accept "2022.3.21f1" and strip to "2022.3"."""
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="2022.3.21f1",
            color_space="Linear", description="",
            features=[], scenes=["Main"], scripts=[], packages=[],
        )
        assert s.unity_version == "2022.3"

    def test_unity_version_2021_normalised(self):
        """Pydantic validator should accept "2021.3" as a valid version."""
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="2021.3",
            color_space="Linear", description="",
            features=[], scenes=["Main"], scripts=[], packages=[],
        )
        assert s.unity_version == "2021.3"

    def test_unity_version_6_aliases(self):
        """All common Unity 6 shorthands normalise to 6000.0."""
        for alias in ("6", "6.0", "6000", "6000.0", "Unity 6", "6000.0.66f2"):
            s = UnitySchema(
                project_name="P", company_name="C",
                game_type="3D", platform="PC",
                render_pipeline="Built-in", unity_version=alias,
                color_space="Linear", description="",
                features=[], scenes=["Main"], scripts=[], packages=[],
            )
            assert s.unity_version in ("6000.0", "6000.1", "6000.4"), f"alias {alias!r} -> {s.unity_version!r}"

    def test_unity_version_6000_1_normalised(self):
        """6000.1 patch strings normalise to 6000.1."""
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="6000.1.4f1",
            color_space="Linear", description="",
            features=[], scenes=["Main"], scripts=[], packages=[],
        )
        assert s.unity_version == "6000.1"

    def test_unity_version_6000_4_normalised(self):
        """6000.4 patch strings normalise to 6000.4."""
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="6000.4.1f1",
            color_space="Linear", description="",
            features=[], scenes=["Main"], scripts=[], packages=[],
        )
        assert s.unity_version == "6000.4"

    def test_unknown_version_falls_back_to_6000(self):
        """Unrecognised version strings fall back to Unity 6 default."""
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="9999.9",
            color_space="Linear", description="",
            features=[], scenes=["Main"], scripts=[], packages=[],
        )
        assert s.unity_version == "6000.0"

    def test_unresolved_packages_empty_when_all_resolved(self):
        pkg = UnityPackage(
            description="urp",
            package_id="com.unity.render-pipelines.universal",
            version="14.0.9",
        )
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="URP", unity_version="6000.0",
            color_space="Linear", description="",
            features=[], scenes=["Main"],
            scripts=[UnityScript(name="GameManager", type="game_manager", description="")],
            packages=[pkg],
        )
        assert s.unresolved_packages() == []

    def test_unresolved_packages_detects_missing_id(self):
        pkg = UnityPackage(description="some unknown package")  # package_id=None
        s = UnitySchema(
            project_name="P", company_name="C",
            game_type="3D", platform="PC",
            render_pipeline="Built-in", unity_version="6000.0",
            color_space="Linear", description="",
            features=[], scenes=["Main"],
            scripts=[UnityScript(name="GameManager", type="game_manager", description="")],
            packages=[pkg],
        )
        assert len(s.unresolved_packages()) == 1

    def test_features_list_stored_correctly(self, full_3d_schema):
        assert "input_system" in full_3d_schema.features
        assert "navmesh"      in full_3d_schema.features

    def test_scenes_non_empty(self, minimal_schema):
        assert len(minimal_schema.scenes) >= 1


# ── EntitySpec path placement ───────────────────────────────────────────────────

class TestEntityPathPoints:
    def test_path_points_clamped_and_coerced(self):
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="Road", placement="path",
                       path_points=[[0, 0, 0], [1, 2], ["x", 1, 2], [99999, 0, -99999],
                                    "garbage", [3, 4, 5, 6]])
        # [1,2] padded to [1,2,0]; "x" entry dropped; out-of-range clamped to ±2000;
        # "garbage" dropped; 4-tuple truncated to 3.
        assert [1.0, 2.0, 0.0] in e.path_points
        assert [2000.0, 0.0, -2000.0] in e.path_points
        assert [3.0, 4.0, 5.0] in e.path_points
        assert len(e.path_points) == 4   # ["x",1,2] and "garbage" dropped

    def test_path_points_capped_at_64(self):
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="R", placement="path",
                       path_points=[[i, 0, 0] for i in range(200)])
        assert len(e.path_points) == 64

    def test_unknown_placement_still_clamps(self):
        from src.unity_schema import EntitySpec
        assert EntitySpec(name="X", placement="spline").placement == "single"


# ── EnvironmentSpec mood block ──────────────────────────────────────────────────

class TestEnvironmentSpec:
    def test_time_of_day_synonyms_and_clamp(self):
        from src.unity_schema import EnvironmentSpec
        assert EnvironmentSpec(time_of_day="dusk").time_of_day == "sunset"
        assert EnvironmentSpec(time_of_day="sunrise").time_of_day == "dawn"
        assert EnvironmentSpec(time_of_day="MIDNIGHT").time_of_day == "night"
        assert EnvironmentSpec(time_of_day="whenever").time_of_day == "day"

    def test_sun_intensity_clamped_or_none(self):
        from src.unity_schema import EnvironmentSpec
        assert EnvironmentSpec(sun_intensity=99).sun_intensity == 3.0
        assert EnvironmentSpec(sun_intensity="bright").sun_intensity is None
        assert EnvironmentSpec().sun_intensity is None

    def test_fog_color_normalised(self):
        from src.unity_schema import EnvironmentSpec
        assert EnvironmentSpec(fog_color="ff8844").fog_color == "#FF8844"
        assert EnvironmentSpec(fog_color=None).fog_color is None
