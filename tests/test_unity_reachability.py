"""Grid/graph reachability (src/unity_reachability.py) — Gap D of
docs/COMPILER_AUDIT.md: an axis-aligned box test reads content behind a wall
as reachable, and a sorted-by-height walk reads two holds as "connected"
even when nothing but a shared Y value puts them anywhere near each other.
"""
from __future__ import annotations

import math

from src.unity_reachability import flood_fill_reachable, wall_climb_reachable
from src.unity_schema import EntitySpec


def _wall_row(z: float, x_from: float, x_to: float, step: float = 0.5) -> EntitySpec:
    """A dense row of `wall`-kind instances at a fixed z, close enough
    together that no gap survives grid quantization -- a barrier spanning
    the full width it's given."""
    n = int(round((x_to - x_from) / step)) + 1
    pts = [[round(x_from + i * step, 3), 0.0, z] for i in range(n)]
    return EntitySpec(name="Wall", kind="wall", count=len(pts),
                      scale=1.5, instance_positions=pts)


class TestFloodFillReachable:
    def test_open_room_reaches_everything_inside_bounds(self):
        result = flood_fill_reachable(
            entities=[],
            bounds=(-10.0, -10.0, 10.0, 10.0),
            spawn_xz=(0.0, 0.0),
            targets={"Coin": [(5.0, 5.0)], "Gem": [(-8.0, -8.0)]})
        assert result.reachable == {"Coin", "Gem"}
        assert not result.unreachable

    def test_a_full_span_wall_seals_off_the_far_side(self):
        """The exact defect Gap D names: content inside the box, behind a
        wall, used to read as reachable because a box test never asks
        whether the space between two points is open."""
        wall = _wall_row(z=0.0, x_from=-10.0, x_to=10.0)
        result = flood_fill_reachable(
            entities=[wall],
            bounds=(-10.0, -10.0, 10.0, 10.0),
            spawn_xz=(0.0, -5.0),
            targets={"NearCoin": [(0.0, -8.0)], "FarCoin": [(0.0, 8.0)]})
        assert "NearCoin" in result.reachable
        assert "FarCoin" in result.unreachable

    def test_a_partial_wall_can_be_walked_around(self):
        """A barrier that doesn't span the whole bounds leaves an opening —
        this is not a maze solver refusing to find the long way round, it's
        a flood fill, and it should find it."""
        wall = _wall_row(z=0.0, x_from=-10.0, x_to=2.0)   # gap from x=2..10
        result = flood_fill_reachable(
            entities=[wall],
            bounds=(-10.0, -10.0, 10.0, 10.0),
            spawn_xz=(0.0, -5.0),
            targets={"FarCoin": [(0.0, 8.0)]})
        assert "FarCoin" in result.reachable

    def test_non_blocking_kinds_never_block(self):
        """`prop`/`collectible`/etc are not in BLOCKING_KINDS — the checklist
        is deliberately narrow so scenery the player walks around at runtime
        doesn't read as an impassable wall."""
        scenery = EntitySpec(name="Trees", kind="prop", count=1,
                             instance_positions=[[0.0, 0.0, 0.0]])
        result = flood_fill_reachable(
            entities=[scenery],
            bounds=(-5.0, -5.0, 5.0, 5.0),
            spawn_xz=(0.0, 0.0),
            targets={"Coin": [(4.0, 4.0)]})
        assert "Coin" in result.reachable

    def test_the_spawn_cell_is_never_treated_as_blocked(self):
        """Something sitting ON the spawn cell is `_check_spawn_clear`'s
        finding to make; it must not also zero out every other group's
        reachability by starting the flood fill from a blocked cell."""
        blocker = EntitySpec(name="Crate", kind="obstacle", count=1,
                             instance_positions=[[0.0, 0.0, 0.0]])
        result = flood_fill_reachable(
            entities=[blocker],
            bounds=(-5.0, -5.0, 5.0, 5.0),
            spawn_xz=(0.0, 0.0),
            targets={"Coin": [(4.0, 4.0)]})
        assert "Coin" in result.reachable

    def test_a_ring_of_walls_seals_its_own_interior(self):
        ring: list[list[float]] = []
        for deg in range(0, 360, 5):
            x = 6.0 * math.cos(math.radians(deg))
            z = 6.0 * math.sin(math.radians(deg))
            ring.append([round(x, 3), 0.0, round(z, 3)])
        # A generous scale (block radius = scale * 0.6, see
        # _instance_block_radius) so consecutive ring points' footprints
        # overlap well past any grid-quantization diagonal gap — this test
        # is about the flood fill correctly refusing to cross a sealed ring,
        # not about pinning down exactly how fine a wall the grid resolves.
        wall = EntitySpec(name="RoomWalls", kind="wall", count=len(ring),
                          scale=2.5, instance_positions=ring)
        result = flood_fill_reachable(
            entities=[wall],
            bounds=(-20.0, -20.0, 20.0, 20.0),
            spawn_xz=(15.0, 0.0),
            targets={"Inside": [(0.0, 0.0)], "Outside": [(-15.0, 0.0)]})
        assert "Inside" in result.unreachable
        assert "Outside" in result.reachable


class TestWallClimbReachable:
    def test_a_clean_zigzag_chain_reaches_the_top(self):
        holds = [(0.0, 2.0, 0.2), (1.5, 4.0, 0.2), (0.0, 6.0, 0.2)]
        hit, ceiling = wall_climb_reachable(holds, [(0.0, 6.5, 0.2)], reach=2.5)
        assert hit == {0}
        assert ceiling == 6.0

    def test_horizontally_displaced_same_height_holds_are_not_connected(self):
        """The bug a sorted-Y-only walk cannot see: two holds can share
        (near enough) a height and still be nowhere near each other."""
        holds = [(0.0, 2.0, 0.2), (0.0, 4.0, 0.2), (50.0, 6.0, 0.2)]
        hit, ceiling = wall_climb_reachable(
            holds, [(0.0, 4.5, 0.2), (50.0, 6.5, 0.2)], reach=2.5)
        # The first target sits near the connected base chain (tops out at
        # y=4); the second sits near the stranded hold 50 m away, which a
        # plain sorted-Y walk would have called reachable (gaps 2, 2 <= 2.5).
        assert hit == {0}
        assert ceiling == 4.0

    def test_ground_attachment_respects_the_players_own_spawn_height(self):
        hit, ceiling = wall_climb_reachable(
            [(0.0, 5.0, 0.0)], [(0.0, 5.5, 0.0)], reach=2.0, start_y=0.0)
        assert hit == set()          # 5.0 m is out of a 2.0 m reach from y=0
        hit, ceiling = wall_climb_reachable(
            [(0.0, 5.0, 0.0)], [(0.0, 5.5, 0.0)], reach=2.0, start_y=3.0)
        assert hit == {0}            # but well within reach from y=3

    def test_no_holds_still_grades_ground_level_targets(self):
        hit, ceiling = wall_climb_reachable([], [(0.0, 1.0, 0.0), (0.0, 40.0, 0.0)],
                                            reach=2.5)
        assert hit == {0}
        assert ceiling == 0.0
