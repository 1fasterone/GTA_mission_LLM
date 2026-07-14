"""
Deterministic race-track scaffold — the racing-genre analog of GTA5's
archetypes.apply_scaffold: when a racing project's LLM-authored entities are
degenerate for the genre (no real drivable vehicle, no track content), inject
a real closed circuit instead of shipping a themed prop scatter that then
hard-fails resolve_entity_assets' racing FAIL-FAST check (unity_pipeline.py,
unity_entity_asset_resolver.py).

Geometry is a parametric rounded-rectangle loop (deterministic, no RNG, no
LLM, no mesh-measurement dependency) — a prerequisite Blender-headless
piece-dimension annotation pass was attempted live and blocked: Kenney's
racing-kit road FBX files are legacy ASCII FBX, which the installed Blender
(5.1.2) cannot import at all ("ASCII FBX files are not supported"), so no
reliable per-piece footprint/socket data exists to tile oriented road
segments edge-to-edge. This is the documented fallback: a waypoint-spline
track — barriers, cones, checkpoint banners, and a road ribbon placed along
the loop via the existing EntitySpawner "path" placement (which now also
faces each instance along the path tangent — see EntitySpawner.cs.j2), plus
a real kart on a start grid. Every entity still resolves through the normal
genre-aware catalog match (unity_entity_asset_resolver.py); this module only
decides WHAT entities to author, never which model file backs them.
"""

from __future__ import annotations

import math

from .unity_schema import EntitySpec, WorldRegion, WorldSpec

# Half-extents of the default circuit (meters) — a modest oval that reads as
# a real track without dwarfing everything else in the scene.
_HALF_LENGTH = 60.0
_HALF_WIDTH = 36.0
_CORNER_RADIUS = 18.0
_ARC_POINTS_PER_CORNER = 8          # smoothness; 4*(n+1)+1 <= 64 (n<=14 room)
_TRACK_WIDTH = 10.0
_BARRIER_MARGIN = 1.5               # gap between track edge and barrier ring

_RACING_CORE_KINDS = frozenset({"vehicle", "road", "track"})


def _rounded_rect_loop(half_w: float, half_l: float, radius: float,
                        arc_points: int = _ARC_POINTS_PER_CORNER) -> list[list[float]]:
    """Closed CCW polyline tracing a rounded rectangle centered on the origin
    in the XZ plane (y=0). radius is clamped so the arcs never exceed the
    rectangle's own half-extents. Deterministic pure geometry."""
    r = max(1.0, min(radius, half_w - 1.0, half_l - 1.0))
    iw, il = half_w - r, half_l - r
    centers = [(iw, il), (-iw, il), (-iw, -il), (iw, -il)]
    starts = (0.0, 90.0, 180.0, 270.0)
    pts: list[list[float]] = []
    for (cx, cz), start_deg in zip(centers, starts):
        for k in range(arc_points + 1):
            ang = math.radians(start_deg + 90.0 * k / arc_points)
            pts.append([round(cx + r * math.cos(ang), 3), 0.0,
                        round(cz + r * math.sin(ang), 3)])
    pts.append(pts[0])
    return pts


def _unit_xz(a: list[float], b: list[float]) -> tuple[float, float]:
    """Unit vector from point a to point b in the XZ plane (ignores y)."""
    dx, dz = b[0] - a[0], b[2] - a[2]
    length = math.hypot(dx, dz) or 1.0
    return dx / length, dz / length


def _start_grid(centerline: list[list[float]], count: int,
                 lane_width: float, row_spacing: float = 4.0) -> list[list[float]]:
    """Deterministic starting-grid positions anchored to the track's actual
    start/finish line (centerline[0] — the same point CheckpointBanners[0]
    marks), staggered two-wide behind the line so karts read as lined up ON
    the track instead of floating wherever the schema's authored
    radius/spacing happened to place them relative to the whole scene's
    origin (which is centered on the track loop itself, not the start
    line)."""
    start = centerline[0]
    nxt = centerline[1] if len(centerline) > 1 else centerline[0]
    fx, fz = _unit_xz(start, nxt)
    rx, rz = -fz, fx  # right-hand perpendicular in the XZ plane
    lane_gap = lane_width * 0.28
    positions = []
    for i in range(count):
        row, col = divmod(i, 2)
        lateral = (col - 0.5) * lane_gap * 2.0
        longitudinal = -(row + 1) * row_spacing
        positions.append([round(start[0] + rx * lateral + fx * longitudinal, 3),
                          0.0,
                          round(start[2] + rz * lateral + fz * longitudinal, 3)])
    return positions


def _every_nth(points: list[list[float]], n: int) -> list[list[float]]:
    """Sparse subsample of a closed loop (keeps the closing duplicate)."""
    if n <= 1 or len(points) <= n:
        return list(points)
    out = [p for i, p in enumerate(points[:-1]) if i % n == 0]
    out.append(points[0])
    return out


def is_racing_degenerate(schema) -> bool:
    """True when a racing-genre schema has no real drivable vehicle AND no
    track/road content — the LLM's entities don't reflect the genre at all."""
    return not any((e.kind or "").lower() in _RACING_CORE_KINDS
                   for e in schema.entities)


def compose_racetrack(schema) -> list[str]:
    """Inject a closed circuit (road ribbon, inner/outer barriers, apex cones,
    checkpoint banners, a start-grid of karts) into schema.entities.

    Only called when genre == 'racing'; caller (unity_pipeline.run) is
    expected to gate on is_racing_degenerate first, mirroring GTA5's
    apply_scaffold "trust non-degenerate LLM output" contract. Idempotent
    against re-running is NOT guaranteed (by design — it should only ever
    run once, on a degenerate schema); always append, never mutate existing
    entities. Returns log messages in the same style as other schema fixers.
    """
    msgs: list[str] = []

    centerline = _rounded_rect_loop(_HALF_WIDTH, _HALF_LENGTH, _CORNER_RADIUS)
    outer = _rounded_rect_loop(_HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
                               _HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
                               _CORNER_RADIUS + _TRACK_WIDTH / 2 + _BARRIER_MARGIN)
    inner = _rounded_rect_loop(max(4.0, _HALF_WIDTH - _TRACK_WIDTH / 2 - _BARRIER_MARGIN),
                               max(4.0, _HALF_LENGTH - _TRACK_WIDTH / 2 - _BARRIER_MARGIN),
                               max(1.0, _CORNER_RADIUS - _TRACK_WIDTH / 2 - _BARRIER_MARGIN))
    apex_cones = _every_nth(inner, 3)
    checkpoints = [centerline[0], centerline[len(centerline) // 4],
                  centerline[len(centerline) // 2], centerline[3 * len(centerline) // 4]]

    new_entities = [
        EntitySpec(name="TrackSurface", kind="road", count=len(centerline),
                   shape="cube", placement="path", color_role="secondary",
                   scale=_TRACK_WIDTH * 0.4, model_hint="road straight racing track",
                   path_points=centerline),
        EntitySpec(name="TrackBarriersOuter", kind="wall", count=len(outer),
                   shape="cube", placement="path", color_role="trim",
                   scale=1.5, model_hint="barrier fence guardrail",
                   path_points=outer),
        EntitySpec(name="TrackBarriersInner", kind="wall", count=len(inner),
                   shape="cube", placement="path", color_role="trim",
                   scale=1.5, model_hint="barrier fence guardrail",
                   path_points=inner),
        EntitySpec(name="ApexCones", kind="obstacle", count=len(apex_cones),
                   shape="cylinder", placement="path", color_role="accent",
                   scale=0.6, model_hint="traffic cone", path_points=apex_cones),
        EntitySpec(name="CheckpointBanners", kind="decoration", count=len(checkpoints),
                   shape="cube", placement="path", color_role="primary",
                   scale=2.0, model_hint="checkpoint banner finish line",
                   path_points=checkpoints),
        EntitySpec(name="PlayerKart", kind="vehicle", count=1,
                   shape="cube", placement="single", color_role="primary",
                   scale=1.0, model_hint="kart racer vehicle",
                   tag="Player",
                   # Pole position: right on the start/finish line, centered
                   # across the track — not the scene origin, which is the
                   # track LOOP's center, not the start line.
                   instance_positions=[[centerline[0][0], 0.0, centerline[0][2]]]),
        EntitySpec(name="OpponentKarts", kind="vehicle", count=3,
                   shape="cube", placement="grid", spacing=4.0, radius=6.0,
                   color_role="accent", scale=1.0,
                   model_hint="kart racer opponent vehicle", tag="Enemy",
                   instance_positions=_start_grid(centerline, 3, _TRACK_WIDTH)),
    ]
    schema.entities = list(schema.entities) + new_entities
    msgs.append(
        "Racing genre with no vehicle/track content — injected a deterministic "
        "closed-circuit scaffold (TrackSurface, inner/outer barriers, apex "
        "cones, 4 checkpoint banners, a player kart, 3 opponent karts)"
    )

    # WorldRegion is a real schema modality (world_spec.regions, bounds
    # [xmin,zmin,xmax,zmax]) that nothing else in the pipeline populates yet —
    # this scaffold is its first real consumer, describing the circuit's
    # world-space footprint for any future region-aware tooling.
    region = WorldRegion(
        name="Racetrack", biome="track",
        entities=[e.name for e in new_entities],
        bounds=[-(_HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN),
               -(_HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN),
               _HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
               _HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN],
    )
    if schema.world_spec is None:
        schema.world_spec = WorldSpec(regions=[region])
    else:
        schema.world_spec.regions = list(schema.world_spec.regions) + [region]

    return msgs
