"""
P1-at-origin / P1-is-start-line normalization (pipeline Step 5, Fixer stage).

Convention this pass completes: for every 3D schema that carries path routes
(EntitySpec.placement == "path", up to 64 [x, y, z(, bank)] points), the
PRIMARY route's first point — "P1" — IS the start/finish line, and P1 sits at
the world origin with the direction of travel aligned to world +Z.

Until now the convention was only half-honored: src/unity_racetrack.py already
anchors its checkpoints, start grid and pole position to centerline[0], but
the loop itself is centered on the origin, so P1 lands wherever the parametric
geometry puts it (the default oval: (36, 42)) and every downstream consumer —
the playable-player merge (unity_generator.py), cameras, HUD, audio cues —
inherits an arbitrary world anchor. LLM-authored routes are worse: P1 can be
anywhere inside the ±2000 clamp.

This pass computes ONE rigid transform (XZ translation + yaw about world Y)
from the primary route and applies it to every world-space artifact the
schema carries at this stage:

  - every path group's `path_points` (y elevation and bank angle preserved)
  - every entity's pre-baked `instance_positions` (e.g. the racetrack start
    grid) and `instance_yaws` (rotated by the same delta)
  - any already-measured `origin_offset`
  - every WorldRegion's bounds (rotated corners re-boxed to an AABB)

One rigid transform for the whole scene preserves all relative geometry —
routes, grids and regions keep their arrangement; only the world anchor
changes. Runs BEFORE src/unity_layout.py's packing pass so groups are packed
(and terrain is sized) in normalized space.

Deterministic: pure geometry, no RNG, no LLM. Idempotent: an already-
normalized schema is returned untouched (empty message list), so schemas
without paths — and re-runs — stay byte-identical downstream.
"""

from __future__ import annotations

import math

# Matches the schema validator's coordinate clamp (unity_schema._clamp_points).
_COORD_LIMIT = 2000.0
# Below these, P1 is considered already at the origin / already heading +Z.
_EPS_POS = 1e-3   # meters
_EPS_YAW = 1e-3   # degrees


def _arc_length_xz(points: list[list[float]]) -> float:
    """Total XZ-plane polyline length (elevation ignored — routes are ranked
    by ground footprint, not climb)."""
    return sum(math.hypot(b[0] - a[0], b[2] - a[2])
               for a, b in zip(points, points[1:]))


def _is_closed(points: list[list[float]]) -> bool:
    """True when the polyline's last point duplicates its first (the closed-
    loop convention used by unity_racetrack._rounded_rect_loop)."""
    if len(points) < 3:
        return False
    a, b = points[0], points[-1]
    return (abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6
            and abs(a[2] - b[2]) < 1e-6)


def _start_direction(points: list[list[float]]) -> tuple[float, float]:
    """Unit XZ direction of travel AT P1. Closed loops use the tangent of the
    segment ARRIVING at P1 (the final straight — same convention as
    unity_racetrack._race_direction, so the start grid lines up along the
    straight it actually sits on); open paths use the outgoing P1 -> P2
    tangent. Degenerate routes default to +Z (no rotation)."""
    if len(points) < 2:
        return 0.0, 1.0
    a, b = (points[-2], points[0]) if _is_closed(points) else (points[0], points[1])
    dx, dz = b[0] - a[0], b[2] - a[2]
    length = math.hypot(dx, dz)
    if length < 1e-9:
        return 0.0, 1.0
    return dx / length, dz / length


def _primary_route(entities):
    """Deterministic primary-route pick: widest `path_width` first (the main
    drivable ribbon — a track's barrier rails are tiled too, and the OUTER
    rail is even longer than the centerline, but it is only ~1 m wide), then
    longest XZ arc length, name as the stable tie-break."""
    paths = [e for e in entities if e.placement == "path" and e.path_points]
    if not paths:
        return None
    return max(paths, key=lambda e: (getattr(e, "path_width", 0.0),
                                     round(_arc_length_xz(e.path_points), 6),
                                     e.name))


def _clamp(v: float) -> float:
    return max(-_COORD_LIMIT, min(_COORD_LIMIT, v))


def _wrap_yaw(deg: float) -> float:
    """Wrap into (-180, 180] — inside the schema validator's [-360, 360]."""
    wrapped = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def _make_transform(p1, fx: float, fz: float):
    """Build the rigid XZ transform (translate P1 to the origin, then rotate
    about world Y so the travel direction (fx, fz) heads +Z). Returns
    (xz_fn, delta_yaw_deg, tx, tz); xz_fn maps (x, z) -> rounded, clamped
    (x', z'). Rotating the scene by Unity-yaw delta maps a vector of yaw d
    to yaw d + delta; delta = -yaw sends the travel direction to yaw 0."""
    yaw = math.degrees(math.atan2(fx, fz))
    delta = -yaw
    rad = math.radians(delta)
    cos_d, sin_d = math.cos(rad), math.sin(rad)
    tx, tz = p1[0], p1[2]

    def _xz(x: float, z: float) -> tuple[float, float]:
        x, z = x - tx, z - tz
        return (round(_clamp(x * cos_d + z * sin_d), 3),
                round(_clamp(-x * sin_d + z * cos_d), 3))

    return _xz, delta, tx, tz


def normalize_route(points: list[list[float]]) -> list[list[float]]:
    """P1-at-origin / heading-+Z normalization for ONE standalone polyline
    (the same convention normalize_paths applies schema-wide) — used by the
    unity-path CLI so an emitted route file is already canonical. Elevation
    (y) and bank (4th component) are preserved."""
    if not points:
        return []
    fx, fz = _start_direction(points)
    _xz, _delta, _tx, _tz = _make_transform(points[0], fx, fz)
    out = []
    for p in points:
        nx, nz = _xz(p[0], p[2])
        out.append([nx, p[1], nz] + list(p[3:4]))
    return out


def normalize_paths(schema) -> list[str]:
    """Rigid-transform the schema so the primary route's P1 sits at the world
    origin with the direction of travel heading +Z. Returns fixer-style log
    messages ([] when there is nothing to do — no 3D path routes, or the
    schema is already normalized)."""
    if schema.game_type in ("2D", "2d_topdown"):
        return []
    primary = _primary_route(schema.entities)
    if primary is None:
        return []

    p1 = primary.path_points[0]
    fx, fz = _start_direction(primary.path_points)
    yaw = math.degrees(math.atan2(fx, fz))     # direction's Unity yaw
    if abs(p1[0]) < _EPS_POS and abs(p1[2]) < _EPS_POS and abs(yaw) < _EPS_YAW:
        return []

    _xz, delta, tx, tz = _make_transform(p1, fx, fz)

    n_paths = n_baked = 0
    for e in schema.entities:
        if e.placement == "path" and e.path_points:
            new_pts = []
            for p in e.path_points:
                nx, nz = _xz(p[0], p[2])
                new_pts.append([nx, p[1], nz] + list(p[3:4]))  # keep bank
            e.path_points = new_pts
            n_paths += 1
        if e.instance_positions:
            new_pos = []
            for p in e.instance_positions:
                nx, nz = _xz(p[0], p[2])
                new_pos.append([nx, p[1], nz])
            e.instance_positions = new_pos
            if e.instance_yaws:
                e.instance_yaws = [round(_wrap_yaw(y + delta), 3)
                                   for y in e.instance_yaws]
            n_baked += 1
        # origin_offset is [0, 0] until the layout pass runs (which is after
        # this pass), but transform any measured offset for robustness.
        if e.origin_offset and (e.origin_offset[0] or e.origin_offset[1]):
            ox, oz = _xz(e.origin_offset[0], e.origin_offset[1])
            e.origin_offset = [ox, oz]

    if schema.world_spec is not None:
        for region in schema.world_spec.regions:
            if len(region.bounds) == 4:
                xmin, zmin, xmax, zmax = region.bounds
                corners = [_xz(x, z) for x in (xmin, xmax) for z in (zmin, zmax)]
                region.bounds = [round(min(c[0] for c in corners), 3),
                                 round(min(c[1] for c in corners), 3),
                                 round(max(c[0] for c in corners), 3),
                                 round(max(c[1] for c in corners), 3)]

    return [
        f"P1 normalization: '{primary.name}' start line (P1) moved to the "
        f"world origin heading +Z (translated ({round(-tx, 3)}, "
        f"{round(-tz, 3)}), rotated {round(delta, 3)} deg) across "
        f"{n_paths} path group(s) and {n_baked} pre-baked group(s)"
    ]
