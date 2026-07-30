"""Tests for archetype-selected spatial layout (src/unity_layout_archetype.py
+ the VerticalCorridor primitive in src/unity_layout.py).

The bug being closed: the layout pass packed every game onto one ground plane,
so a skydiving schema — altitude-banded content, a player who exits an aircraft
at 4 km — came out flattened with everything at y ≈ 0.

Four contracts are locked here:
  1. A skydiving schema selects VerticalCorridor and actually gets a corridor,
     with the exit platform (and the player attached to it) at the top.
  2. A free-medium schema (fly/swim, no descent framing) selects BoundedVolume
     and gets a play box — it must not hard-fail, and must not flatten.
  3. A planar/ground schema still selects ArenaPlane and reproduces the
     pre-change layout EXACTLY (tests/fixtures/arenaplane_baseline.json).
  4. Anything else abstains with [layout.strict] — never a silent fall-through.
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path

import pytest

from src.unity_layout import compute_layout, terrain_bounds
from src.unity_layout_archetype import (
    LayoutStrictError,
    corridor_height,
    pick_exit_platform,
    select_archetype,
    volume_height,
)
from src.unity_racetrack import compose_racetrack
from src.unity_schema import EntitySpec, UnitySchema

_FIXTURES = Path(__file__).parent / "fixtures"


# ── Fixture builders ─────────────────────────────────────────────────────────

def _load(name: str) -> UnitySchema:
    return UnitySchema.model_validate(
        json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _skydive_schema() -> UnitySchema:
    """The DropZone skydiving fixture (derived from prompts/dropzone.txt)."""
    return _load("dropzone_skydive.schema.json")


def _glider_schema() -> UnitySchema:
    """fly locomotion, no descent framing — the BoundedVolume air fixture.
    Before BoundedVolume existed this schema hard-failed [layout.strict]."""
    return _load("skyline_glider.schema.json")


def _survey_schema() -> UnitySchema:
    """swim locomotion, no descent framing — the BoundedVolume water fixture."""
    return _load("lagoon_survey.schema.json")


def _planar_schema() -> UnitySchema:
    """Plain 3D ground game — the ArenaPlane regression subject. Must stay
    identical to the shape recorded in arenaplane_baseline.json."""
    return UnitySchema(
        project_name="RustbeltDepotPlanar", company_name="C",
        description=("A third-person scavenger game set in a fenced rustbelt "
                     "depot yard. The player walks between cargo containers, "
                     "collects scrap, and fights off patrolling drones."),
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear", locomotion="walk",
        features=[], scenes=["Main"], packages=[],
        entities=[
            EntitySpec(name="Player", kind="character", count=1,
                       placement="single", tag="Player", scale=1.0),
            EntitySpec(name="Containers", kind="prop", count=14,
                       placement="scatter", radius=8, spacing=2.0, scale=3.0),
            EntitySpec(name="Drones", kind="enemy", count=6,
                       placement="ring", radius=10, scale=1.2),
            EntitySpec(name="ScrapPiles", kind="prop", count=9,
                       placement="grid", spacing=2.5, scale=0.8),
            EntitySpec(name="Floodlights", kind="prop", count=5,
                       placement="line", spacing=6.0, scale=2.0),
        ],
    )


def _racing_schema() -> UnitySchema:
    """ArenaPlane's path-corridor variant (racetrack scaffold)."""
    s = UnitySchema(
        project_name="TurboToyRacerPlanar", company_name="C",
        description="A kart racing game around a closed circuit.",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear", locomotion="drive",
        features=[], scenes=["Main"], packages=[], entities=[])
    s.meta["genre"] = "racing"
    compose_racetrack(s)
    s.entities.append(EntitySpec(name="Crates", count=12, placement="scatter",
                                 radius=6, spacing=2.0, scale=1.5))
    return s


def _dive_schema() -> UnitySchema:
    """vertical + water: a freediving descent to a reef floor."""
    return UnitySchema(
        project_name="DeepDive", company_name="C",
        description=("A freediving game. The player dives from the surface and "
                     "descends past a coral reef to the seabed 80m below, "
                     "then ascends before their breath runs out."),
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear", locomotion="swim",
        features=[], scenes=["Main"], packages=[],
        entities=[
            EntitySpec(name="Player", kind="character", count=1,
                       placement="single", tag="Player"),
            EntitySpec(name="ReefFish", kind="enemy", count=8,
                       placement="scatter", radius=6, scale=0.5,
                       model_hint="tropical fish shoal"),
            EntitySpec(name="CoralHeads", kind="prop", count=5,
                       placement="ring", radius=8, scale=2.0,
                       model_hint="coral outcrop"),
        ],
    )


def _llm_authored_schema() -> UnitySchema:
    """A REAL schema-fill output, captured verbatim from a live unity-create
    run (prompts/skyring_descent.txt). Its role vocabulary is nothing like the
    hand-written fixtures — kind='prop' with the meaning in `tag`, generic
    names, kind='structure' for the jump plane — and every hand-written
    fixture passed while this shape laid out completely wrong."""
    return _load("dropzone_arcade_llm.schema.json")


def _entity(schema: UnitySchema, name: str) -> EntitySpec:
    return next(e for e in schema.entities if e.name == name)


# ── 1. Selection ─────────────────────────────────────────────────────────────

class TestArchetypeSelection:
    def test_skydiving_schema_selects_vertical_corridor(self):
        arch = select_archetype(_skydive_schema())
        assert arch.name == "VerticalCorridor"
        assert arch.motion_axis == "vertical"
        assert arch.medium == "air"
        assert arch.goal_topology == "reach_point"

    def test_planar_ground_schema_selects_arena_plane(self):
        arch = select_archetype(_planar_schema())
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "ArenaPlane", "planar", "ground")

    def test_racing_circuit_is_not_a_path_axis(self):
        """A freely driven circuit is not on rails — path entities in the
        schema must not push a racing game into the (unimplemented) spline
        primitive and hard-fail every racetrack."""
        arch = select_archetype(_racing_schema())
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "ArenaPlane", "planar", "ground")

    def test_dive_schema_selects_vertical_corridor_in_water(self):
        arch = select_archetype(_dive_schema())
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "VerticalCorridor", "vertical", "water")

    def test_free_flight_selects_bounded_volume(self):
        arch = select_archetype(_glider_schema())
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "BoundedVolume", "volumetric", "air")

    def test_free_swim_selects_bounded_volume(self):
        arch = select_archetype(_survey_schema())
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "BoundedVolume", "volumetric", "water")

    def test_descent_evidence_still_beats_free_medium_locomotion(self):
        """The skydiving fixture is locomotion='fly' too — descent framing is
        what separates a corridor from a box, and it is checked first."""
        assert select_archetype(_skydive_schema()).name == "VerticalCorridor"
        assert select_archetype(_dive_schema()).name == "VerticalCorridor"

    def test_hover_vocabulary_reaches_volumetric_without_the_enum(self):
        """UnitySchema.locomotion cannot say "hover" — it normalises to walk.
        The vocabulary carries it instead."""
        s = _planar_schema()
        s.description = ("A drone courier game: the quadcopter hovers between "
                         "rooftop pads high in the open sky above the yard.")
        arch = select_archetype(s)
        assert (arch.name, arch.motion_axis, arch.medium) == (
            "BoundedVolume", "volumetric", "air")

    def test_setting_vocabulary_never_lifts_a_walker_off_the_ground(self):
        """A walker described in sky words gets the GROUND plane.

        This previously abstained, on the theory that "planar + air" was an
        unmatched combination. That was wrong twice over: ArenaPlane is the
        correct primitive for a walking game, not a wrong one, and the abstain
        hard-failed real builds off a single incidental word — "atmosphere",
        then "abyss", both in the same climbing-horror concept. Setting
        vocabulary describes the scene; `locomotion` says what the player
        moves through, and it wins."""
        s = _planar_schema()
        s.description = ("A scavenger game on a sky island: the player walks "
                         "between cargo containers under an open sky, with "
                         "cloud banks far overhead and the abyss below.")
        s.locomotion = "walk"
        arch = select_archetype(s)
        assert (arch.name, arch.medium) == ("ArenaPlane", "ground"), arch.evidence

    def test_prose_can_still_name_the_medium_when_it_says_the_player_floats(self):
        """The one case the `locomotion` enum cannot express — it normalises
        hover/float to "walk" — still reaches a volume."""
        s = _planar_schema()
        s.description = ("A drone courier game: the quadcopter hovers between "
                         "rooftop pads high in the open sky above the yard.")
        s.locomotion = "walk"
        arch = select_archetype(s)
        assert (arch.name, arch.medium) == ("BoundedVolume", "air"), arch.evidence

    def test_selection_is_deterministic(self):
        a, b = select_archetype(_skydive_schema()), select_archetype(_skydive_schema())
        assert (a.name, a.motion_axis, a.medium, a.goal_topology, a.evidence) == \
               (b.name, b.motion_axis, b.medium, b.goal_topology, b.evidence)

    def test_one_ambiguous_descent_verb_does_not_flip_a_ground_game(self):
        """"A descent into the catacombs" is prose, not a drop corridor.
        Flipping on it would hard-fail (vertical+ground) a healthy game."""
        s = _planar_schema()
        s.description = ("A dungeon crawler about the descent into the "
                         "catacombs beneath the depot yard.")
        assert select_archetype(s).name == "ArenaPlane"

    def test_provenance_names_the_archetype_and_all_three_axes(self):
        line = select_archetype(_skydive_schema()).provenance()
        assert "VerticalCorridor" in line
        for field in ("motion_axis=vertical", "medium=air",
                      "goal_topology=reach_point"):
            assert field in line, line


class TestAbstain:
    @pytest.mark.parametrize("locomotion", ["fly", "swim"])
    def test_free_medium_locomotion_no_longer_hard_fails(self, locomotion):
        """The regression BoundedVolume exists to fix: these projects used to
        produce (wrong) output, then hard-failed, and now lay out properly."""
        s = _planar_schema()
        s.locomotion = locomotion
        msgs = compute_layout(s)
        assert msgs[0].startswith("Layout archetype BoundedVolume selected")
        assert all(e.instance_positions for e in s.entities)

    def test_abstain_message_names_the_derivation_and_a_fix(self):
        s = _planar_schema()
        _entity(s, "Drones").instance_positions = [[0.0, 800.0, 0.0]]  # vertical+ground
        with pytest.raises(LayoutStrictError) as exc:
            compute_layout(s)
        msg = str(exc.value)
        assert "Derived from:" in msg
        assert "Fix:" in msg
        assert "vertical+air -> VerticalCorridor" in msg
        assert "volumetric+air -> BoundedVolume" in msg
        assert "planar+ground -> ArenaPlane" in msg

    def test_rail_shooter_abstains_rather_than_becoming_an_arena(self):
        s = _planar_schema()
        s.description = ("An on-rails shooter: the player rides the rail "
                         "through the depot yard shooting drones.")
        with pytest.raises(LayoutStrictError) as exc:
            compute_layout(s)
        assert "motion_axis='path'" in str(exc.value)

    def test_vertical_ground_abstains(self):
        """Altitude-banded content in a scene that never says air or water:
        descent through solid ground has no primitive."""
        s = _planar_schema()
        _entity(s, "Drones").instance_positions = [[0.0, 800.0, 0.0]]
        with pytest.raises(LayoutStrictError) as exc:
            compute_layout(s)
        assert "motion_axis='vertical' + medium='ground'" in str(exc.value)

    def test_abstain_never_silently_lays_the_scene_out(self):
        s = _planar_schema()
        s.description = ("An on-rails shooter through the depot yard.")
        with pytest.raises(LayoutStrictError):
            compute_layout(s)
        for e in s.entities:
            assert e.instance_positions == []
            assert e.origin_offset == [0.0, 0.0]


# ── 2. VerticalCorridor ──────────────────────────────────────────────────────

class TestVerticalCorridor:
    def test_corridor_height_comes_from_the_schema(self):
        h, why, defaulted = corridor_height(_skydive_schema())
        assert defaulted is False
        assert h == 1500.0            # "pull-altitude alarm at 1500m"
        assert "1500m" in why

    def test_corridor_height_falls_back_to_a_named_default_with_a_warning(self, caplog):
        s = _skydive_schema()
        # Strip every altitude figure from the schema's prose.
        s.description = ("A skydiving game: the player exits the aircraft, "
                         "freefalls, and deploys a parachute canopy.")
        s.scripts, s.missions, s.story_beats = [], [], []
        h, _why, defaulted = corridor_height(s)
        assert defaulted is True
        assert h == 4000.0            # _CORRIDOR_HEIGHT_DEFAULT
        with caplog.at_level("WARNING"):
            compute_layout(s)
        assert any("named default corridor height" in r.message
                   for r in caplog.records), caplog.text

    def test_player_spawns_at_the_top_of_the_corridor(self):
        s = _skydive_schema()
        h, _why, _d = corridor_height(s)
        compute_layout(s)
        player = _entity(s, "Player")
        assert player.instance_positions, "player never got a baked spawn"
        assert all(p[1] > 0.8 * h for p in player.instance_positions), \
            player.instance_positions

    def test_no_enemy_is_left_on_the_ground(self):
        """The flattening bug in one assertion: formation AI skydivers belong
        in the corridor, not standing on the landing pad."""
        s = _skydive_schema()
        compute_layout(s)
        for e in s.entities:
            if e.kind != "enemy":
                continue
            for p in e.instance_positions:
                assert p[1] >= 50.0, f"{e.name} instance at y={p[1]}"

    def test_content_is_stratified_not_stacked_on_one_plane(self):
        s = _skydive_schema()
        compute_layout(s)
        corridor_ys = {round(e.instance_positions[0][1], 3)
                       for e in s.entities
                       if e.instance_positions and e.name not in
                       ("LandingPad", "CrowdStands")}
        assert len(corridor_ys) >= 4, corridor_ys
        assert 0.0 not in corridor_ys

    def test_goal_sits_on_the_terminal_plane_under_the_drop_axis(self):
        s = _skydive_schema()
        compute_layout(s)
        pad = _entity(s, "LandingPad")
        assert pad.origin_offset == [0.0, 0.0]
        assert [p[1] for p in pad.instance_positions] == [0.0]

    def test_a_mid_air_target_is_not_dragged_down_to_the_pad(self):
        """"FormationTarget" reads as a goal to a naive keyword gate — it is
        corridor content and must stay at altitude."""
        s = _skydive_schema()
        compute_layout(s)
        assert _entity(s, "FormationTarget").instance_positions[0][1] > 0.0

    def test_groups_are_parked_beside_the_fall_line_not_on_it(self):
        """The player must fall PAST stratified content, not through it. The
        fall line is the player's own spawn column — which the exit platform
        role moves off the world origin, since the platform itself occupies
        the axis and the player stands in its doorway."""
        s = _skydive_schema()
        compute_layout(s)
        px, _py, pz = _entity(s, "Player").instance_positions[0]
        exempt = {"Player", "LandingPad", "CessnaCaravan"}   # platform + terminal
        for e in s.entities:
            if e.name in exempt or not e.instance_positions:
                continue
            cx, cz = e.origin_offset
            d = ((cx - px) ** 2 + (cz - pz) ** 2) ** 0.5
            assert d >= e.group_radius, f"{e.name} straddles the fall line"

    def test_ground_plane_still_covers_the_corridor_footprint(self):
        s = _skydive_schema()
        compute_layout(s)
        xmin, zmin, xmax, zmax = terrain_bounds(s.entities, s.locomotion)
        for e in s.entities:
            if e.group_radius <= 0.0:
                continue
            cx, cz = e.origin_offset
            assert xmin <= cx - e.group_radius and xmax >= cx + e.group_radius
            assert zmin <= cz - e.group_radius and zmax >= cz + e.group_radius

    def test_corridor_layout_is_deterministic(self):
        a, b = _skydive_schema(), _skydive_schema()
        compute_layout(a)
        compute_layout(b)
        assert [e.model_dump() for e in a.entities] == \
               [e.model_dump() for e in b.entities]

    def test_corridor_layout_is_idempotent(self):
        s = _skydive_schema()
        compute_layout(s)
        first = [e.model_dump() for e in s.entities]
        compute_layout(s)
        assert [e.model_dump() for e in s.entities] == first

    def test_water_medium_hangs_its_inhabitants_below_the_surface(self):
        s = _dive_schema()
        msgs = compute_layout(s)
        fish = _entity(s, "ReefFish")
        coral = _entity(s, "CoralHeads")
        assert all(p[1] < 0.0 for p in fish.instance_positions), \
            fish.instance_positions
        assert all(p[1] < 0.0 for p in coral.instance_positions), \
            coral.instance_positions
        assert any("Water surface plane" in m for m in msgs), msgs

    def test_water_player_still_spawns_at_the_top_of_the_corridor(self):
        s = _dive_schema()
        h, _why, _d = corridor_height(s)
        compute_layout(s)
        assert _entity(s, "Player").instance_positions[0][1] > 0.8 * h


class TestExitPlatform:
    """The ordering bug: stratification sorts by footprint size, so the Cessna
    the player jumps FROM landed at y=575 while the player spawned at 1500 —
    a jump ship parked below its own jumper."""

    def test_jump_ship_is_never_below_the_player(self):
        s = _skydive_schema()
        compute_layout(s)
        cessna = _entity(s, "CessnaCaravan").instance_positions[0][1]
        player = _entity(s, "Player").instance_positions[0][1]
        assert cessna >= player, f"Cessna at y={cessna}, player at y={player}"

    def test_player_spawn_is_attached_to_the_platform(self):
        s = _skydive_schema()
        compute_layout(s)
        cessna = _entity(s, "CessnaCaravan")
        player = _entity(s, "Player")
        gap = math_dist_xz(player.origin_offset, cessna.origin_offset)
        # Clear of the platform's own footprint, but adjacent to it — not
        # parked in some unrelated corner of the corridor.
        assert cessna.group_radius <= gap <= (cessna.group_radius
                                              + player.group_radius + 5.0), gap

    def test_provenance_names_the_platform_and_the_signal(self):
        msgs = compute_layout(_skydive_schema())
        line = next((m for m in msgs if "Exit platform" in m), "")
        assert "CessnaCaravan" in line, msgs
        assert "signal:" in line, line
        assert "relation" in line, line   # the description states it outright

    def test_everything_else_stratifies_below_the_platform(self):
        s = _skydive_schema()
        compute_layout(s)
        top = _entity(s, "CessnaCaravan").instance_positions[0][1]
        for e in s.entities:
            if e.name in ("CessnaCaravan", "Player"):
                continue
            for p in e.instance_positions:
                assert p[1] < top, f"{e.name} at y={p[1]} is above the platform"

    def test_kind_signal_is_used_when_no_relation_is_stated(self):
        s = _skydive_schema()
        s.description = "A skydiving game with freefall and a parachute canopy."
        s.missions, s.story_beats = [], []
        _entity(s, "Player").behavior = "freefall and canopy poses"
        plat, why = pick_exit_platform(s, _entity(s, "Player"),
                                       [_entity(s, "CessnaCaravan"),
                                        _entity(s, "FormationDivers")], "air")
        assert plat is not None and plat.name == "CessnaCaravan"
        assert why.startswith("kind")

    def test_vocabulary_signal_is_the_last_resort(self):
        """No relation, and the kind signal is off the table (water corridor):
        the name/hint vocabulary still finds the boat you dive from."""
        s = _dive_schema()
        boat = EntitySpec(name="DiveBoat", kind="prop", count=1,
                          placement="single", model_hint="dive platform hull")
        s.entities.append(boat)
        plat, why = pick_exit_platform(s, _entity(s, "Player"), [boat], "water")
        assert plat is boat
        assert why.startswith("vocabulary")

    def test_at_most_one_entity_takes_the_role(self):
        s = _skydive_schema()
        s.entities.append(EntitySpec(name="JumpBalloon", kind="vehicle",
                                     count=1, placement="single",
                                     model_hint="hot air balloon"))
        compute_layout(s)
        tops = [e.name for e in s.entities
                if e.instance_positions
                and e.instance_positions[0][1] >= 1500.0
                and e.name != "Player"]
        assert len(tops) == 1, tops

    def test_no_platform_leaves_the_player_on_the_drop_axis(self):
        """No warning, no role — an unattached spawn at the top is fine."""
        s = _skydive_schema()
        s.entities = [e for e in s.entities if e.name != "CessnaCaravan"]
        s.description = "A skydiving game: freefall, parachute canopy, 1500m pull."
        s.missions, s.story_beats = [], []
        msgs = compute_layout(s)
        assert not any("Exit platform" in m for m in msgs), msgs
        assert _entity(s, "Player").origin_offset == [0.0, 0.0]


def math_dist_xz(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ── 2b. BoundedVolume ────────────────────────────────────────────────────────

class TestBoundedVolume:
    def test_volume_height_comes_from_the_schema(self):
        h, why, defaulted = volume_height(_glider_schema())
        assert defaulted is False
        assert h == 900.0            # "the ceiling of the flyable airspace is 900m"
        assert "900m" in why

    def test_volume_height_falls_back_to_a_named_default_with_a_warning(self, caplog):
        s = _survey_schema()         # no altitude figure anywhere
        h, _why, defaulted = volume_height(s)
        assert defaulted is True
        assert h == 200.0            # _VOLUME_HEIGHT_DEFAULT
        with caplog.at_level("WARNING"):
            compute_layout(s)
        assert any("named default box height" in r.message
                   for r in caplog.records), caplog.text

    def test_air_volume_puts_nothing_on_the_floor_but_ground_dwellers(self):
        s = _glider_schema()
        compute_layout(s)
        for e in s.entities:
            on_floor = all(p[1] == 0.0 for p in e.instance_positions)
            if on_floor:
                assert e.kind == "terrain", f"{e.name} defaulted to the floor"

    def test_air_volume_spreads_content_through_the_interior(self):
        s = _glider_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        interior = {round(e.instance_positions[0][1], 3) for e in s.entities
                    if e.instance_positions and e.instance_positions[0][1] > 0.0}
        assert len(interior) >= 3, interior
        assert all(0.0 < y < h for y in interior), interior

    def test_player_is_inside_the_volume_not_on_a_boundary(self):
        s = _glider_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        py = _entity(s, "Player").instance_positions[0][1]
        assert 0.0 < py < h, py

    def test_water_volume_has_a_surface_and_everything_swims_below_it(self):
        s = _survey_schema()
        h, _why, _d = volume_height(s)
        msgs = compute_layout(s)
        assert any("Water surface plane" in m for m in msgs), msgs
        for e in s.entities:
            if e.name == "SurveyBoat":
                assert [p[1] for p in e.instance_positions] == [h]
                continue
            for p in e.instance_positions:
                assert p[1] < h, f"{e.name} at y={p[1]} is not below the surface"
                assert p[1] > 0.0, f"{e.name} at y={p[1]} sank to the seabed"

    def test_surface_marked_content_rides_the_surface(self):
        s = _survey_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        assert _entity(s, "SurveyBoat").instance_positions[0][1] == h

    def test_layers_reuse_the_existing_packer_within_each_altitude(self):
        """Groups sharing a layer must not overlap in XZ; groups in DIFFERENT
        layers may, because they are separated vertically."""
        s = _glider_schema()
        compute_layout(s)
        by_layer: dict[float, list[EntitySpec]] = {}
        for e in s.entities:
            if not e.instance_positions or e.name == "Player":
                continue
            by_layer.setdefault(round(e.instance_positions[0][1], 3), []).append(e)
        for y, group in by_layer.items():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    d = math_dist_xz(a.origin_offset, b.origin_offset)
                    assert d >= a.group_radius + b.group_radius - 1e-6, \
                        f"{a.name} and {b.name} overlap at y={y}"

    @pytest.mark.parametrize("builder", [_glider_schema, _survey_schema])
    def test_volume_layout_is_deterministic_and_idempotent(self, builder):
        a, b = builder(), builder()
        compute_layout(a)
        compute_layout(b)
        assert [e.model_dump() for e in a.entities] == \
               [e.model_dump() for e in b.entities]
        first = [e.model_dump() for e in a.entities]
        compute_layout(a)
        assert [e.model_dump() for e in a.entities] == first

    @pytest.mark.parametrize("builder", [_glider_schema, _survey_schema])
    def test_ground_plane_covers_the_volume_footprint(self, builder):
        s = builder()
        compute_layout(s)
        xmin, zmin, xmax, zmax = terrain_bounds(s.entities, s.locomotion)
        for e in s.entities:
            if e.group_radius <= 0.0:
                continue
            cx, cz = e.origin_offset
            assert xmin <= cx - e.group_radius and xmax >= cx + e.group_radius
            assert zmin <= cz - e.group_radius and zmax >= cz + e.group_radius

class TestLiveLLMSchemaRoles:
    """Role detection against a REAL schema-fill output, not a fixture written
    to match the predicates. Every one of these assertions failed on the live
    run that produced this schema: the role predicates keyed off `kind` and a
    vocabulary tuned to hand-written fixtures, while the LLM put the meaning in
    `tag` and reached for generic kinds ('prop', 'structure', 'mechanism')."""

    def test_player_is_found_without_the_player_tag(self):
        """kind='prop', tag='Untagged', name='Player' — the old gate demanded
        tag=='Player' AND an avatar kind, found nothing, and stratified the
        avatar at 475 m in a corridor that promises the top."""
        s = _llm_authored_schema()
        h, _why, _d = corridor_height(s)
        compute_layout(s)
        assert _entity(s, "Player").instance_positions[0][1] > 0.8 * h

    def test_jump_plane_is_the_exit_platform_despite_kind_structure(self):
        s = _llm_authored_schema()
        msgs = compute_layout(s)
        assert any("Exit platform 'TurbopropExit'" in m for m in msgs), msgs
        plane = _entity(s, "TurbopropExit").instance_positions[0][1]
        player = _entity(s, "Player").instance_positions[0][1]
        assert plane >= player

    def test_terrain_tagged_ground_lands_on_the_terminal_plane(self):
        """kind='prop', tag='Ground', name='ValleyFloor' — floated at 1466 m."""
        s = _llm_authored_schema()
        compute_layout(s)
        floor = _entity(s, "ValleyFloor")
        assert floor.instance_positions
        assert all(p[1] == 0.0 for p in floor.instance_positions), \
            sorted({p[1] for p in floor.instance_positions})

    def test_environment_tagged_scenery_is_not_dragged_to_the_floor(self):
        """The fix's own over-reach, caught on the same schema: "Environment"
        is Unity's catch-all scenery tag, not a claim about the ground — it was
        carrying the cloud layer of a skydiving game."""
        s = _llm_authored_schema()
        compute_layout(s)
        assert all(p[1] > 0.0 for p in _entity(s, "Clouds").instance_positions)

    def test_the_whole_scene_is_not_flattened(self):
        s = _llm_authored_schema()
        compute_layout(s)
        ys = {round(p[1], 1) for e in s.entities for p in e.instance_positions}
        assert len(ys) >= 6, ys

    def test_live_schema_layout_is_deterministic(self):
        a, b = _llm_authored_schema(), _llm_authored_schema()
        compute_layout(a)
        compute_layout(b)
        assert [e.model_dump() for e in a.entities] == \
               [e.model_dump() for e in b.entities]


# ── 2b. BoundedVolume ────────────────────────────────────────────────────────

class TestBoundedVolume:
    def test_volume_height_comes_from_the_schema(self):
        h, why, defaulted = volume_height(_glider_schema())
        assert defaulted is False
        assert h == 900.0            # "the ceiling of the flyable airspace is 900m"
        assert "900m" in why

    def test_volume_height_falls_back_to_a_named_default_with_a_warning(self, caplog):
        s = _survey_schema()         # no altitude figure anywhere
        h, _why, defaulted = volume_height(s)
        assert defaulted is True
        assert h == 200.0            # _VOLUME_HEIGHT_DEFAULT
        with caplog.at_level("WARNING"):
            compute_layout(s)
        assert any("named default box height" in r.message
                   for r in caplog.records), caplog.text

    def test_air_volume_puts_nothing_on_the_floor_but_ground_dwellers(self):
        s = _glider_schema()
        compute_layout(s)
        for e in s.entities:
            on_floor = all(p[1] == 0.0 for p in e.instance_positions)
            if on_floor:
                assert e.kind == "terrain", f"{e.name} defaulted to the floor"

    def test_air_volume_spreads_content_through_the_interior(self):
        s = _glider_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        interior = {round(e.instance_positions[0][1], 3) for e in s.entities
                    if e.instance_positions and e.instance_positions[0][1] > 0.0}
        assert len(interior) >= 3, interior
        assert all(0.0 < y < h for y in interior), interior

    def test_player_is_inside_the_volume_not_on_a_boundary(self):
        s = _glider_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        py = _entity(s, "Player").instance_positions[0][1]
        assert 0.0 < py < h, py

    def test_water_volume_has_a_surface_and_everything_swims_below_it(self):
        s = _survey_schema()
        h, _why, _d = volume_height(s)
        msgs = compute_layout(s)
        assert any("Water surface plane" in m for m in msgs), msgs
        for e in s.entities:
            if e.name == "SurveyBoat":
                assert [p[1] for p in e.instance_positions] == [h]
                continue
            for p in e.instance_positions:
                assert p[1] < h, f"{e.name} at y={p[1]} is not below the surface"
                assert p[1] > 0.0, f"{e.name} at y={p[1]} sank to the seabed"

    def test_surface_marked_content_rides_the_surface(self):
        s = _survey_schema()
        h, _why, _d = volume_height(s)
        compute_layout(s)
        assert _entity(s, "SurveyBoat").instance_positions[0][1] == h

    def test_layers_reuse_the_existing_packer_within_each_altitude(self):
        """Groups sharing a layer must not overlap in XZ; groups in DIFFERENT
        layers may, because they are separated vertically."""
        s = _glider_schema()
        compute_layout(s)
        by_layer: dict[float, list[EntitySpec]] = {}
        for e in s.entities:
            if not e.instance_positions or e.name == "Player":
                continue
            by_layer.setdefault(round(e.instance_positions[0][1], 3), []).append(e)
        for y, group in by_layer.items():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    d = math_dist_xz(a.origin_offset, b.origin_offset)
                    assert d >= a.group_radius + b.group_radius - 1e-6, \
                        f"{a.name} and {b.name} overlap at y={y}"

    @pytest.mark.parametrize("builder", [_glider_schema, _survey_schema])
    def test_volume_layout_is_deterministic_and_idempotent(self, builder):
        a, b = builder(), builder()
        compute_layout(a)
        compute_layout(b)
        assert [e.model_dump() for e in a.entities] == \
               [e.model_dump() for e in b.entities]
        first = [e.model_dump() for e in a.entities]
        compute_layout(a)
        assert [e.model_dump() for e in a.entities] == first

    @pytest.mark.parametrize("builder", [_glider_schema, _survey_schema])
    def test_ground_plane_covers_the_volume_footprint(self, builder):
        s = builder()
        compute_layout(s)
        xmin, zmin, xmax, zmax = terrain_bounds(s.entities, s.locomotion)
        for e in s.entities:
            if e.group_radius <= 0.0:
                continue
            cx, cz = e.origin_offset
            assert xmin <= cx - e.group_radius and xmax >= cx + e.group_radius
            assert zmin <= cz - e.group_radius and zmax >= cz + e.group_radius


# ── 3. ArenaPlane is untouched ───────────────────────────────────────────────

class TestArenaPlaneUnchanged:
    """The pre-change recording. `arenaplane_baseline.json` was captured from
    the commit BEFORE archetype selection existed — see tests/fixtures/README.md.
    """

    @staticmethod
    def _baseline() -> dict:
        return json.loads((_FIXTURES / "arenaplane_baseline.json")
                          .read_text(encoding="utf-8"))

    @pytest.mark.parametrize("key,builder", [
        ("planar", _planar_schema),
        ("racing", _racing_schema),
    ])
    def test_layout_matches_the_pre_change_recording(self, key, builder):
        want = self._baseline()[key]
        s = builder()
        compute_layout(s)
        assert [e.model_dump() for e in s.entities] == want["entities"]
        assert (s.world_spec.model_dump() if s.world_spec else None) == \
            want["world_spec"]
        assert list(terrain_bounds(s.entities, s.locomotion)) == \
            want["terrain_bounds"]

    def test_generated_project_bytes_are_unchanged(self):
        """The real output: the whole generated Assets/ tree, hashed.

        Straight from the schema — the generator's own model-aware re-layout
        (unity_generator._relayout) is what calls compute_layout here, so this
        covers archetype selection on the path a real build takes.
        """
        from src.unity_generator import UnityProjectGenerator
        from src.unity_resolver import UnityResolver

        s = _planar_schema()
        resolver = UnityResolver(lookups_dir="lookups")
        s = resolver.inject_required_packages(resolver.resolve_schema(s))
        deps = resolver.build_manifest_deps(s, inject_mcp_bridge=True)
        gen = UnityProjectGenerator(template_dir="templates/unity")
        with tempfile.TemporaryDirectory() as td:
            root, _n = gen.write(s, deps, td, strict_assets=False)
            h = hashlib.sha256()
            for f in sorted(Path(root).rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(root)).replace("\\", "/").encode())
                    h.update(f.read_bytes())
        # Not vacuous: the generator really did lay the scene out on the way.
        assert all(e.instance_positions for e in s.entities)
        assert h.hexdigest() == self._baseline()["planar_project_sha256"]


# ── 4. Provenance on every run ───────────────────────────────────────────────

class TestProvenance:
    @pytest.mark.parametrize("builder,expected", [
        (_planar_schema, "ArenaPlane"),
        (_racing_schema, "ArenaPlane"),
        (_skydive_schema, "VerticalCorridor"),
        (_dive_schema, "VerticalCorridor"),
        (_glider_schema, "BoundedVolume"),
        (_survey_schema, "BoundedVolume"),
    ])
    def test_every_run_logs_the_archetype_and_why(self, builder, expected):
        msgs = compute_layout(builder())
        assert msgs, "layout pass emitted no provenance at all"
        head = msgs[0]
        assert head.startswith(f"Layout archetype {expected} selected")
        for field in ("motion_axis=", "medium=", "goal_topology="):
            assert field in head, head

    def test_2d_projects_skip_selection_entirely(self):
        """2D builds its own layout at spawn time — it must never be asked to
        justify a 3D spatial primitive (and never abstain on one)."""
        for gt in ("2D", "2d_topdown"):
            s = _planar_schema()
            s.game_type = gt
            assert compute_layout(s) == []

    def test_schema_with_nothing_to_place_skips_selection(self):
        s = _planar_schema()
        s.entities = []
        assert compute_layout(s) == []
