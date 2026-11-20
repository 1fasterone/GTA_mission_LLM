"""Step 5 layout must never bake a layout its own final spatial preflight
rejects.

The live neon_fugitive E2E batch of 2026-11-17 died twice at the final
preflight (`acceptance.require_spatial_validation`) on geometry the layout had
just baked. Two runs, two mechanisms in `src/unity_layout.py`:

  run 01  instance_footprints. A ring was sized by ARC length. Neighbours sit a
          CHORD apart, and the chord is shorter. TunnelWalls: a 2-instance
          stop 4.00 m apart, against a 4.60 m minimum.
  run 02  group_footprints. Packing circles enclosed instance CENTRES, but the
          validator compares BODIES. Helipads x ContainerStacks: 6.50 m apart,
          against the 7.55 m required.

Every schema in this file is ENGINEER-WRITTEN TEST DATA, not pipeline output.
The cases reproduce those two mechanisms with the same numbers (count, scale,
spacing, radius), not the captures themselves. The captures carry more
`scene_specs` than main's scene cap and so cannot validate here; see the PR.
The one exception is `skyline_glider.schema.json`, a checked-in hand-written
fixture (tests/fixtures/README.md) reused as a BoundedVolume base.

Every layout is judged twice: by `validate_spatial_layout` itself, and by a
direct body-to-body check (`_body_findings`). The validator culls group pairs
on `group_radius`, which encloses centres only, so it cannot see every body
overlap. The direct check holds the layout to what the validator MEANS.
"""
from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from typing import Mapping, Optional

import pytest

from src.scene_graph import build_scene_graph
from src.unity_layout import (
    _bake_positions,
    _effective_spacing,
    _footprint_radius,
    _packing_radius,
    _scenery_stop,
    compute_layout,
    instance_body_radius,
    instance_min_spacing,
)
from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_pipeline import prepare_spatial_layout
from src.unity_spatial_realization import realize_spatial_layout
from src.unity_spatial_roles import derive_spatial_roles
from src.unity_spatial_validation import validate_spatial_layout

_FIXTURES = Path(__file__).parent / "fixtures"


# ── helpers ───────────────────────────────────────────────────────────────────

def _scene(*entities: EntitySpec, locomotion: str = "drive",
           route: bool = False) -> UnitySchema:
    """An ArenaPlane ground scene: a player single, an optional path route,
    then the groups under test."""
    head = [EntitySpec(name="Hero", kind="vehicle", tag="Player")]
    if route:
        head.append(EntitySpec(
            name="Road", kind="track", placement="path",
            path_points=[[0, 0, 0], [0, 0, 60], [40, 0, 60]], path_width=8))
    return UnitySchema(
        project_name="BodyPacking", company_name="Test",
        description="A deterministic ground scene for layout tests.",
        game_type="3D", platform="pc", render_pipeline="URP",
        locomotion=locomotion, scenes=["Main"], entities=head + list(entities))


def _preflight(schema: UnitySchema,
               footprints: Optional[Mapping[str, float]] = None) -> dict:
    """The final Step-5 preflight. With `footprints`, the measured-footprint
    relayout the generator and the sidecar freeze run instead."""
    if footprints is None:
        return prepare_spatial_layout(
            schema, closed_image_ir=None, image_spatial_evidence=None,
            reset_generated_layout=True)["spatial_validation"]
    roles = derive_spatial_roles(schema)
    graph = build_scene_graph(schema, roles)
    realized = realize_spatial_layout(schema, footprints=footprints,
                                      relation_graph=graph)
    return validate_spatial_layout(schema, world_layout=realized,
                                   spatial_roles=roles)


def _body_findings(schema: UnitySchema,
                   footprints: Optional[Mapping[str, float]] = None) -> list[str]:
    """Every body overlap in the baked layout, with no broad-phase cull: the
    validator's two contracts applied to every instance pair."""
    fp = footprints or {}
    groups = [e for e in schema.entities
              if e.placement != "path" and e.instance_positions]
    found: list[str] = []
    for a, b in itertools.combinations(groups, 2):
        if not math.isclose(a.elevation, b.elevation, abs_tol=1e-6):
            continue
        need = (instance_body_radius(a.scale, fp.get(a.name))
                + instance_body_radius(b.scale, fp.get(b.name)))
        d = min(math.hypot(p[0] - q[0], p[2] - q[2])
                for p in a.instance_positions for q in b.instance_positions)
        if d + 0.25 < need:
            found.append(f"{a.name} x {b.name}: {d:.2f} m < {need - 0.25:.2f} m")
    for e in groups:
        need = instance_min_spacing(e.scale, fp.get(e.name))
        for p, q in itertools.combinations(e.instance_positions, 2):
            if math.dist(p, q) + 0.01 < need:
                found.append(f"{e.name} within: {math.dist(p, q):.2f} m < {need:.2f} m")
                break
    return found


def _rejections(validation: dict) -> list[str]:
    return [f"{c['check']}: {c['detail']}" for c in validation["checks"]
            if c["status"] == "fail"]


def _assert_accepted(schema: UnitySchema, validation: dict,
                     footprints: Optional[Mapping[str, float]] = None) -> None:
    assert _rejections(validation) == []
    assert _body_findings(schema, footprints) == []


def _closest(a: EntitySpec, b: EntitySpec) -> float:
    return min(math.hypot(p[0] - q[0], p[2] - q[2])
               for p in a.instance_positions for q in b.instance_positions)


def _closest_within(e: EntitySpec) -> float:
    return min(math.dist(p, q)
               for p, q in itertools.combinations(e.instance_positions, 2))


# ── run 01: rings are spaced by chord ────────────────────────────────────────

class TestRingChordSpacing:

    @pytest.mark.parametrize("eff", [0.5, 2.0, 4.6, 11.5])
    def test_ring_neighbours_are_a_full_spacing_apart(self, eff):
        for n in range(2, 25):
            ring = EntitySpec(name="Ring", placement="ring", count=n, radius=0.0)
            r = _footprint_radius(ring, eff)
            assert 2.0 * r * math.sin(math.pi / n) >= eff - 1e-9, n

    def test_chord_radius_never_narrows_an_existing_ring(self):
        """sin x <= x, so the chord form only ever widens the old arc form:
        a ring whose authored radius already won is drawn exactly as before."""
        for n, eff in itertools.product(range(1, 25), (0.5, 2.0, 11.5)):
            ring = EntitySpec(name="Ring", placement="ring", count=n, radius=0.0)
            assert _footprint_radius(ring, eff) >= n * eff / (2.0 * math.pi) - 1e-12

    def test_tunnel_walls_two_instance_stop_keeps_the_minimum(self):
        """Run 01's mechanism. TunnelWalls (ring, count 8, scale 4, spacing 2,
        radius 8) is scenery, spread as stops of [3, 3, 2]. The 2-instance
        stop scales the authored radius to 8 * 2/8 = 2.0. The arc form kept
        2.0, putting the pair 4.00 m apart. The minimum is 1.15 * 4 = 4.60 m."""
        walls = EntitySpec(name="TunnelWalls", kind="structure", tag="Environment",
                           placement="ring", count=8, scale=4.0, spacing=2.0,
                           radius=8.0)
        stub, _reserved = _scenery_stop(walls, 2, None)
        assert stub.radius == pytest.approx(2.0)
        assert stub.group_radius == pytest.approx(2.3)   # 4.6 / (2 sin(pi/2))
        assert 2.0 * stub.group_radius >= 4.6 - 1e-9

        schema = _scene(walls)
        validation = _preflight(schema)
        _assert_accepted(schema, validation)
        laid = next(e for e in schema.entities if e.name == "TunnelWalls")
        assert _closest_within(laid) + 0.01 >= 4.6


# ── run 02: packing circles enclose bodies ───────────────────────────────────

class TestBodyInclusivePacking:

    def test_packing_radius_reserves_the_body_beyond_the_centre(self):
        """Run 02's numbers: a stop of two scale-10 helipads (eff 11.5) has its
        centres 5.75 m out, but its bodies reach 5.75 + 6.0 = 11.75 m."""
        pads = EntitySpec(name="Helipads", kind="platform", tag="Platform",
                          placement="line", count=4, scale=10.0, spacing=2.0,
                          radius=8.0)
        stub, reserved = _scenery_stop(pads, 2, None)
        # Pattern (centre) radius: max(authored share 8 * 2/4, 0.5 * 11.5).
        # This 5.75 m is all the old packer reserved.
        assert stub.group_radius == pytest.approx(5.75)
        assert reserved == pytest.approx(5.75 + 6.0)

    def test_packing_radius_encloses_every_baked_body(self):
        """Measured from the real bake, not a formula. A grid's rows run one
        way from its anchor, so `_footprint_radius` misses its far corner at
        n=9 and n=16. The packing circle may not."""
        for placement, n, scale in itertools.product(
                ("line", "ring", "grid", "scatter", "single"),
                (1, 2, 3, 9, 16, 20), (0.3, 1.0, 3.0, 10.0)):
            if placement == "single" and n != 1:
                continue
            e = EntitySpec(name="G", placement=placement, count=n, scale=scale,
                           spacing=2.0, radius=0.0)
            eff = _effective_spacing(e)
            e.group_radius = _footprint_radius(e, eff)
            reserved = _packing_radius(e)
            for p in _bake_positions(e, eff, 0.0, 0.0):
                assert math.hypot(p[0], p[2]) + instance_body_radius(scale, None) \
                    <= reserved + 1e-9, (placement, n, scale)

    def test_ring_is_drawn_at_its_pattern_radius_not_its_packing_radius(self):
        """The packing radius is reserved, never drawn. A cohesive ring's
        instances sit exactly `group_radius` from its anchor."""
        schema = _scene(EntitySpec(name="Drones", kind="vehicle", placement="ring",
                                   count=6, scale=1.2, radius=10.0))
        compute_layout(schema)
        drones = next(e for e in schema.entities if e.name == "Drones")
        assert drones.group_radius == pytest.approx(10.0)
        cx, cz = drones.origin_offset
        for p in drones.instance_positions:
            assert math.hypot(p[0] - cx, p[2] - cz) == pytest.approx(10.0, abs=2e-3)

    def test_helipads_and_container_stacks_keep_their_bodies_apart(self):
        """Run 02's mechanism, scenery stops on the no-route spread."""
        schema = _scene(
            EntitySpec(name="Helipads", kind="platform", tag="Platform",
                       placement="line", count=4, scale=10.0, spacing=2.0,
                       radius=8.0),
            EntitySpec(name="ContainerStacks", kind="prop", tag="Prop",
                       placement="grid", count=12, scale=3.0, spacing=2.0,
                       radius=8.0))
        validation = _preflight(schema)
        _assert_accepted(schema, validation)
        by = {e.name: e for e in schema.entities}
        assert _closest(by["Helipads"], by["ContainerStacks"]) + 0.25 >= 6.0 + 1.8

    @pytest.mark.parametrize("placement,count,scale,route", [
        ("line", 4, 10.0, False),   # main: Hero x GroupA 2.62 m, bodies need 6.35
        ("ring", 3, 10.0, False),   # main: GroupA x GroupB 4.77 m, need 9.05
        ("ring", 5, 10.0, True),    # main: GroupA x Crates 6.79 m, need 7.55
    ])
    def test_cohesive_groups_pack_on_bodies(self, placement, count, scale, route):
        """`_pack_groups` and `_corridor_pack` pack FORMATION groups
        (kind="vehicle") on the same centre-only radius the scenery stops used.
        Their `group_radius` circles stay 2 m apart, so the validator's broad
        phase culled every such pair. These overlaps were real (a scale-10
        convoy parked on the player) and invisible to the preflight."""
        schema = _sweep_scene(placement, count, scale, "vehicle", route)
        validation = _preflight(schema)
        _assert_accepted(schema, validation)

    def test_group_added_between_passes_clears_the_first_passes_bodies(self):
        """Step 5 lays out once (src/unity_pipeline.py), later passes can add a
        group, and the final preflight re-runs the layout with every earlier
        group preplaced. Their seed circle was `group_radius`, centres only,
        so the late group was packed into the bodies around them. The
        validator's broad phase culls on those same circles, so only the
        direct body check sees this one."""
        for placement, count, scale in itertools.product(
                ("line", "ring", "grid"), (2, 3, 4), (8.0, 10.0)):
            schema = _scene(EntitySpec(name="Pads", kind="vehicle",
                                       placement=placement, count=count,
                                       scale=scale, spacing=2.0, radius=0.0),
                            locomotion="walk")
            compute_layout(schema)
            schema.entities.append(EntitySpec(name="LateTower", kind="vehicle",
                                              placement="single", scale=3.0))
            validation = _preflight(schema)
            _assert_accepted(schema, validation)

    @pytest.mark.parametrize("placement", ["line", "ring", "grid", "scatter"])
    @pytest.mark.parametrize("kind", ["prop", "vehicle"])
    def test_tiny_spacing_keeps_the_half_metre_floor(self, placement, kind):
        """The preflight's within-group minimum never drops under 0.5 m. A
        0.3-scale group authored at spacing 0.2 used to bake at 0.345 m."""
        pebbles = EntitySpec(name="Pebbles", kind=kind, placement=placement,
                             count=6, scale=0.3, spacing=0.2, radius=0.0)
        assert _effective_spacing(pebbles) == pytest.approx(0.5)
        schema = _scene(pebbles, locomotion="walk")
        validation = _preflight(schema)
        _assert_accepted(schema, validation)


# ── the general case: placement x count x scale ──────────────────────────────

_COUNTS = (1, 2, 3, 4, 5, 8, 12, 20)
_SCALES = (0.3, 1.0, 4.0, 10.0)
# Engineer-chosen measured footprints (metres at scale 1): one larger than the
# primitive approximation, one smaller, one in between.
_MEASURED = {"GroupA": 2.4, "GroupB": 0.6, "Crates": 1.5}


def _sweep_scene(placement: str, count: int, scale: float, kind: str,
                 route: bool) -> UnitySchema:
    """kind="prop" takes the scenery-stop spread, kind="vehicle" the cohesive
    formation packer (`spatial_relationship_of`). An untagged kind="enemy" is
    scenery too, so it is no substitute for the second."""
    return _scene(
        EntitySpec(name="GroupA", kind=kind, placement=placement, count=count,
                   scale=scale, spacing=2.0, radius=8.0),
        EntitySpec(name="GroupB", kind=kind, placement=placement, count=count,
                   scale=scale * 0.5 + 0.5, spacing=2.0, radius=4.0),
        EntitySpec(name="Crates", kind="prop", placement="grid", count=6,
                   scale=3.0, spacing=2.0),
        locomotion="walk", route=route)


class TestLayoutNeverFailsItsPreflight:
    """1176 layouts: 1056 ArenaPlane cells, 120 BoundedVolume cells. On
    origin/main 9aa2f6f the preflight rejected 100 of them, and the direct
    body check found overlaps in 160 (60 the preflight could not see). Every
    one must now pass both."""

    @pytest.mark.parametrize("measured", [False, True], ids=["primitive", "measured"])
    @pytest.mark.parametrize("route", [False, True], ids=["level", "route"])
    @pytest.mark.parametrize("kind", ["prop", "vehicle"], ids=["scenery", "formation"])
    @pytest.mark.parametrize("placement", ["line", "ring", "grid", "scatter", "single"])
    def test_arena_plane(self, placement, kind, route, measured):
        footprints = _MEASURED if measured else None
        failures = []
        for count, scale in itertools.product(_COUNTS, _SCALES):
            if placement == "single" and count != 1:
                continue
            schema = _sweep_scene(placement, count, scale, kind, route)
            validation = _preflight(schema, footprints)
            assert schema.meta.get("layout_archetype") == "ArenaPlane"
            bad = _rejections(validation) + _body_findings(schema, footprints)
            if bad:
                failures.append(f"count={count} scale={scale}: {bad}")
        assert failures == []

    @pytest.mark.parametrize("kind", ["prop", "vehicle"], ids=["scenery", "formation"])
    @pytest.mark.parametrize("placement", ["line", "ring", "grid", "scatter"])
    def test_bounded_volume(self, placement, kind):
        """Same groups inside the Skyline Glider play box (BoundedVolume, air):
        each altitude band is packed by `_pack_groups`."""
        base = json.loads((_FIXTURES / "skyline_glider.schema.json")
                          .read_text(encoding="utf-8"))
        failures = []
        for count, scale in itertools.product((2, 3, 4, 8, 12), (1.0, 4.0, 10.0)):
            schema = UnitySchema.model_validate(base)
            schema.entities += [
                EntitySpec(name="GroupA", kind=kind, placement=placement,
                           count=count, scale=scale),
                EntitySpec(name="GroupB", kind=kind, placement=placement,
                           count=count, scale=scale * 0.5 + 0.5),
            ]
            validation = _preflight(schema)
            assert schema.meta.get("layout_archetype") == "BoundedVolume"
            bad = _rejections(validation) + _body_findings(schema)
            if bad:
                failures.append(f"count={count} scale={scale}: {bad}")
        assert failures == []
