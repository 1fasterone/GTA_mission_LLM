"""Engineer-authored compiler fixtures; these do NOT evaluate Step 3."""
import math

import pytest

from src.unity_layout import compute_layout, _boundary_follow_pack
from src.unity_layout_archetype import is_finish_gate_entity
from src.unity_route_axis import height_at, project
from src.unity_schema import CourseSpec, EntitySpec, RouteZoneSpec, UnitySchema, WorldSpec


def course_fixture():
    return UnitySchema(project_name="RacecourseAcceptance", game_type="3D",
                       platform="pc", render_pipeline="builtin", unity_version="6000.1",
                       locomotion="drive", scenes=["Race"], entities=[
        EntitySpec(name="TrackSurface", kind="road", placement="path", path_width=10,
                   path_points=[[0, 5, 0], [0, 8, 40], [20, 12, 70],
                                [50, 16, 80], [90, 20, 80]]),
        EntitySpec(name="CheckpointBanners", kind="decoration", count=4,
                   model_hint="checkpoint banner finish line"),
        EntitySpec(name="FinishArch", kind="decoration", count=1),
        EntitySpec(name="Streetlights", kind="decoration", count=12),
        EntitySpec(name="Barriers", kind="decoration", count=1, spacing=2),
        EntitySpec(name="Crates", kind="prop", count=9),
    ])


@pytest.mark.parametrize("kind,name", [("checkpoint", "Markers"),
                                     ("decoration", "CheckpointBanners")])
def test_checkpoint_identity_outranks_search_hint(kind, name):
    e = EntitySpec(name=name, kind=kind, count=4,
                   model_hint="checkpoint banner finish line")
    assert not is_finish_gate_entity(e)


def test_curved_elevated_course_preserves_checkpoints_and_heights():
    schema = course_fixture()
    compute_layout(schema)
    route = schema.entities[0].path_points
    assert len(schema.entities[1].instance_positions) == 4
    boundary = schema.entities[4]
    assert max(math.dist(a, b) for a, b in zip(boundary.instance_positions,
               boundary.instance_positions[1:])) <= boundary.spacing + .002
    for e in schema.entities[1:]:
        assert e.count == len(e.instance_positions)
        for x, y, z in e.instance_positions:
            t, _ = project(route, x, z)
            assert y == pytest.approx(height_at(route, t), abs=.002)
    first = [e.model_dump() for e in schema.entities]
    compute_layout(schema)
    assert [e.model_dump() for e in schema.entities] == first


@pytest.mark.parametrize("length,spacing", [(10, 2), (1, 2), (10, 3)])
def test_open_boundary_maximum_gap(length, spacing):
    route = EntitySpec(name="Road", kind="road", placement="path",
                       path_points=[[0, 0, 0], [length, 0, 0]], path_width=4)
    p, _ = _boundary_follow_pack(EntitySpec(name="Barriers", spacing=spacing), route, [])
    assert p[0][0] == 0 and p[-1][0] == length
    assert max(math.dist(a, b) for a, b in zip(p, p[1:])) <= spacing + .001


def test_authored_positions_untouched():
    schema = course_fixture()
    schema.entities[1].instance_positions = [[5, 39, 8]]
    compute_layout(schema)
    assert schema.entities[1].instance_positions == [[5, 39, 8]]


def test_distributed_instances_apply_course_zone_height():
    baseline, dipped = course_fixture(), course_fixture()
    dipped.world_spec = WorldSpec(course=CourseSpec(zones=[
        RouteZoneSpec(start_t=0, end_t=1, modifier="dip", amplitude=2.5)]))
    compute_layout(baseline)
    compute_layout(dipped)
    for a, b in zip(baseline.entities[1:], dipped.entities[1:]):
        for p, q in zip(a.instance_positions, b.instance_positions):
            assert q[1] == pytest.approx(p[1] - 2.5, abs=.002)


def test_closed_boundary_has_no_duplicate_seam_instance():
    route = EntitySpec(name="Road", kind="road", placement="path", path_width=4,
                       path_points=[[0, 0, 0], [10, 0, 0], [10, 0, 10], [0, 0, 0]])
    p, _ = _boundary_follow_pack(EntitySpec(name="Barriers", spacing=2), route, [])
    assert p[0] != p[-1]
