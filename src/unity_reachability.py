"""
Grid/graph reachability (pipeline Step 7, Gap D of docs/COMPILER_AUDIT.md).

Two of the playability evaluator's own checks used to grade "is this a real
path" with something that never asked what stood between two points:

  * `content_reachable` tested axis-aligned bounding-box CONTAINMENT — a
    coin inside the scene's ground bounds but sealed behind a wall read as
    reachable, because a box test never asks whether the space between the
    spawn and the coin is open.
  * `objective_kinematics`'s wall-climbing branch sorted hold instances by Y
    ALONE and walked bottom-up, breaking at the first vertical gap wider
    than the reach constant — two holds could share a height and read as
    "connected" while sitting on opposite sides of the wall with nothing
    joining them, because the walk never looked at X.

This module replaces both with an actual graph walk: a 2-D occupancy grid
flood-fill for ground movement, and a 3-D proximity graph BFS for the hold
chain. Both stay schema-only and deterministic, matching Step 7's own
constraint (it grades a schema before real Unity geometry exists) — blocking
footprints are approximated from the same `scale` field
src/unity_layout.py already uses to size an unmeasured "single" placement,
not from a measured collider.

The gap this module exists to close is mostly moot for scenes
`src/unity_layout.py` packed itself: `_pack_groups` already keeps every
group's footprint circle from overlapping any other, so nothing legitimately
ends up geometrically "inside" a solid group's circle. It matters for
content whose positions were baked outside that packer — most concretely, a
hand-authored Scene Compiler layout (src/unity_sceneinput.py /
unity_scenebind.py bakes `instance_positions` straight from a 3-D editor,
walls included, with no circle-packing guarantee at all).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .unity_schema import EntitySpec

# Kinds that are SOLID in plan view: a player-shaped thing cannot pass
# through the footprint of an instance of one of these. Deliberately a small
# allowlist rather than unity_playability's `_SCENERY_KINDS` denylist — most
# scenery (trees, decor, furniture) is walked around by the player's own
# collider at runtime, not the kind of large, deliberately-enclosing
# geometry this check exists to catch, and an allowlist keeps a kind nobody
# authored as a barrier from reading as one.
BLOCKING_KINDS: frozenset[str] = frozenset({"wall", "building", "obstacle", "rock"})

# Grid resolution for the ground flood-fill. A cell has to be narrower than
# the thinnest blocker this module means to catch — a "single"-placement
# blocker's own minimum radius is 0.5 m, see
# src/unity_layout.py._footprint_radius — or a one-cell gap in a wall reads
# as open when it is not. Capped by total cell count so a scene with a very
# large terrain_bounds (the "drive"/"fly" ground margin) still runs in
# milliseconds.
_MIN_CELL = 0.75
_MAX_CELLS_PER_AXIS = 160


def _instance_block_radius(entity: EntitySpec) -> float:
    """Per-INSTANCE solid radius. `group_radius` is the footprint of the
    whole packed GROUP (many instances spread across a ring/line/scatter),
    not of any one instance standing in the path, so this mirrors
    src/unity_layout.py's own approximation for an unmeasured "single"
    placement instead."""
    return max(0.5, entity.scale * 0.6)


@dataclass
class ReachabilityResult:
    reachable: set[str] = field(default_factory=set)
    unreachable: set[str] = field(default_factory=set)
    cell_size: float = 0.0
    grid_dims: tuple[int, int] = (0, 0)


def _grid_dims(xmin: float, zmin: float, xmax: float, zmax: float) -> tuple[float, int, int]:
    width, depth = max(xmax - xmin, 1e-6), max(zmax - zmin, 1e-6)
    cell = max(_MIN_CELL, width / _MAX_CELLS_PER_AXIS, depth / _MAX_CELLS_PER_AXIS)
    cols = max(1, min(_MAX_CELLS_PER_AXIS, math.ceil(width / cell)))
    rows = max(1, min(_MAX_CELLS_PER_AXIS, math.ceil(depth / cell)))
    return cell, cols, rows


def flood_fill_reachable(
    entities: Sequence[EntitySpec],
    bounds: tuple[float, float, float, float],
    spawn_xz: tuple[float, float],
    targets: dict[str, list[tuple[float, float]]],
) -> ReachabilityResult:
    """BFS out from `spawn_xz` over a 2-D occupancy grid covering `bounds`,
    blocked wherever a `BLOCKING_KINDS` instance's footprint lands.
    `targets` maps a group name to the XZ points to test (its baked
    instances) — a name is reachable if ANY of its points falls in a cell
    the flood fill reached, or in a still-open cell immediately adjacent to
    one (grid quantization: a point one cell short of the fill boundary is a
    rounding artifact of cell size, not a wall).

    The player's own spawn cell is always treated as open, whatever sits on
    it — a group trapping the player at t=0 is `_check_spawn_clear`'s
    finding to make, not a reason for every other check in the file to read
    as "nothing is reachable from spawn".
    """
    xmin, zmin, xmax, zmax = bounds
    cell, cols, rows = _grid_dims(xmin, zmin, xmax, zmax)
    blocked = [[False] * cols for _ in range(rows)]

    def cell_of(x: float, z: float) -> tuple[int, int]:
        c = min(cols - 1, max(0, int((x - xmin) / cell)))
        r = min(rows - 1, max(0, int((z - zmin) / cell)))
        return c, r

    for e in entities:
        if (e.kind or "").strip().lower() not in BLOCKING_KINDS:
            continue
        br = _instance_block_radius(e)
        span = max(1, math.ceil(br / cell))
        for p in (e.instance_positions or []):
            if not (xmin <= p[0] <= xmax and zmin <= p[2] <= zmax):
                continue    # graded separately by the AABB "over the void" test
            c0, r0 = cell_of(p[0], p[2])
            for dr in range(-span, span + 1):
                for dc in range(-span, span + 1):
                    c, r = c0 + dc, r0 + dr
                    if not (0 <= c < cols and 0 <= r < rows):
                        continue
                    # Circle-in-cell, not square-in-square: a diagonal
                    # neighbour outside the actual footprint radius stays
                    # open, so a thin blocker does not fatten into a bigger
                    # square than it is.
                    cx, cz = xmin + (c + 0.5) * cell, zmin + (r + 0.5) * cell
                    if math.hypot(cx - p[0], cz - p[2]) <= br + cell * 0.5:
                        blocked[r][c] = True

    # Clear a small disk around the player's own spawn, not just its one
    # cell: a blocking instance sitting AT (or near) spawn marks a multi-cell
    # window (see `span` above), and unblocking only the exact spawn cell
    # left every one of its neighbours still blocked — the flood fill had
    # nowhere to go before it had even started. Something trapping the
    # player at t=0 is `_check_spawn_clear`'s finding to make, not a reason
    # for this flood fill to start caged.
    sc, sr = cell_of(spawn_xz[0], spawn_xz[1])
    clearance = max(2.0, cell * 2.0)
    clear_span = max(1, math.ceil(clearance / cell))
    for dr in range(-clear_span, clear_span + 1):
        for dc in range(-clear_span, clear_span + 1):
            c, r = sc + dc, sr + dr
            if not (0 <= c < cols and 0 <= r < rows):
                continue
            cx, cz = xmin + (c + 0.5) * cell, zmin + (r + 0.5) * cell
            if math.hypot(cx - spawn_xz[0], cz - spawn_xz[1]) <= clearance:
                blocked[r][c] = False

    reached = [[False] * cols for _ in range(rows)]
    reached[sr][sc] = True
    queue = [(sc, sr)]
    head = 0
    while head < len(queue):
        c, r = queue[head]
        head += 1
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nc, nr = c + dc, r + dr
            if (0 <= nc < cols and 0 <= nr < rows
                    and not reached[nr][nc] and not blocked[nr][nc]):
                reached[nr][nc] = True
                queue.append((nc, nr))

    def is_reachable(x: float, z: float) -> bool:
        c, r = cell_of(x, z)
        for dc, dr in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            nc, nr = c + dc, r + dr
            if 0 <= nc < cols and 0 <= nr < rows and reached[nr][nc]:
                return True
        return False

    out = ReachabilityResult(cell_size=cell, grid_dims=(cols, rows))
    for name, pts in targets.items():
        if any(is_reachable(x, z) for x, z in pts):
            out.reachable.add(name)
        else:
            out.unreachable.add(name)
    return out


def wall_climb_reachable(
    holds: Sequence[tuple[float, float, float]],
    targets: Sequence[tuple[float, float, float]],
    reach: float,
    start_y: float = 0.0,
) -> tuple[set[int], float]:
    """BFS over the hold chain as a real graph — 3-D edges between holds
    actually within `reach` of each other, not a sorted list of heights.
    Two holds can share a Y value while sitting on opposite sides of the
    wall with nothing connecting them; a plain sorted-Y walk read them as
    adjacent anyway.

    A hold is reachable from the ground directly when its height above
    `start_y` (the player's own spawn altitude, 0.0 for the ordinary case) is
    within `reach` — the player walks the base freely, so no horizontal
    budget is spent getting to the first hold, matching
    src/unity_layout.py's own `_hold_chain`, which never validates a
    spawn-to-first-hold gap either. Every other edge is full 3-D distance.

    Returns the set of `targets` indices reachable (within `reach` of the
    ground or of any hold the chain actually connects to) and the ceiling —
    the highest Y among reached holds (or the floor, if none).
    """
    floor_y = max(0.0, start_y)
    n = len(holds)
    reach2 = reach * reach
    adj: list[list[int]] = [[] for _ in range(n)]
    reached = [False] * n
    queue: list[int] = []
    for i in range(n):
        hx, hy, hz = holds[i]
        if hy - floor_y <= reach:
            reached[i] = True
            queue.append(i)
        for j in range(i + 1, n):
            ox, oy, oz = holds[j]
            if (hx - ox) ** 2 + (hy - oy) ** 2 + (hz - oz) ** 2 <= reach2:
                adj[i].append(j)
                adj[j].append(i)
    head = 0
    while head < len(queue):
        i = queue[head]
        head += 1
        for j in adj[i]:
            if not reached[j]:
                reached[j] = True
                queue.append(j)

    ceiling = max((holds[i][1] for i in range(n) if reached[i]), default=floor_y)
    hit: set[int] = set()
    for k in range(len(targets)):
        tx, ty, tz = targets[k]
        if ty - floor_y <= reach:
            hit.add(k)
            continue
        for i in range(n):
            if not reached[i]:
                continue
            hx, hy, hz = holds[i]
            if (tx - hx) ** 2 + (ty - hy) ** 2 + (tz - hz) ** 2 <= reach2:
                hit.add(k)
                break
    return hit, ceiling
