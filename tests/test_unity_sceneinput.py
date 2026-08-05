"""Tests for the deterministic scene-layout front door (src/unity_sceneinput.py)
— Scene Compiler layout JSON -> canonical scene file."""

import json

import pytest

from src.unity_sceneinput import (MAX_INSTANCES, SceneInputError, build_scene,
                                  default_models_dir, entity_name_for,
                                  load_layout, load_scene, role_for,
                                  to_unity_position, to_unity_yaw)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _item(name, file, pos=(0, 0, 0), yaw=0.0, scale=1.0, **extra):
    d = {"name": name, "file": file, "type": "model", "orient": None,
         "position": list(pos), "yaw_deg": yaw, "scale": scale}
    d.update(extra)
    return d


def _layout(*items, **extra):
    d = {"version": 2, "units": "meters", "view_mode": "top",
         "items": list(items)}
    d.update(extra)
    return d


@pytest.fixture
def models(tmp_path):
    root = tmp_path / "models"
    (root / "props").mkdir(parents=True)
    for rel in ("monacoGP.fbx", "victorian_monobike.glb", "hero.png",
                "props/crate.fbx"):
        (root / rel).write_bytes(b"BINARY")
    return root


# ── coordinates ──────────────────────────────────────────────────────────────

class TestCoordinateConversion:
    def test_z_axis_flips_for_unity_handedness(self):
        assert to_unity_position([1, 2, 3]) == [1.0, 2.0, -3.0]

    def test_negative_zero_is_normalized(self):
        # -0.0 serializes as "-0" and makes identical layouts diff.
        assert to_unity_position([0, 0, 0]) == [0.0, 0.0, 0.0]
        assert not any(str(v).startswith("-") for v in to_unity_position([0, 0, 0]))

    def test_positions_clamp_to_the_schema_range(self):
        assert to_unity_position([9999, 0, -9999]) == [2000.0, 0.0, 2000.0]

    def test_yaw_flips_and_wraps_into_zero_360(self):
        assert to_unity_yaw(150) == 210.0
        assert to_unity_yaw(0) == 0.0
        assert to_unity_yaw(-90) == 90.0

    def test_malformed_position_fails_loudly(self):
        with pytest.raises(SceneInputError, match="position"):
            to_unity_position([1, "x", 3])


# ── naming ───────────────────────────────────────────────────────────────────

class TestEntityNaming:
    @pytest.mark.parametrize("filename,expected", [
        ("monacoGP.fbx", "MonacoGP"),
        ("victorian_monobike.glb", "VictorianMonobike"),
        ("big-head-cromulon.fbx", "BigHeadCromulon"),
        ("props/crate.fbx", "Crate"),
    ])
    def test_pascal_case_from_stem(self, filename, expected):
        assert entity_name_for(filename) == expected

    def test_same_file_twice_gets_distinct_entity_names(self, models):
        layout = _layout(
            _item("a", "monacoGP.fbx", scale=1.0),
            _item("b", "monacoGP.fbx", scale=2.0),   # different scale -> own group
        )
        scene, _ = build_scene(layout, models)
        assert [g["name"] for g in scene["groups"]] == ["MonacoGP", "MonacoGP2"]


# ── grouping ─────────────────────────────────────────────────────────────────

class TestGrouping:
    def test_repeated_model_becomes_one_group_with_baked_instances(self, models):
        layout = _layout(
            _item("bike_01", "victorian_monobike.glb", pos=(-64, 0, -40), yaw=150),
            _item("bike_02", "victorian_monobike.glb", pos=(-65.5, 0, -41), yaw=150),
            _item("bike_03", "victorian_monobike.glb", pos=(-67, 0, -42), yaw=150),
        )
        scene, _ = build_scene(layout, models)
        assert len(scene["groups"]) == 1
        g = scene["groups"][0]
        assert g["count"] == 3
        assert g["positions"] == [[-64, 0, 40], [-65.5, 0, 41], [-67, 0, 42]]
        assert g["yaws"] == [210.0, 210.0, 210.0]

    def test_distinct_scales_split_into_distinct_groups(self, models):
        layout = _layout(_item("a", "props/crate.fbx", scale=1.0),
                         _item("b", "props/crate.fbx", scale=1.0),
                         _item("c", "props/crate.fbx", scale=3.0))
        scene, _ = build_scene(layout, models)
        assert [g["count"] for g in scene["groups"]] == [2, 1]
        assert [g["scale"] for g in scene["groups"]] == [1.0, 3.0]

    def test_group_and_instance_order_follow_the_layout(self, models):
        layout = _layout(_item("a", "props/crate.fbx"),
                         _item("b", "monacoGP.fbx"),
                         _item("c", "props/crate.fbx"))
        scene, _ = build_scene(layout, models)
        assert [g["file"] for g in scene["groups"]] == ["props/crate.fbx",
                                                        "monacoGP.fbx"]
        assert scene["groups"][0]["items"] == ["a", "c"]

    def test_instances_beyond_the_schema_cap_are_dropped_with_a_warning(self, models):
        layout = _layout(*[_item(f"c{i}", "props/crate.fbx")
                           for i in range(MAX_INSTANCES + 5)])
        scene, warnings = build_scene(layout, models)
        assert scene["groups"][0]["count"] == MAX_INSTANCES
        assert any("more than" in w for w in warnings)

    def test_sprites_are_typed_separately(self, models):
        scene, _ = build_scene(_layout(_item("h", "hero.png", type="sprite",
                                             orient="upright")), models)
        assert scene["groups"][0]["type"] == "sprite"


# ── base_scale ───────────────────────────────────────────────────────────────

class TestBaseScale:
    def test_base_scale_multiplies_the_layout_scale(self, models):
        layout = _layout(_item("t", "monacoGP.fbx", scale=2.5, base_scale=0.01))
        scene, warnings = build_scene(layout, models)
        assert scene["groups"][0]["scale"] == 0.025
        assert warnings == []

    def test_missing_base_scale_warns_instead_of_guessing(self, models):
        scene, warnings = build_scene(_layout(_item("t", "monacoGP.fbx")), models)
        assert scene["groups"][0]["scale"] == 1.0
        assert any("base_scale" in w and "monacoGP.fbx" in w for w in warnings)

    def test_non_positive_scale_fails_loudly(self, models):
        with pytest.raises(SceneInputError, match="non-positive"):
            build_scene(_layout(_item("t", "monacoGP.fbx", scale=0)), models)


# ── failure modes ────────────────────────────────────────────────────────────

class TestFailsLoudly:
    def test_missing_model_file_names_it_and_the_remedy(self, models):
        with pytest.raises(SceneInputError) as exc:
            build_scene(_layout(_item("x", "nope.fbx")), models)
        assert "nope.fbx" in str(exc.value)
        assert "models dir" in str(exc.value)

    def test_missing_models_dir_fails(self, tmp_path):
        with pytest.raises(SceneInputError, match="models dir not found"):
            build_scene(_layout(_item("x", "a.fbx")), tmp_path / "nope")

    def test_unsupported_extension_fails(self, models, tmp_path):
        (models / "notes.txt").write_text("hi")
        with pytest.raises(SceneInputError, match="unsupported asset type"):
            build_scene(_layout(_item("x", "notes.txt")), models)

    def test_layout_without_items_is_rejected(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"version": 2, "items": []}))
        with pytest.raises(SceneInputError, match="no items"):
            load_layout(p)

    def test_non_layout_json_is_rejected(self, tmp_path):
        p = tmp_path / "other.json"
        p.write_text(json.dumps({"hello": "world"}))
        with pytest.raises(SceneInputError, match="not a Scene Compiler layout"):
            load_layout(p)


# ── determinism + round trip ─────────────────────────────────────────────────

class TestSceneFile:
    def test_same_layout_produces_byte_identical_scene(self, models):
        layout = _layout(_item("a", "props/crate.fbx", pos=(1, 0, 2), yaw=90),
                         _item("b", "monacoGP.fbx"))
        first, _ = build_scene(layout, models)
        second, _ = build_scene(layout, models)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_load_scene_accepts_a_normalized_scene_file(self, models, tmp_path):
        scene, _ = build_scene(_layout(_item("a", "props/crate.fbx")), models)
        p = tmp_path / "scene.json"
        p.write_text(json.dumps(scene))
        again, warnings = load_scene(p)
        assert again["groups"] == scene["groups"]
        assert warnings == []

    def test_load_scene_accepts_a_raw_layout_via_the_models_sibling(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "crate.fbx").write_bytes(b"F")
        (tmp_path / "layouts").mkdir()
        p = tmp_path / "layouts" / "L.json"
        p.write_text(json.dumps(_layout(_item("a", "crate.fbx"))))
        scene, _ = load_scene(p)
        assert scene["groups"][0]["name"] == "Crate"
        assert default_models_dir(p) == (tmp_path / "models").resolve()

    def test_scene_file_pointing_at_deleted_models_fails_with_a_remedy(
            self, models, tmp_path):
        scene, _ = build_scene(_layout(_item("a", "props/crate.fbx")), models)
        (models / "props" / "crate.fbx").unlink()
        p = tmp_path / "scene.json"
        p.write_text(json.dumps(scene))
        with pytest.raises(SceneInputError, match="unity-scene"):
            load_scene(p)


# ── role prefixes ────────────────────────────────────────────────────────────
# The author's channel for declaring WHAT a model is. Binding used to score an
# authored group against entity names the Pipeline LLM invented, which works
# only when the two happen to share a word — `victorian_monobike.fbx` reaches
# an entity called `PlayerMonobike` and reaches nothing called `Racer`. A
# leading role word on the filename is a statement instead of a coincidence,
# and it needs no change to the Scene Compiler app.

class TestRolePrefixes:
    def test_leading_role_word_is_the_kind(self):
        assert role_for("player_monobike.fbx") == "player"
        assert role_for("enemy_drone.glb") == "enemy"
        assert role_for("pickup_scrap.fbx") == "pickup"

    def test_separators_and_casing_do_not_matter(self):
        for name in ("player_monobike.fbx", "player-monobike.fbx",
                     "playerMonobike.fbx", "Player_Monobike.fbx",
                     "PLAYER.monobike.fbx"):
            assert role_for(name) == "player", name

    def test_aliases_normalize_to_the_pipeline_kind(self):
        assert role_for("rival_bike.fbx") == "enemy"
        assert role_for("hero_knight.fbx") == "player"
        assert role_for("car_red.fbx") == "vehicle"

    def test_a_non_role_first_word_declares_nothing(self):
        """Every layout written before roles existed keeps its old binding."""
        assert role_for("victorian_monobike.fbx") is None
        assert role_for("monacoGP.fbx") is None

    def test_the_role_word_must_LEAD(self):
        """`bike_player.fbx` is a bike, not a declaration — only the first
        token is read, so a role word later in the name is just a word."""
        assert role_for("bike_player.fbx") is None

    def test_role_travels_on_the_group(self, models, tmp_path):
        (models / "player_monobike.fbx").write_bytes(b"BINARY")
        scene, _ = build_scene(
            _layout(_item("a", "player_monobike.fbx"),
                    _item("b", "monacoGP.fbx")),
            models)
        by_file = {g["file"]: g for g in scene["groups"]}
        assert by_file["player_monobike.fbx"]["role"] == "player"
        assert by_file["monacoGP.fbx"]["role"] is None

    def test_the_group_name_still_reads_naturally(self):
        """The role word stays in the entity name — `PlayerMonobike` is a
        better name than `Monobike`, and it helps keyword binding too."""
        assert entity_name_for("player_monobike.fbx") == "PlayerMonobike"

    def test_a_bare_role_word_is_not_a_declaration(self):
        """`vehicle.fbx` / `tree.fbx` are ordinary asset names. Reading them as
        declarations would let one generic file claim an entity outright — the
        case MIN_BIND_SCORE exists to prevent."""
        for name in ("vehicle.fbx", "tree.fbx", "car.fbx", "player.fbx"):
            assert role_for(name) is None, name
