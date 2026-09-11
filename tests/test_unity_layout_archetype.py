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

import json
import math
import tempfile
from pathlib import Path

import pytest

from src.unity_layout import (
    _hold_chain,
    _reach_distance,
    _verify_hold_chain,
    compute_layout,
    terrain_bounds,
)
from src.unity_layout_archetype import (
    LayoutStrictError,
    corridor_height,
    is_climbable_entity,
    is_formation_entity,
    is_hazard_entity,
    pick_exit_platform,
    select_archetype,
    spatial_relationship_of,
    surface_height,
    volume_height,
)
from src.unity_racetrack import compose_racetrack
from src.unity_schema import CourseSpec, EntitySpec, RouteZoneSpec, UnitySchema, WorldSpec

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


def _llm_authored_schema() -> UnitySchema:
    """A REAL schema-fill output, captured verbatim from a live unity-create
    run (prompts/skyring_descent.txt). Its role vocabulary is nothing like the
    hand-written fixtures — kind='prop' with the meaning in `tag`, generic
    names, kind='structure' for the jump plane — and every hand-written
    fixture passed while this shape laid out completely wrong."""
    return _load("dropzone_arcade_llm.schema.json")


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
    # kind="vehicle" -> spatial_relationship_of() == "formation" (a cohesive
    # pack that stays at one anchor) even though it's not preplaced like
    # compose_racetrack's own PlayerKart/OpponentKarts -- the one packable,
    # non-scenery group this fixture needs so course-zone tests still have
    # something that goes through `_corridor_pack` + zone-elevation
    # application instead of `_scenery_distributed_pack` (which doesn't
    # interpret zones yet, by design -- see its docstring).
    s.entities.append(EntitySpec(name="RivalDrones", kind="vehicle", count=4,
                                 placement="scatter", radius=6, spacing=2.0,
                                 scale=1.0))
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


def _entity(schema: UnitySchema, name: str) -> EntitySpec:
    return next(e for e in schema.entities if e.name == name)


def _hold_positions(schema: UnitySchema) -> list[list[float]]:
    """Every baked hold position, laid out and sorted up the wall."""
    compute_layout(schema)
    pts = [p for e in schema.entities if is_climbable_entity(e)
           and e.name != "BackgroundCliffGroup"
           for p in e.instance_positions]
    return sorted(pts, key=lambda p: p[1])


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

    def test_mood_vocabulary_does_not_carry_the_medium(self):
        """Live: a CLIMBING game (player on a rock face) derived medium='air'
        from the single word "atmosphere" in its concept and hard-failed
        [layout.strict]. Mood and rendering words are not a claim about what
        the player is moving through."""
        s = _planar_schema()
        s.description = ("A third-person climbing game up a cliff face. Wet "
                         "stone, heavy atmosphere, atmospheric scattering in "
                         "the fog, a tense mood throughout.")
        arch = select_archetype(s)
        # The MEDIUM is the subject here: a rock face is ground, whatever the
        # mood words say. (The archetype is VerticalSurface rather than
        # ArenaPlane because "cliff face" is real climbing evidence — that is
        # the ascent axis working, not the medium leaking.)
        assert arch.medium == "ground", arch.evidence
        assert arch.name == "VerticalSurface", arch.evidence

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

    def test_climbing_the_leaderboard_does_not_flip_a_ground_game(self):
        """The ascent half of the same guard, and the reason ascent NOUNS
        establish the axis while ascent VERBS only choose a direction.

        Ascent and descent clear the same evidentiary bar — a climb is as much
        a vertical game as a drop is. What neither clears is one figurative
        verb: "climb the leaderboard" and "sales are climbing" are ordinary
        English, and a walking game that says either must stay on the ground
        plane rather than become a wall.
        """
        s = _planar_schema()
        s.description = ("A scavenger game in a depot yard. Collect scrap to "
                         "climb the leaderboard — the top scavengers are "
                         "ranked on a board by the gate, and your score keeps "
                         "climbing as you scale up the operation.")
        arch = select_archetype(s)
        assert (arch.name, arch.motion_axis) == ("ArenaPlane", "planar"), \
            arch.evidence
        assert arch.direction == "", arch.evidence

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
        s.description += " The player plummets down the collapsing shaft."
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

    def test_vertical_ground_descent_abstains(self):
        """Altitude-banded content in a scene that never says air or water:
        DESCENT through solid ground still has no primitive (a shaft). Ascent
        through it is a wall, and now resolves — hence the explicit descent
        evidence here."""
        s = _planar_schema()
        s.description += " The player plummets down the collapsing shaft."
        _entity(s, "Drones").instance_positions = [[0.0, 800.0, 0.0]]
        with pytest.raises(LayoutStrictError) as exc:
            compute_layout(s)
        assert "motion_axis='vertical' + medium='ground'" in str(exc.value)

    def test_vertical_with_undecided_direction_abstains(self):
        """Vertical motion with nothing saying which way: ascent and descent
        are different PRIMITIVES, not different numbers, so guessing is worse
        than stopping."""
        s = _planar_schema()
        _entity(s, "Drones").instance_positions = [[0.0, 800.0, 0.0]]
        with pytest.raises(LayoutStrictError) as exc:
            compute_layout(s)
        msg = str(exc.value)
        assert "DIRECTION is undecided" in msg, msg
        assert "'ascent'" in msg and "'descent'" in msg, msg
        assert "VerticalSurface" in msg and "VerticalCorridor" in msg, msg

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


class TestVerticalSurface:
    """Ascent moves ALONG a surface; descent moves THROUGH a volume. The
    living-wall e2e produced a flat arena before this primitive existed,
    because vertical evidence was descent-only."""

    @staticmethod
    def _wall():
        return _load("living_wall_llm.schema.json")

    def test_climbing_schema_selects_vertical_surface(self):
        arch = select_archetype(self._wall())
        assert arch.name == "VerticalSurface"
        assert (arch.motion_axis, arch.direction) == ("vertical", "ascent")

    def test_player_spawns_at_the_base(self):
        s = self._wall()
        compute_layout(s)
        px, py, _pz = _entity(s, "Player").instance_positions[0]
        assert py < 5.0, py

    def test_holds_span_base_to_summit_on_the_wall_face(self):
        s = self._wall()
        h, _why, _d = surface_height(s)
        holds = _hold_positions(s)
        assert holds, "no holds were placed"
        ys = [p[1] for p in holds]
        assert min(ys) <= 2.0, min(ys)          # reachable from standing
        assert max(ys) >= h - 0.01, (max(ys), h)  # reaches the summit
        assert all(abs(p[2]) > 0.0 for p in holds), "holds are not on the face"

    def test_every_successive_hold_is_within_reach(self):
        """The invariant: a wall whose holds are further apart than the player
        can reach is not a hard level, it is a broken one."""
        s = self._wall()
        reach, _why = _reach_distance(s)
        chain = sorted(_hold_positions(s), key=lambda p: p[1])
        gaps = [math.dist((a[0], a[1]), (b[0], b[1]))
                for a, b in zip(chain, chain[1:])]
        assert gaps and max(gaps) <= reach + 1e-6, max(gaps)

    def test_wall_creatures_are_on_the_wall_not_on_the_ground(self):
        s = self._wall()
        compute_layout(s)
        organism = _entity(s, "WallOrganismGroup")
        assert all(p[1] > 0.0 for p in organism.instance_positions), \
            organism.instance_positions

    def test_platform_kind_counts_as_a_hold(self):
        """Live incident (LithobornAscent, 2026-09-07): a wall-climb schema
        whose holds were authored as kind="platform" (a valid EntitySpec kind,
        unity_schema.py's own EntityKind literal) placed zero holds — every
        objective on the wall was unreachable because `is_climbable_entity`
        only recognised handhold/hold/climbable/ledge/grip. "Rib platforms" on
        a climbing wall ARE the holds; the kind must count."""
        assert is_climbable_entity(EntitySpec(name="RibPlatform", kind="platform"))

    def test_hazards_never_smother_a_hold(self):
        s = self._wall()
        compute_layout(s)
        holds = {e.name for e in s.entities if is_climbable_entity(e)}
        hold_pts = [p for e in s.entities if e.name in holds
                    for p in e.instance_positions]
        hazard_pts = [p for e in s.entities
                      if e.name not in holds and is_hazard_entity(e)
                      for p in e.instance_positions]
        assert hold_pts and hazard_pts
        for hz in hazard_pts:
            nearest = min(math.dist((hz[0], hz[1]), (h[0], h[1])) for h in hold_pts)
            assert nearest >= 1.0 - 1e-6, nearest

    def test_wall_surface_entity_is_chosen_and_logged(self):
        s = self._wall()
        msgs = compute_layout(s)
        line = next((m for m in msgs if "Wall surface is" in m), "")
        assert "BackgroundCliffGroup" in line, msgs
        assert "signal:" in line, line

    def test_provenance_is_complete(self):
        s = self._wall()
        msgs = compute_layout(s)
        blob = "\n".join(msgs)
        assert "motion_axis=vertical/ascent" in blob        # direction + tier
        assert "strong-vocabulary" in blob or "entity" in blob
        assert "Wall surface is" in blob                    # chosen + signal
        assert "Reach distance" in blob                     # the value used
        assert "max gap" in blob                            # chain check result

    def test_ground_scenery_stays_on_the_ground(self):
        s = self._wall()
        compute_layout(s)
        for name in ("NPC", "Collectible"):
            e = _entity(s, name)
            assert all(p[1] == 0.0 for p in e.instance_positions), e.instance_positions

    def test_surface_layout_is_deterministic_and_idempotent(self):
        a, b = self._wall(), self._wall()
        compute_layout(a)
        compute_layout(b)
        assert [e.model_dump() for e in a.entities] == \
               [e.model_dump() for e in b.entities]
        first = [e.model_dump() for e in a.entities]
        compute_layout(a)
        assert [e.model_dump() for e in a.entities] == first


class TestReachabilityInvariant:
    """The check must be able to REJECT, not just decorate a chain that was
    already correct by construction."""

    def test_a_chain_with_an_over_reach_gap_is_rejected(self):
        bad = [(0.0, 0.0), (0.0, 2.0), (0.0, 9.0)]      # 7 m gap, 2.5 m reach
        with pytest.raises(LayoutStrictError) as exc:
            _verify_hold_chain(bad, 2.5, "BadWall")
        msg = str(exc.value)
        assert "unclimbable" in msg
        assert "7.000" in msg and "2.500" in msg, msg

    def test_a_chain_inside_reach_is_accepted_and_returns_the_max_gap(self):
        ok = [(0.0, 0.0), (0.0, 2.0), (1.0, 3.5)]
        assert _verify_hold_chain(ok, 2.5, "OkWall") == pytest.approx(2.0)

    @pytest.mark.parametrize("n,height,reach", [
        (5, 10.0, 2.5), (50, 60.0, 2.5), (12, 40.0, 4.0),
        (200, 300.0, 2.0), (2, 3.0, 2.5), (30, 12.0, 1.0),
    ])
    def test_generated_chains_always_satisfy_their_reach(self, n, height, reach):
        chain = _hold_chain(n, 1.0, height, reach, half_width=8.0)
        assert len(chain) == n
        # Generated chains are the input the invariant is supposed to pass.
        _verify_hold_chain(chain, reach, "GenWall")


class TestSpatialContract:
    """The seam with src/unity_spatial.py and Step 7.

    The layout pass is the authority on which primitive a scene is, and the
    playability evaluator grades against it. That only works if every
    primitive writes `elevation` back and the two modules agree on the name.
    """

    @pytest.mark.parametrize("builder,primitive", [
        (_planar_schema,  "ground_plane"),
        (_racing_schema,  "route_corridor"),
        (_skydive_schema, "airspace_column"),
        (_dive_schema,    "depth_column"),
        (_glider_schema,  "airspace_column"),
        (_survey_schema,  "depth_column"),
        (lambda: _load("living_wall_llm.schema.json"), "wall_surface"),
    ])
    def test_layout_stamps_the_primitive_it_used(self, builder, primitive):
        s = builder()
        compute_layout(s)
        assert s.meta.get("spatial_primitive") == primitive

    @pytest.mark.parametrize("builder", [
        _skydive_schema, _dive_schema, _glider_schema, _survey_schema,
        lambda: _load("living_wall_llm.schema.json"),
    ])
    def test_every_group_reports_the_elevation_it_was_baked_at(self, builder):
        """Step 7 reads `elevation`, not instance_positions. A primitive that
        bakes altitude without writing it back leaves the evaluator grading a
        scene that does not exist — the volume_player check in particular."""
        s = builder()
        compute_layout(s)
        for e in s.entities:
            if not e.instance_positions:
                continue
            ys = {round(p[1], 3) for p in e.instance_positions}
            if len(ys) == 1:                       # a single-altitude group
                only = next(iter(ys))
                assert abs(e.elevation - only) < 1e-6, (e.name, e.elevation, only)

    def test_an_authored_elevation_is_never_overridden(self):
        """src/unity_spatial.py's contract: the field is the intent channel
        and always wins over anything derived."""
        s = _glider_schema()
        _entity(s, "CloudBanks").elevation = 812.0
        compute_layout(s)
        cloud = _entity(s, "CloudBanks")
        assert cloud.elevation == 812.0
        assert all(p[1] == 812.0 for p in cloud.instance_positions)

    def test_altitude_follows_kind_not_footprint(self):
        """Ordering by size is arbitrary with respect to altitude — it put a
        jump plane below its own jumper. unity_spatial.tier_of decides."""
        s = _glider_schema()
        compute_layout(s)
        # collectible (tier .45) sits above prop (tier .25) whatever their
        # relative footprints are; RingGates is far smaller than the islands.
        assert _entity(s, "RingGates").elevation > _entity(s, "FloatingIslands").elevation


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

    @staticmethod
    def _round(obj, nd: int = 6):
        """Round every float in a nested structure.

        The recording is made on one platform and compared on another, and
        `math.sin`/`cos` differ in their last bits between libms — a ring of
        drones came out 2 ULP apart on Linux CI vs the Windows box the
        baseline was taken on. Rounding to 1e-6 keeps the regression this
        guards (positions must not MOVE) while dropping noise no scene can
        see. Comparing raw float64, or hashing generated text that embeds it,
        is not portable and never was."""
        if isinstance(obj, float):
            return round(obj, nd)
        if isinstance(obj, list):
            return [TestArenaPlaneUnchanged._round(x, nd) for x in obj]
        if isinstance(obj, dict):
            return {k: TestArenaPlaneUnchanged._round(v, nd) for k, v in obj.items()}
        return obj

    @pytest.mark.parametrize("key,builder", [
        ("planar", _planar_schema),
        ("racing", _racing_schema),
    ])
    def test_layout_matches_the_pre_change_recording(self, key, builder):
        want = self._round(self._baseline()[key])
        s = builder()
        compute_layout(s)
        got = self._round([e.model_dump() for e in s.entities])
        assert got == want["entities"]
        assert self._round(
            s.world_spec.model_dump() if s.world_spec else None) == \
            want["world_spec"]
        assert self._round(list(terrain_bounds(s.entities, s.locomotion))) == \
            want["terrain_bounds"]

    def test_generated_project_tree_is_unchanged(self):
        """The real output: the whole generated Assets/ tree.

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
            files = sorted(str(f.relative_to(root)).replace("\\", "/")
                           for f in Path(root).rglob("*") if f.is_file())
        # Not vacuous: the generator really did lay the scene out on the way.
        assert all(e.instance_positions for e in s.entities)
        # The file SET, not a byte hash: generated .unity text embeds the
        # float64 positions above, so a hash of it is platform-specific for
        # the same reason `_round` exists. The numbers themselves are covered
        # exactly (to 1e-6) by test_layout_matches_the_pre_change_recording.
        assert files == self._baseline()["planar_project_files"]


# ── 3b. Course zones (docs/COURSE_ARCHETYPE_LAYERS.md, step 3/4) ────────────
# "Do not default to empty if present": an authored course.zones must
# produce a REAL, different, deterministic effect on the two wired
# archetypes (ArenaPlane's path-route corridor, BoundedVolume), and a LOUD
# warning (never silent) on every archetype that doesn't consume it yet.

class TestCourseZonesWiring:
    @staticmethod
    def _with_zones(schema: UnitySchema, *zones: RouteZoneSpec) -> UnitySchema:
        if schema.world_spec is None:
            schema.world_spec = WorldSpec()
        schema.world_spec.course = CourseSpec(zones=list(zones))
        return schema

    def test_arena_plane_dip_zone_lowers_packed_group_elevation(self):
        baseline = _racing_schema()
        compute_layout(baseline)
        baseline_elev = {e.name: e.elevation for e in baseline.entities}

        zoned = self._with_zones(
            _racing_schema(),
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="dip",
                         amplitude=2.5, label="TestDip"))
        msgs = compute_layout(zoned)

        # Every packed, FORMATION (non-path, non-preplaced, non-scenery-
        # distributed) group covered by a dip spanning the WHOLE route must
        # have shifted by exactly -amplitude — not stayed at its zone-free
        # baseline. RivalDrones (kind="vehicle") goes through `_corridor_pack`
        # same as before this fixture also gained a scenery-distributed
        # "Crates" group, which zones don't reach yet (see
        # _scenery_distributed_pack's docstring).
        drones = next(e for e in zoned.entities if e.name == "RivalDrones")
        assert drones.elevation == round(baseline_elev["RivalDrones"] - 2.5, 3)
        assert drones.elevation != baseline_elev["RivalDrones"]
        assert any("Applied 1 authored course zone" in m for m in msgs)

    def test_arena_plane_zones_are_deterministic(self):
        """Same schema, same zones, twice -> identical output (no RNG leak)."""
        def _run():
            s = self._with_zones(
                _racing_schema(),
                RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="rough", amplitude=1.0))
            compute_layout(s)
            return {e.name: e.elevation for e in s.entities}
        assert _run() == _run()

    def test_arena_plane_empty_zones_still_byte_identical(self):
        """CourseSpec present but zones=[] (the schema default) must be a
        complete no-op — the "empty means unchanged" half of the contract,
        proven directly rather than only via the golden-baseline test."""
        plain = _racing_schema()
        compute_layout(plain)
        explicit_empty = self._with_zones(_racing_schema())  # zones=()
        compute_layout(explicit_empty)
        assert ([e.model_dump() for e in plain.entities] ==
                [e.model_dump() for e in explicit_empty.entities])

    def test_bounded_volume_dip_zone_shifts_interior_elevation(self):
        baseline = _glider_schema()
        compute_layout(baseline)
        baseline_elev = {e.name: e.elevation for e in baseline.entities}

        zoned = self._with_zones(
            _glider_schema(),
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="dip",
                         amplitude=0.1, label="TestVolumeDip"))
        msgs = compute_layout(zoned)
        zoned_elev = {e.name: e.elevation for e in zoned.entities}

        shifted = [name for name in baseline_elev
                   if baseline_elev[name] != zoned_elev.get(name)]
        assert shifted, "a course-wide dip zone must shift at least one interior group"
        assert any("Applied 1 authored course zone" in m for m in msgs)

    def test_unwired_archetype_warns_loudly_instead_of_silent_noop(self, monkeypatch):
        """All five real archetypes are wired as of step 4 (see the other
        tests in this class) -- this test proves the WARNING MECHANISM
        itself still fires correctly for whatever archetype ISN'T in
        `_ZONE_AWARE_ARCHETYPES`, by shrinking that set for one call, rather
        than depending on any particular archetype staying unwired forever
        (VerticalCorridor was that archetype until this same change wired
        it, which is exactly why this test needed to stop depending on it)."""
        import src.unity_layout as unity_layout_module
        monkeypatch.setattr(unity_layout_module, "_ZONE_AWARE_ARCHETYPES",
                            {"ArenaPlane", "BoundedVolume", "VerticalSurface",
                             "UISurface"})  # VerticalCorridor deliberately excluded
        zoned = self._with_zones(
            _skydive_schema(),
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="dip", amplitude=5.0))
        msgs = compute_layout(zoned)
        assert any("doesn't interpret them yet" in m for m in msgs), (
            "authored course zones on an archetype outside "
            "_ZONE_AWARE_ARCHETYPES must warn, not silently no-op")

    @pytest.mark.parametrize("builder", [_skydive_schema, _dive_schema])
    def test_vertical_corridor_dip_zone_shifts_stratified_elevation(self, builder):
        baseline = builder()
        compute_layout(baseline)
        baseline_elev = {e.name: e.elevation for e in baseline.entities}

        zoned = self._with_zones(
            builder(),
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="dip",
                         amplitude=0.2, label="TestCorridorDip"))
        msgs = compute_layout(zoned)
        zoned_elev = {e.name: e.elevation for e in zoned.entities}

        shifted = [name for name in baseline_elev
                  if baseline_elev[name] != zoned_elev.get(name)]
        assert shifted, "a course-wide dip zone must shift at least one stratified group"
        assert any("Applied 1 authored course zone(s) along the drop axis" in m
                  for m in msgs)

    def test_vertical_surface_dip_zone_shifts_hazard_height(self):
        baseline = _load("living_wall_llm.schema.json")
        compute_layout(baseline)
        baseline_elev = {e.name: e.elevation for e in baseline.entities}

        zoned = self._with_zones(
            _load("living_wall_llm.schema.json"),
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="dip",
                         amplitude=0.15, label="TestClimbDip"))
        msgs = compute_layout(zoned)
        zoned_elev = {e.name: e.elevation for e in zoned.entities}

        shifted = [name for name in baseline_elev
                  if baseline_elev[name] != zoned_elev.get(name)]
        assert shifted, "a course-wide dip zone must shift at least one hazard's height"
        assert any("Applied" in m and "climb axis" in m for m in msgs)

    @staticmethod
    def _board_schema() -> UnitySchema:
        """A minimal non-embodied (UISurface) schema, freshly built each
        call -- must NOT be reused across two compute_layout() calls, since
        the first bakes instance_positions onto its entities and the second
        would then see them as already-preplaced."""
        return UnitySchema(
            project_name="TestBoard", company_name="C",
            description="A file browser interface.",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear", locomotion="none",
            features=[], scenes=["Main"], packages=[],
            entities=[
                EntitySpec(name="Row1", kind="prop", count=1, placement="single", scale=1.0),
                EntitySpec(name="Row2", kind="prop", count=1, placement="single", scale=1.0),
                EntitySpec(name="Row3", kind="prop", count=1, placement="single", scale=1.0),
                EntitySpec(name="Row4", kind="prop", count=1, placement="single", scale=1.0),
            ],
        )

    def test_ui_surface_ripple_zone_shifts_row_depth(self):
        baseline = self._board_schema()
        compute_layout(baseline)
        baseline_z = {e.name: e.origin_offset[1] for e in baseline.entities}

        zoned = self._with_zones(
            self._board_schema(),
            # frequency=0.25 so t=0 and t=1 (this schema's 2 reading-order
            # rows) don't BOTH land on sin's zero crossing the way
            # frequency=1.0 would (sin(0)=sin(2*pi)=0) -- deliberately picked
            # so the test exercises a real nonzero shift, not a coincidental no-op.
            RouteZoneSpec(start_t=0.0, end_t=1.0, modifier="ripple",
                         amplitude=1.5, frequency=0.25, label="TestRowRipple"))
        msgs = compute_layout(zoned)
        zoned_z = {e.name: e.origin_offset[1] for e in zoned.entities}

        shifted = [name for name in baseline_z if baseline_z[name] != zoned_z.get(name)]
        assert shifted, "a ripple zone must shift at least one row's depth"
        assert any("Applied 1 authored course zone" in m and "reading-order" in m
                  for m in msgs)


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


class TestSpatialRelationshipOf:
    """spatial_relationship_of() classifies a packable group's relationship to
    an authored route corridor — pure keyword/kind, no schema field, no LLM.
    See its docstring for why "cluster" is the correct default for anything
    unrecognised, not a placeholder."""

    @pytest.mark.parametrize("name", [
        "Streetlights", "TrackTorches", "WarningSigns", "FinishFlags",
        "Bollards", "LampPosts",
    ])
    def test_edge_distributed_keywords(self, name):
        e = EntitySpec(name=name, kind="prop", count=8)
        assert spatial_relationship_of(e) == "edge_distributed"

    @pytest.mark.parametrize("name", [
        "CrashBarriers", "TrackFences", "Guardrails", "TireWalls",
    ])
    def test_boundary_follow_keywords(self, name):
        e = EntitySpec(name=name, kind="prop", count=20)
        assert spatial_relationship_of(e) == "boundary_follow"

    @pytest.mark.parametrize("name", [
        "Spectators", "PitCrew", "Grandstands", "DirtMounds", "Merch",
    ])
    def test_unrecognised_scenery_names_distribute_by_default(self, name):
        e = EntitySpec(name=name, kind="prop", count=6)
        assert spatial_relationship_of(e) == "scenery_distributed"

    def test_camel_case_name_still_matches(self):
        # _entity_words CamelCase-splits before matching -- "StreetLamps"
        # must read as "street lamps", not fail to match "lamp" as one token.
        e = EntitySpec(name="StreetLamps", kind="prop", count=8)
        assert spatial_relationship_of(e) == "edge_distributed"

    def test_kind_alone_does_not_trigger_an_edge_or_boundary_match(self):
        # kind="prop" is the schema default for nearly every entity; only
        # NAME/model_hint/tag content should drive edge/boundary matching.
        e = EntitySpec(name="Rivals", kind="prop", count=3)
        assert spatial_relationship_of(e) == "scenery_distributed"


class TestFormationVsScenery:
    """is_formation_entity() / spatial_relationship_of()'s "formation" vs
    "scenery_distributed" split (src/unity_layout_archetype.py) — a squad/
    pack that belongs staying together in one spot (rival karts at a start
    grid) vs. decorative content whose count means "this many exist around
    the level", not "these form one group". See
    feedback-no-single-anchor-clusters memory for why this distinction
    exists: the user does not want ANY group dumped as one clustered
    anchor unless it's genuinely supposed to read as one unit."""

    @pytest.mark.parametrize("kind", ["vehicle", "aircraft", "character", "creature"])
    def test_actor_kinds_are_formations(self, kind):
        e = EntitySpec(name="Rivals", kind=kind, count=3)
        assert is_formation_entity(e)
        assert spatial_relationship_of(e) == "formation"

    @pytest.mark.parametrize("tag", ["Enemy", "Ally"])
    def test_relational_tags_are_formations_regardless_of_kind(self, tag):
        # Live schema-fill puts hostility in `tag` more reliably than
        # `kind` -- a kind="prop" landmine with tag="Enemy" must still read
        # as a formation, not get scattered like decorative scenery.
        # (tag="Player" is deliberately not parametrized here:
        # EntitySpec._reserve_player_tag resets it to "Untagged" for a
        # kind="prop" entity by design -- "Player" is a singleton tag a prop
        # is never allowed to hold, so that combination can't occur.)
        e = EntitySpec(name="Ambush", kind="prop", count=4, tag=tag)
        assert is_formation_entity(e)
        assert spatial_relationship_of(e) == "formation"

    def test_prop_with_no_relational_tag_is_scenery(self):
        e = EntitySpec(name="Crates", kind="prop", count=12, tag="Untagged")
        assert not is_formation_entity(e)
        assert spatial_relationship_of(e) == "scenery_distributed"

    def test_collectibles_are_scenery_not_formation(self):
        # "collectibles along a platforming route" is a distribute case, not
        # a formation, even though every instance is functionally identical.
        e = EntitySpec(name="Coins", kind="prop", count=15, tag="Collectible")
        assert spatial_relationship_of(e) == "scenery_distributed"
