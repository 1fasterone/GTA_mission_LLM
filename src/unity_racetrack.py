"""
Deterministic race-track scaffold — the racing-genre analog of GTA5's
archetypes.apply_scaffold: when a racing project's LLM-authored entities are
degenerate for the genre (no real drivable vehicle, no track content), inject
a real closed circuit instead of shipping a themed prop scatter that then
hard-fails resolve_entity_assets' racing FAIL-FAST check (unity_pipeline.py,
unity_entity_asset_resolver.py).

Geometry is a parametric rounded-rectangle loop (deterministic, no RNG, no
LLM, no mesh-measurement dependency). Historical note: a Blender-headless
piece-dimension annotation pass was once attempted and blocked (Kenney's
racing-kit road FBX are legacy ASCII FBX Blender can't import), so no
per-piece footprint data exists in Python. That blocker is now resolved at
RUNTIME instead: entities carrying `path_width > 0` are tiled edge-to-edge
by EntitySpawner's SpawnTiledGroup (EntitySpawner.cs.j2), which measures the
resolved model's renderer bounds in Unity, width-normalizes it to
path_width meters, and derives the segment count from the loop's arc length
— the road surface and barrier rails read as one continuous circuit.
Entities without path_width keep the even waypoint spread (cones, banners).
Every entity still resolves through the normal genre-aware catalog match
(unity_entity_asset_resolver.py); this module only decides WHAT entities to
author, never which model file backs them.
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


def _is_closed(points: list[list[float]]) -> bool:
    """True when the polyline's last point duplicates its first (the closed-
    loop convention _rounded_rect_loop uses)."""
    return len(points) > 2 and points[0][:3] == points[-1][:3]


def _race_direction(centerline: list[list[float]]) -> tuple[float, float]:
    """Unit race direction AT the start/finish line (centerline[0]): for a
    closed loop, the tangent of the segment ARRIVING at it — i.e. the final
    straight (the outgoing segment already curves into turn 1, which used to
    skew the start grid a few degrees off the straight it actually sits on);
    for an open route (a custom point-to-point course from `unity-create
    --route`), the outgoing centerline[0] -> [1] tangent."""
    if _is_closed(centerline):
        return _unit_xz(centerline[-2], centerline[0])
    if len(centerline) > 1:
        return _unit_xz(centerline[0], centerline[1])
    return 0.0, 1.0


def _offset_polyline(points: list[list[float]], dist: float) -> list[list[float]]:
    """Polyline offset by `dist` meters perpendicular to the local direction
    of travel (positive = right-hand side in the XZ plane, matching
    _start_grid's perpendicular). Elevation rides along; a closed input
    stays closed. Used to derive barrier rails from a CUSTOM centerline —
    the default rounded-rect circuit keeps its exact parametric rails."""
    closed = _is_closed(points)
    core = points[:-1] if closed else points
    n = len(core)
    out: list[list[float]] = []
    for i, p in enumerate(core):
        a = core[(i - 1) % n] if closed else core[max(0, i - 1)]
        b = core[(i + 1) % n] if closed else core[min(n - 1, i + 1)]
        fx, fz = _unit_xz(a, b)
        rx, rz = -fz, fx
        out.append([round(p[0] + rx * dist, 3), p[1],
                    round(p[2] + rz * dist, 3)])
    if closed:
        out.append(list(out[0]))
    return out


def _xz_bbox_area(points: list[list[float]]) -> float:
    xs = [p[0] for p in points]
    zs = [p[2] for p in points]
    return (max(xs) - min(xs)) * (max(zs) - min(zs))


def _start_yaw(centerline: list[list[float]]) -> float:
    """Race direction at the start line as a Unity yaw (degrees about world
    Y; 0 = +Z, clockwise positive) — baked onto the start-grid karts as
    `instance_yaws` so they face down the track instead of world +Z."""
    fx, fz = _race_direction(centerline)
    return round(math.degrees(math.atan2(fx, fz)), 3)


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
    fx, fz = _race_direction(centerline)
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


def compose_racetrack(schema, centerline: list[list[float]] | None = None,
                      track_width: float | None = None) -> list[str]:
    """Inject a closed circuit (road ribbon, inner/outer barriers, apex cones,
    checkpoint banners, a start-grid of karts) into schema.entities.

    Two entry modes:
      - Default (centerline=None): the parametric rounded-rectangle oval —
        called when genre == 'racing' and the caller (unity_pipeline.run)
        gated on is_racing_degenerate, mirroring GTA5's apply_scaffold
        "trust non-degenerate LLM output" contract. Byte-identical to the
        historical scaffold.
      - Custom route (centerline=..., from `unity-create --route` — a text
        point spec or a traced image, see src/unity_pathinput.py): the same
        circuit furniture built around the AUTHORED centerline; barrier
        rails are derived by perpendicular polyline offset, and open
        (point-to-point) routes are supported — the grid still forms behind
        P1 in the direction of travel.

    Idempotent against re-running is NOT guaranteed (by design — it should
    only ever run once); always append, never mutate existing entities.
    Returns log messages in the same style as other schema fixers.
    """
    msgs: list[str] = []

    custom = centerline is not None
    width = (track_width if track_width and track_width > 0 else _TRACK_WIDTH)
    if not custom:
        width = _TRACK_WIDTH
        centerline = _rounded_rect_loop(_HALF_WIDTH, _HALF_LENGTH, _CORNER_RADIUS)
        outer = _rounded_rect_loop(_HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
                                   _HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
                                   _CORNER_RADIUS + _TRACK_WIDTH / 2 + _BARRIER_MARGIN)
        inner = _rounded_rect_loop(max(4.0, _HALF_WIDTH - _TRACK_WIDTH / 2 - _BARRIER_MARGIN),
                                   max(4.0, _HALF_LENGTH - _TRACK_WIDTH / 2 - _BARRIER_MARGIN),
                                   max(1.0, _CORNER_RADIUS - _TRACK_WIDTH / 2 - _BARRIER_MARGIN))
    else:
        assert centerline is not None, "custom is only True when centerline was provided"
        centerline = [list(p) for p in centerline]
        rail = width / 2 + _BARRIER_MARGIN
        side_a = _offset_polyline(centerline, rail)
        side_b = _offset_polyline(centerline, -rail)
        # Closed loops: the larger-footprint side is the outer rail. Open
        # routes have no enclosing side — the labels are arbitrary but the
        # pick is stable (bbox area, then the +offset side).
        outer, inner = ((side_a, side_b)
                        if _xz_bbox_area(side_a) >= _xz_bbox_area(side_b)
                        else (side_b, side_a))
    apex_cones = _every_nth(inner, 3)
    checkpoints = [centerline[0], centerline[len(centerline) // 4],
                  centerline[len(centerline) // 2], centerline[3 * len(centerline) // 4]]

    new_entities = [
        # path_width > 0 = SpawnTiledGroup tile mode: count/scale below are
        # only the fallback when the resolved model has no measurable bounds.
        EntitySpec(name="TrackSurface", kind="road", count=len(centerline),
                   shape="cube", placement="path", color_role="secondary",
                   scale=width * 0.4, model_hint="road straight racing track",
                   path_points=centerline, path_width=width),
        EntitySpec(name="TrackBarriersOuter", kind="wall", count=len(outer),
                   shape="cube", placement="path", color_role="trim",
                   scale=1.5, model_hint="barrier fence guardrail",
                   path_points=outer, path_width=1.0),
        EntitySpec(name="TrackBarriersInner", kind="wall", count=len(inner),
                   shape="cube", placement="path", color_role="trim",
                   scale=1.5, model_hint="barrier fence guardrail",
                   path_points=inner, path_width=1.0),
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
                   # track LOOP's center, not the start line. The generator
                   # merges this group INTO the scene's playable Player
                   # (position, facing, model) when a player script exists.
                   instance_positions=[[centerline[0][0], 0.0, centerline[0][2]]],
                   instance_yaws=[_start_yaw(centerline)]),
        EntitySpec(name="OpponentKarts", kind="vehicle", count=3,
                   shape="cube", placement="grid", spacing=4.0, radius=6.0,
                   color_role="accent", scale=1.0,
                   model_hint="kart racer opponent vehicle", tag="Enemy",
                   instance_positions=_start_grid(centerline, 3, width),
                   instance_yaws=[_start_yaw(centerline)] * 3),
    ]
    prior_entities = {e.name: e for e in schema.entities}
    schema.entities = list(schema.entities) + new_entities

    # Register the circuit in per-scene entity subsets. SceneSpec semantics
    # (unity_schema.SceneSpec.entities): None = every group spawns there;
    # an explicit list gates spawning. The circuit is injected AFTER the LLM
    # authored those lists, so without this step a subset-gated schema shows
    # the circuit in ZERO scenes (EntitySpawner._sceneGroups drops it all).
    # A scene qualifies when its subset already names track content — an
    # entity with path_points or a road/track kind (i.e. a driving scene).
    def _is_track_content(name: str) -> bool:
        e = prior_entities.get(name)
        return e is not None and (
            bool(getattr(e, "path_points", None))
            or getattr(e, "kind", "") in ("road", "track"))

    gated = [s for s in schema.scene_specs if s.entities is not None]
    if gated:
        targets = [s for s in gated if any(_is_track_content(n) for n in s.entities)]
        if not targets:
            # No gated scene already names track content. Forcing it into an
            # arbitrary gated scene (e.g. "the first one") is only safe when
            # NO scene shows every entity by default — otherwise that
            # unrestricted scene already renders the circuit correctly, and
            # forcing a semantically wrong scene (hit live: an unnamed
            # "RaceTrack" scene had an explicit entities=None spec, so it
            # already hosted the circuit; the old code still force-added it
            # to "MainMenu", the only OTHER, gated scene, putting a race
            # track in the main menu) does actual harm instead of preventing
            # invisibility. A scene counts as unrestricted whether that's an
            # explicit entities=None SceneSpec or no SceneSpec entry at all.
            covered = {s.name for s in schema.scene_specs}
            unrestricted = ({s.name for s in schema.scene_specs
                             if s.entities is None}
                            | (set(schema.scenes) - covered))
            if not unrestricted:
                targets = gated[:1]
        new_names = [e.name for e in new_entities]
        for s in targets:
            s.entities = list(s.entities) + [n for n in new_names
                                             if n not in s.entities]
        if targets:
            msgs.append("Registered circuit entities in scene subset(s): "
                        + ", ".join(s.name for s in targets))
    if custom:
        msgs.append(
            f"Composed circuit from an authored route ({len(centerline)} "
            f"points, {'closed loop' if _is_closed(centerline) else 'open course'}, "
            f"{width:g} m wide) — track surface, offset barrier rails, apex "
            "cones, 4 checkpoint banners, a player kart, 3 opponent karts"
        )
    else:
        msgs.append(
            "Racing genre with no vehicle/track content — injected a deterministic "
            "closed-circuit scaffold (TrackSurface, inner/outer barriers, apex "
            "cones, 4 checkpoint banners, a player kart, 3 opponent karts)"
        )

    # WorldRegion is a real schema modality (world_spec.regions, bounds
    # [xmin,zmin,xmax,zmax]) that nothing else in the pipeline populates yet —
    # this scaffold is its first real consumer, describing the circuit's
    # world-space footprint for any future region-aware tooling.
    if custom:
        _oxs = [p[0] for p in outer]
        _ozs = [p[2] for p in outer]
        _bounds = [round(min(_oxs), 3), round(min(_ozs), 3),
                   round(max(_oxs), 3), round(max(_ozs), 3)]
    else:
        _bounds = [-(_HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN),
                   -(_HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN),
                   _HALF_WIDTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN,
                   _HALF_LENGTH + _TRACK_WIDTH / 2 + _BARRIER_MARGIN]
    region = WorldRegion(
        name="Racetrack", biome="track",
        entities=[e.name for e in new_entities],
        bounds=_bounds,
    )
    if schema.world_spec is None:
        schema.world_spec = WorldSpec(regions=[region])
    else:
        schema.world_spec.regions = list(schema.world_spec.regions) + [region]

    return msgs
