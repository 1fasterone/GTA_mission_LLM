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
        # "garbage" dropped; 4th component kept as a bank angle.
        assert [1.0, 2.0, 0.0] in e.path_points
        assert [2000.0, 0.0, -2000.0] in e.path_points
        assert [3.0, 4.0, 5.0, 6.0] in e.path_points
        assert len(e.path_points) == 4   # ["x",1,2] and "garbage" dropped

    def test_path_points_capped_at_64(self):
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="R", placement="path",
                       path_points=[[i, 0, 0] for i in range(200)])
        assert len(e.path_points) == 64

    def test_bank_component_clamped_to_45_degrees(self):
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="R", placement="path",
                       path_points=[[0, 0, 0, 80], [1, 0, 0, -80], [2, 0, 0, 12.5]])
        assert e.path_points == [[0.0, 0.0, 0.0, 45.0],
                                 [1.0, 0.0, 0.0, -45.0],
                                 [2.0, 0.0, 0.0, 12.5]]

    def test_zero_bank_drops_back_to_triple(self):
        # A 0° bank must not survive as a 4th component, or unbanked schemas
        # authored as 4-tuples would flip the generator's has_banked_paths
        # gate and change otherwise byte-identical output.
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="R", placement="path",
                       path_points=[[0, 0, 0, 0], [1, 0, 0, 0.0]])
        assert e.path_points == [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]

    def test_instance_positions_never_carry_bank(self):
        from src.unity_schema import EntitySpec
        e = EntitySpec(name="R", instance_positions=[[1, 2, 3, 30]])
        assert e.instance_positions == [[1.0, 2.0, 3.0]]

    def test_unknown_placement_still_clamps(self):
        from src.unity_schema import EntitySpec
        assert EntitySpec(name="X", placement="spline").placement == "single"

    def test_path_width_defaults_to_zero(self):
        from src.unity_schema import EntitySpec
        assert EntitySpec(name="R", placement="path").path_width == 0.0

    def test_path_width_clamped_and_coerced(self):
        from src.unity_schema import EntitySpec
        assert EntitySpec(name="R", path_width=-3).path_width == 0.0
        assert EntitySpec(name="R", path_width=500).path_width == 100.0
        assert EntitySpec(name="R", path_width="garbage").path_width == 0.0
        assert EntitySpec(name="R", path_width=10).path_width == 10.0


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


# ── Role-graph invariants ─────────────────────────────────────────────────────

class TestRoleGraphInvariants:
    """UnitySchema.enforce_role_graph — cross-field role checks.

    The motivating defect: a Step-3 schema-fill that inverted the character
    role graph (the final boss filled in as role="player", the real player
    demoted to an NPC). Both orientations are schema-valid, so only a
    cross-field check against the deterministic entity tags can catch it.
    """

    @staticmethod
    def _base(**overrides):
        kwargs = dict(
            project_name="AntiqueShop", game_type="3D", platform="pc",
            render_pipeline="URP",
        )
        kwargs.update(overrides)
        return UnitySchema(**kwargs)

    def test_inverted_roles_repaired_from_entity_tags(self):
        """The run-#4 inversion: boss as player, player as npc — both flipped
        back deterministically, sourced from the entity tags, and logged."""
        s = self._base(
            entities=[
                {"name": "GrandmaEdna",  "kind": "player", "tag": "Player"},
                {"name": "TheAppraiser", "kind": "boss",   "tag": "Enemy"},
            ],
            characters=[
                {"id": "appraiser", "name": "TheAppraiser", "role": "player"},
                {"id": "edna",      "name": "GrandmaEdna",  "role": "npc"},
            ],
        )
        roles = {c.name: c.role for c in s.characters}
        assert roles["GrandmaEdna"] == "player"
        assert roles["TheAppraiser"] == "boss"
        repairs = s.meta["role_lint"]["repairs"]
        assert len(repairs) == 2
        assert not s.meta["role_lint"].get("violations")

    def test_display_name_matches_single_token_entity(self):
        """CharacterSpec may carry the display form; keys must still match."""
        s = self._base(
            entities=[{"name": "GrandmaEdna", "kind": "player", "tag": "Player"}],
            characters=[{"id": "edna", "name": "Grandma Edna", "role": "npc"}],
        )
        assert s.characters[0].role == "player"

    def test_correct_roles_untouched_and_unlogged(self):
        s = self._base(
            entities=[
                {"name": "GrandmaEdna",  "kind": "player", "tag": "Player"},
                {"name": "TheAppraiser", "kind": "boss",   "tag": "Enemy"},
            ],
            characters=[
                {"id": "edna",      "name": "GrandmaEdna",  "role": "player"},
                {"id": "appraiser", "name": "TheAppraiser", "role": "boss"},
            ],
        )
        assert "role_lint" not in s.meta

    def test_duplicate_player_tag_stripped_to_singleton(self):
        """R1: per-entity _reserve_player_tag can't see siblings; the root
        keeps the kind='player' holder and untags the rest (AstralArena)."""
        s = self._base(entities=[
            {"name": "WeaponModels", "kind": "vehicle", "tag": "Player"},
            {"name": "GrandmaEdna",  "kind": "player",  "tag": "Player"},
        ])
        tags = {e.name: e.tag for e in s.entities}
        assert tags["GrandmaEdna"] == "Player"
        assert tags["WeaponModels"] == "Untagged"
        assert any("R1" in r for r in s.meta["role_lint"]["repairs"])

    def test_no_player_character_is_violation_not_failure(self):
        """R3: underivable defects are recorded for the playability gate,
        never raised — one bad role must not discard a valid schema."""
        s = self._base(characters=[
            {"id": "a", "name": "AuctionRaider", "role": "enemy"},
        ])
        assert s.project_name == "AntiqueShop"   # schema still valid
        assert any("R3" in v for v in s.meta["role_lint"]["violations"])

    def test_no_characters_no_lint(self):
        """Legacy schemas without CharacterSpec stay byte-identical."""
        s = self._base(entities=[{"name": "GrandmaEdna", "kind": "player",
                                  "tag": "Player"}])
        assert "role_lint" not in s.meta
