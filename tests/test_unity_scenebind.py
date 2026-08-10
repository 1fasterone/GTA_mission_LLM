"""Tests for authored-scene binding (src/unity_scenebind.py) and the Tier A
authored-model path in src/unity_entity_asset_resolver.py.

The contract under test: a Scene Compiler layout decides WHICH model and
WHERE, the pipeline keeps deciding everything else.
"""

import json

import pytest

from src.unity_entity_asset_resolver import resolve_entity_assets
from src.unity_layout import compute_layout
from src.unity_model_resolver import ResolvedModel
from src.unity_sceneinput import role_for
from src.unity_scenebind import (MIN_BIND_SCORE, apply_scene, authored_models,
                                 bind_score)
from src.unity_schema import EntitySpec, UnitySchema


# ── fixtures ─────────────────────────────────────────────────────────────────

def _schema(entities, game_type="3D", scenes=None):
    return UnitySchema(
        project_name="SceneBindTest", company_name="C", description="",
        game_type=game_type, platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=scenes or ["Main"], packages=[],
        entities=entities,
    )


def _group(name, file, src="", count=1, positions=None, yaws=None,
           scale=1.0, gtype="model", role=None):
    positions = positions or [[0.0, 0.0, 0.0]]
    return {
        "name": name, "file": file, "source_file": file,
        "src": src or f"C:/models/{file}", "type": gtype, "orient": None,
        "role": role if role is not None else role_for(file),
        "scale": scale, "count": count or len(positions),
        "positions": positions, "yaws": yaws or [0.0] * len(positions),
        "items": [f"{name}_{i+1:02d}" for i in range(len(positions))],
    }


def _scene(*groups):
    return {"version": 1, "source": "scene-compiler", "layout": "TEST",
            "layout_path": "", "models_dir": "C:/models", "view_mode": "top",
            "units": "meters", "groups": list(groups)}


# ── matching ─────────────────────────────────────────────────────────────────

class TestBindScore:
    def test_name_match_binds(self):
        e = EntitySpec(name="MonacoCircuit", kind="road")
        assert bind_score(_group("MonacoGP", "monaco-gp-circuit.fbx"), e) >= MIN_BIND_SCORE

    def test_containment_matches_bike_to_monobike(self):
        e = EntitySpec(name="PlayerBike", kind="vehicle")
        assert bind_score(_group("VictorianMonobike", "victorian_monobike.fbx"),
                          e) >= MIN_BIND_SCORE

    def test_model_hint_alone_is_enough(self):
        e = EntitySpec(name="Rivals", kind="vehicle", model_hint="monobike racer")
        assert bind_score(_group("VictorianMonobike", "victorian_monobike.fbx"),
                          e) >= MIN_BIND_SCORE

    def test_kind_alone_is_below_the_threshold(self):
        # Otherwise one authored car would claim every kind="vehicle" group.
        e = EntitySpec(name="Rivals", kind="vehicle")
        assert bind_score(_group("Vehicle", "vehicle.fbx"), e) < MIN_BIND_SCORE

    def test_unrelated_model_scores_zero(self):
        e = EntitySpec(name="Spectators", kind="crowd")
        assert bind_score(_group("MonacoGP", "monaco-gp-circuit.fbx"), e) == 0.0


# ── binding + baking ─────────────────────────────────────────────────────────

class TestApplyScene:
    def test_bound_entity_gets_the_authored_transforms(self):
        e = EntitySpec(name="RivalBikes", kind="vehicle", count=6,
                       placement="scatter", scale=1.0)
        schema = _schema([e])
        apply_scene(schema, _scene(_group(
            "VictorianMonobike", "victorian_monobike.fbx", count=3,
            positions=[[-64, 0, 40], [-65.5, 0, 41], [-67, 0, 42]],
            yaws=[210.0, 210.0, 210.0], scale=0.2)))

        assert e.count == 3                        # layout, not the LLM's 6
        assert e.scale == 0.2
        assert e.instance_positions == [[-64, 0, 40], [-65.5, 0, 41], [-67, 0, 42]]
        assert e.instance_yaws == [210.0, 210.0, 210.0]

    def test_unmatched_group_is_injected_as_its_own_entity(self):
        schema = _schema([EntitySpec(name="Spectators", kind="crowd")])
        apply_scene(schema, _scene(_group("MonacoGP", "monaco-gp-circuit.fbx")))

        assert [e.name for e in schema.entities] == ["Spectators", "MonacoGP"]
        injected = schema.entities[-1]
        assert injected.instance_positions == [[0.0, 0.0, 0.0]]
        assert "monaco" in injected.model_hint

    def test_injected_name_never_collides(self):
        # "MonacoGP" binds to the FIRST group (both name the circuit), so the
        # second group has to inject under a name already in use.
        schema = _schema([EntitySpec(name="MonacoGP", kind="road")])
        apply_scene(schema, _scene(_group("MonacoGPCircuit",
                                          "monaco-gp-circuit.fbx"),
                                   _group("MonacoGP", "monacoGP.fbx")))
        names = [e.name for e in schema.entities]
        assert len(set(names)) == len(names) == 2
        assert "MonacoGP2" in names

    def test_a_single_instance_group_binds_exactly_one_entity(self):
        a = EntitySpec(name="PlayerBike", kind="vehicle")
        b = EntitySpec(name="RivalBike", kind="vehicle")
        schema = _schema([a, b])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian_monobike.fbx")))
        bound = [e for e in (a, b) if e.instance_positions]
        assert len(bound) == 1     # one instance authored -> nothing to share


class TestStartingGridSpill:
    """The defect a live Monaco run exposed: three authored monobikes all
    landed on PlayerMonobike (count=3), which silently disqualifies it from
    the generator's player-merge (that path requires count == 1) — the player
    then drove an invisible capsule while three bikes sat on the grid.
    """

    def _grid_schema(self):
        return _schema([
            EntitySpec(name="PlayerMonobike", kind="vehicle", tag="Player",
                       count=1),
            EntitySpec(name="RivalMonobikeOne", kind="vehicle", count=1),
            EntitySpec(name="RivalMonobikeTwo", kind="vehicle", count=1),
        ])

    def _grid_scene(self):
        return _scene(_group(
            "VictorianMonobike", "victorian_monobike.fbx", count=3,
            positions=[[-64, 0, 40], [-65.5, 0, 41], [-67, 0, 42]],
            yaws=[210.0, 210.0, 210.0]))

    def test_player_keeps_count_one_so_the_player_merge_still_fires(self):
        schema = self._grid_schema()
        apply_scene(schema, self._grid_scene())
        player = schema.entities[0]
        assert player.count == 1
        assert player.instance_positions == [[-64, 0, 40]]   # pole position

    def test_each_named_rival_gets_one_authored_instance(self):
        schema = self._grid_schema()
        apply_scene(schema, self._grid_scene())
        assert schema.entities[1].instance_positions == [[-65.5, 0, 41]]
        assert schema.entities[2].instance_positions == [[-67, 0, 42]]
        assert all(authored_models(schema)[e.name]["file"]
                   == "victorian_monobike.fbx" for e in schema.entities)

    def test_every_authored_instance_is_placed_exactly_once(self):
        schema = self._grid_schema()
        apply_scene(schema, self._grid_scene())
        placed = [tuple(p) for e in schema.entities for p in e.instance_positions]
        assert sorted(placed) == sorted({(-64, 0, 40), (-65.5, 0, 41),
                                         (-67, 0, 42)})

    def test_group_entities_never_take_spill(self):
        # A 20-barrel pile is one pile, not 19 plus a loose barrel.
        pile = EntitySpec(name="BarrelPile", kind="prop", count=20)
        stack = EntitySpec(name="BarrelStack", kind="prop", count=6)
        schema = _schema([pile, stack])
        apply_scene(schema, _scene(_group(
            "Barrel", "barrel.fbx", count=4,
            positions=[[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]],
            yaws=[0.0] * 4)))
        assert pile.count == 4                 # the whole authored group
        assert stack.instance_positions == []  # count > 1 -> not a singleton

    def test_spill_is_reported(self):
        schema = self._grid_schema()
        msgs = apply_scene(schema, self._grid_scene())
        assert any("RivalMonobikeOne" in m and "keeps 1" in m for m in msgs)
        assert schema.meta["authored_scene"]["spill"] == 2

    def test_spill_is_deterministic(self):
        runs = []
        for _ in range(3):
            schema = self._grid_schema()
            apply_scene(schema, self._grid_scene())
            runs.append([(e.name, e.instance_positions) for e in schema.entities])
        assert runs[0] == runs[1] == runs[2]

    def test_binding_is_deterministic_across_runs(self):
        scene = _scene(_group("VictorianMonobike", "victorian_monobike.fbx"),
                       _group("MonacoGP", "monaco-gp-circuit.fbx"))
        results = []
        for _ in range(3):
            schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle"),
                              EntitySpec(name="MonacoTrack", kind="road")])
            apply_scene(schema, json.loads(json.dumps(scene)))
            results.append([(e.name, e.instance_positions) for e in schema.entities])
        assert results[0] == results[1] == results[2]

    def test_wiring_is_untouched(self):
        e = EntitySpec(name="PlayerBike", kind="vehicle", tag="Player",
                       behavior="drive")
        schema = _schema([e])
        schema.scripts = list(schema.scripts)
        n_scripts = len(schema.scripts)
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian_monobike.fbx")))
        assert e.tag == "Player" and e.behavior == "drive" and e.kind == "vehicle"
        assert len(schema.scripts) == n_scripts

    def test_uniform_nonzero_height_becomes_group_elevation(self):
        e = EntitySpec(name="Platforms", kind="platform")
        schema = _schema([e])
        apply_scene(schema, _scene(_group(
            "Platforms", "platform.fbx", positions=[[0, 12, 0], [4, 12, 0]],
            yaws=[0.0, 0.0])))
        assert e.elevation == 12.0

    def test_path_entities_keep_their_geometry(self):
        e = EntitySpec(name="TrackRibbon", kind="road", placement="path",
                       path_points=[[0, 0, 0], [0, 0, 10]], path_width=8.0)
        schema = _schema([e])
        msgs = apply_scene(schema, _scene(_group("TrackRibbon", "track.fbx")))
        assert e.path_points == [[0, 0, 0], [0, 0, 10]]
        assert e.instance_positions == []
        assert any("path geometry" in m for m in msgs)
        # ...but the model binding still applies.
        assert "TrackRibbon" in authored_models(schema)

    def test_2d_projects_bind_models_but_abstain_from_placement(self):
        e = EntitySpec(name="Crate", kind="prop")
        schema = _schema([e], game_type="2D")
        msgs = apply_scene(schema, _scene(_group("Crate", "crate.fbx",
                                                 positions=[[5, 0, 5]])))
        assert e.instance_positions == []
        assert "Crate" in authored_models(schema)
        assert any("cannot be honored" in m for m in msgs)

    def test_sprite_in_a_3d_project_is_skipped_loudly(self):
        schema = _schema([EntitySpec(name="Hero", kind="character")])
        msgs = apply_scene(schema, _scene(_group("Hero", "hero.png",
                                                 gtype="sprite")))
        assert authored_models(schema) == {}
        assert any("sprite" in m for m in msgs)

    def test_provenance_is_stamped_on_meta(self):
        schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian_monobike.fbx"),
                                   _group("MonacoGP", "monaco-gp-circuit.fbx")))
        prov = schema.meta["authored_scene"]
        assert prov["groups"] == 2 and prov["bound"] == 1 and prov["injected"] == 1


# ── interaction with the layout pass ─────────────────────────────────────────

class TestLayoutRespectsAuthoredPlacement:
    def test_compute_layout_never_moves_an_authored_group(self):
        authored = EntitySpec(name="PlayerBike", kind="vehicle")
        packed = EntitySpec(name="Spectators", kind="crowd", count=20,
                            placement="ring", radius=8)
        schema = _schema([authored, packed])
        apply_scene(schema, _scene(_group(
            "VictorianMonobike", "victorian_monobike.fbx", count=2,
            positions=[[-64, 0, 40], [-65.5, 0, 41]], yaws=[210.0, 210.0])))
        before = [list(p) for p in authored.instance_positions]

        compute_layout(schema)

        assert authored.instance_positions == before
        assert packed.instance_positions      # the rest still gets packed


# ── Tier A in the asset resolver ─────────────────────────────────────────────

@pytest.fixture
def authored_fbx(tmp_path):
    d = tmp_path / "sc"
    (d / "Textures").mkdir(parents=True)
    f = d / "victorian-monobike.fbx"
    f.write_bytes(b"FBX")
    (d / "Textures" / "bike.png").write_bytes(b"PNG")
    return f


class TestTierA:
    def _resolve(self, schema, lookups=""):
        used: dict[str, ResolvedModel] = {}
        return resolve_entity_assets(schema, used, {}, lookups_dir=str(lookups)), used

    def test_authored_model_wins_over_every_catalog_tier(self, authored_fbx):
        e = EntitySpec(name="PlayerBike", kind="vehicle")
        schema = _schema([e])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian-monobike.fbx",
                                          src=str(authored_fbx))))
        paths, used = self._resolve(schema)
        assert paths["PlayerBike"] == "Models/SceneCompiler/Authored/victorian-monobike"
        assert used["authored:victorian-monobike"].source == "scene_compiler"

    def test_authored_picks_take_no_bounds_fit_or_clamp(self, authored_fbx):
        # The author sized it by eye — nothing downstream may resize it.
        schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian-monobike.fbx",
                                          src=str(authored_fbx))))
        _, used = self._resolve(schema)
        m = used["authored:victorian-monobike"]
        assert m.fit_size == 0.0
        assert m.scale == 1.0          # importer scale; layout scale is on the entity

    def test_entities_the_layout_does_not_cover_still_reach_the_catalog(
            self, authored_fbx):
        covered = EntitySpec(name="PlayerBike", kind="vehicle")
        uncovered = EntitySpec(name="Spectators", kind="crowd")
        schema = _schema([covered, uncovered])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian-monobike.fbx",
                                          src=str(authored_fbx))))
        paths, _ = self._resolve(schema)
        assert paths["PlayerBike"].startswith("Models/SceneCompiler/")
        assert "Spectators" not in paths or not paths["Spectators"].startswith(
            "Models/SceneCompiler/")

    def test_textures_ship_under_the_authored_subtree(self, authored_fbx):
        from src.unity_entity_asset_resolver import texture_dirs_with_res_root_for
        schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian-monobike.fbx",
                                          src=str(authored_fbx))))
        _, used = self._resolve(schema)
        pairs = texture_dirs_with_res_root_for(used)
        assert pairs and pairs[0][1] == "Models/SceneCompiler"

    def test_missing_source_falls_through_instead_of_spawning_nothing(self):
        schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        apply_scene(schema, _scene(_group("VictorianMonobike",
                                          "victorian-monobike.fbx",
                                          src="C:/gone/nope.fbx")))
        paths, _ = self._resolve(schema)
        assert not paths.get("PlayerBike", "").startswith("Models/SceneCompiler/")

    def test_no_authored_scene_leaves_resolution_untouched(self):
        schema = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        paths, used = self._resolve(schema)
        assert not any(k.startswith("authored:") for k in used)


# ── declared roles ────────────────────────────────────────────────────────────
# The author names the file `player_monobike.fbx` and the pipeline stops
# guessing: the role binds ahead of keyword overlap, refuses a conflicting
# entity outright, and becomes the bound entity's `kind` — which is what gets
# the model the controller/AI/objective wiring instead of scenery treatment.

class TestDeclaredRole:
    def test_role_binds_where_keywords_never_would(self):
        """The live failure: the LLM called the avatar `Racer`, so no word in
        `victorian_monobike.fbx` reached it and the bike became a prop."""
        e = EntitySpec(name="Racer", kind="player")
        plain = _group("VictorianMonobike", "victorian_monobike.fbx")
        named = _group("PlayerMonobike", "player_monobike.fbx")
        assert bind_score(plain, e) < MIN_BIND_SCORE
        assert bind_score(named, e) >= MIN_BIND_SCORE

    def test_role_outranks_a_keyword_hit_on_the_wrong_entity(self):
        """Two candidates, one lucky word: the role must win the avatar."""
        avatar = EntitySpec(name="Racer", kind="player")
        scenery = EntitySpec(name="MonobikeStatue", kind="prop")
        g = _group("PlayerMonobike", "player_monobike.fbx")
        assert bind_score(g, avatar) > bind_score(g, scenery)

    def test_a_conflicting_role_is_vetoed_not_merely_outscored(self):
        """`enemy_drone.fbx` must never reach the player, however well the
        names happen to line up."""
        player = EntitySpec(name="DroneRider", kind="player")
        assert bind_score(_group("EnemyDrone", "enemy_drone.fbx"), player) == 0.0

    def test_descriptive_kinds_never_veto(self):
        """The player IS a vehicle in a racing game — only player/enemy/npc/
        objective are mutually exclusive jobs."""
        e = EntitySpec(name="PlayerMonobike", kind="vehicle")
        assert bind_score(_group("PlayerMonobike", "player_monobike.fbx"),
                          e) >= MIN_BIND_SCORE

    def test_role_claims_the_entity_the_schema_already_tagged(self):
        """The avatar is whichever entity carries tag=="Player" — that is what
        the generator's player-merge selects on, so it is a far better signal
        than the name or kind the LLM happened to write."""
        s = _schema([EntitySpec(name="Racer", kind="vehicle", tag="Player")])
        apply_scene(s, _scene(_group("PlayerMonobike", "player_monobike.fbx")))
        racer = next(e for e in s.entities if e.name == "Racer")
        assert racer.kind == "player"
        assert "Racer" in authored_models(s)
        assert len(s.entities) == 1, "claimed, not duplicated"

    def test_role_injects_a_tagged_avatar_when_nothing_holds_the_job(self):
        """A schema whose avatar is neither kind='player' nor tagged has no
        entity to claim, so the authored model becomes the avatar itself —
        tagged and count==1, which is exactly what the merge looks for."""
        s = _schema([EntitySpec(name="Racer", kind="vehicle", tag="Untagged")])
        apply_scene(s, _scene(_group("PlayerMonobike", "player_monobike.fbx")))
        avatar = next(e for e in s.entities if e.name == "PlayerMonobike")
        assert (avatar.kind, avatar.tag, avatar.count) == ("player", "Player", 1)
        # Exactly one Player-tagged singleton — two would split the avatar.
        assert sum(1 for e in s.entities if e.tag == "Player") == 1

    def test_unmatched_role_injects_with_its_kind_not_prop(self):
        """The whole point: an authored pickup the LLM never wrote becomes a
        real objective, not set dressing."""
        s = _schema([EntitySpec(name="Track", kind="road")])
        apply_scene(s, _scene(_group("PickupScrap", "pickup_scrap.fbx",
                                     count=3,
                                     positions=[[0, 0, 0], [2, 0, 0], [4, 0, 0]])))
        injected = [e for e in s.entities if e.name == "PickupScrap"]
        assert injected and injected[0].kind == "pickup"
        assert injected[0].count == 3

    def test_roleless_layouts_are_unaffected(self):
        """Additive: a layout written before roles existed binds identically."""
        s = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        apply_scene(s, _scene(_group("VictorianMonobike",
                                     "victorian_monobike.fbx")))
        assert s.entities[0].kind == "vehicle"     # untouched
        assert "PlayerBike" in authored_models(s)

    def test_spill_entities_keep_their_own_job(self):
        """A grid of `player_` bikes feeds the rivals the same MACHINE, never
        the player's role — otherwise the scene has three avatars."""
        s = _schema([
            EntitySpec(name="Racer", kind="player"),
            EntitySpec(name="RivalOne", kind="enemy", model_hint="monobike"),
            EntitySpec(name="RivalTwo", kind="enemy", model_hint="monobike"),
        ])
        apply_scene(s, _scene(_group(
            "PlayerMonobike", "player_monobike.fbx", count=3,
            positions=[[0, 0, 0], [3, 0, 0], [6, 0, 0]])))
        by_name = {e.name: e for e in s.entities}
        assert by_name["Racer"].kind == "player"
        assert by_name["RivalOne"].kind == "enemy"
        assert by_name["RivalTwo"].kind == "enemy"
        # All three ride the authored bike.
        assert set(authored_models(s)) == {"Racer", "RivalOne", "RivalTwo"}

    def test_player_stays_a_singleton_and_the_surplus_is_re_homed(self):
        """count > 1 silently disqualifies the generator's player-merge, so a
        `player_` group that places a whole grid keeps pole position only —
        and the rest still reach the scene."""
        s = _schema([EntitySpec(name="Racer", kind="player")])
        msgs = apply_scene(s, _scene(_group(
            "PlayerMonobike", "player_monobike.fbx", count=3,
            positions=[[0, 0, 0], [3, 0, 0], [6, 0, 0]])))
        racer = next(e for e in s.entities if e.name == "Racer")
        assert racer.count == 1
        assert racer.instance_positions == [[0.0, 0.0, 0.0]]
        extra = next(e for e in s.entities if e.name.endswith("Extra"))
        assert extra.count == 2
        assert extra.kind == "prop"
        assert extra.instance_positions == [[3.0, 0.0, 0.0], [6.0, 0.0, 0.0]]
        assert any("pole position" in m for m in msgs)


# ── uncovered entities ────────────────────────────────────────────────────────

class TestUncoveredReport:
    def test_entities_the_layout_misses_are_named(self):
        """Catalog fallback is by design, but silent it reads as 'the pipeline
        output models I never chose'."""
        s = _schema([
            EntitySpec(name="PlayerBike", kind="vehicle"),
            EntitySpec(name="Grandstands", kind="building", count=6),
            EntitySpec(name="Marshals", kind="npc", count=4),
        ])
        msgs = apply_scene(s, _scene(_group("PlayerBike", "player_bike.fbx")))
        report = next(m for m in msgs if "NOT in your layout" in m)
        assert "Grandstands(building x6)" in report
        assert "Marshals(npc x4)" in report
        assert "PlayerBike" not in report
        assert s.meta["authored_scene"]["uncovered"] == 2

    def test_a_fully_authored_scene_reports_nothing(self):
        s = _schema([EntitySpec(name="PlayerBike", kind="vehicle")])
        msgs = apply_scene(s, _scene(_group("PlayerBike", "player_bike.fbx")))
        assert not any("NOT in your layout" in m for m in msgs)
