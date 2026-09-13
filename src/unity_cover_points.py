"""
Cover-point sidecar (pipeline Step 5d) — deterministic geometry the
`cover_shooter` AI variant reads instead of the naive advance/retreat math it
shipped with (see templates/unity/micro/ai/AI.cs.j2).

Same shape as unity_spatial_facts.py: compute once, ride in `schema.meta`,
let a downstream consumer decide relevance. Runs immediately after
compute_layout() in unity_pipeline.py, so every entity's baked
`instance_positions` already exist — both the BLOCKING_KINDS obstacles this
module scans and the enemies it computes candidate points for.

Why this layer and not unity_reachability.py's existing machinery: that
module's flood_fill_reachable is only ever consumed by unity_playability.py
at Step 7 — the post-generation evaluator. Grading happens after the
generator already rendered AI.cs.j2, too late to feed its codegen. This
module is the Step-5-adjacent sibling that actually reaches the template,
reusing unity_reachability's BLOCKING_KINDS/_instance_block_radius footprint
math rather than re-deriving it.

Ground-only, matching unity_reachability's own content_reachable check:
cover behind an obstacle is a ground-combat concept. A flying/swimming/
climbing game's enemies have no ground-plane geometry to hide behind, so
this abstains (empty result) rather than fabricating cover in a volume.

`compute_cover_points` always writes schema.meta["cover_points"], even {} —
same non-fabrication discipline as unity_spatial_facts.attach_spatial_facts:
a model inventing its own meta.cover_points cannot smuggle one past this.
"""

from __future__ import annotations

import math

from .unity_playability import _spawn_point
from .unity_reachability import BLOCKING_KINDS, _instance_block_radius
from .unity_schema import UnitySchema
from .unity_spatial import GROUND_PLANE, ROUTE_CORRIDOR

META_KEY = "cover_points"

# How far from an enemy instance an obstacle can be and still count as usable
# cover — a wall on the other side of the arena is not something an enemy
# will run to mid-fight. Independent of the AI's own detection_radius: that
# schema (src/micro/ai.py) does not exist yet at this pipeline stage (it
# resolves later, during compose), and this is about nearby GEOMETRY, not
# about seeing the player.
_COVER_SEARCH_RADIUS = 15.0
# How far past the obstacle's own footprint the enemy actually stands —
# close enough to plausibly read as "behind it", far enough not to clip into
# the mesh.
_COVER_STANDOFF = 1.0


def compute_cover_points(schema: UnitySchema) -> list[str]:
    """Bake candidate cover points for every `kind == "enemy"` entity from
    BLOCKING_KINDS obstacle footprints already placed by compute_layout().
    Returns fixer-style messages (see compute_layout's own calling
    convention — `for msg in compute_cover_points(schema): self._fix(msg)`).
    """
    messages: list[str] = []
    cover_points: dict[str, list[dict]] = {}

    def _finish() -> list[str]:
        if isinstance(getattr(schema, "meta", None), dict):
            schema.meta[META_KEY] = cover_points
        return messages

    primitive = None
    if isinstance(getattr(schema, "meta", None), dict):
        primitive = schema.meta.get("spatial_primitive")
    if primitive not in (GROUND_PLANE, ROUTE_CORRIDOR):
        return _finish()

    enemies = [e for e in schema.entities
              if (e.kind or "").strip().lower() == "enemy" and (e.instance_positions or [])]
    obstacles = [e for e in schema.entities
                if (e.kind or "").strip().lower() in BLOCKING_KINDS and (e.instance_positions or [])]
    if not enemies or not obstacles:
        return _finish()

    sx, _sy, sz = _spawn_point(schema)

    for enemy in enemies:
        points: list[dict] = []
        for epos in enemy.instance_positions:
            ex = epos[0]
            ez = epos[2] if len(epos) > 2 else 0.0
            for obs in obstacles:
                br = _instance_block_radius(obs)
                for opos in obs.instance_positions:
                    ox = opos[0]
                    oy = opos[1] if len(opos) > 1 else 0.0
                    oz = opos[2] if len(opos) > 2 else 0.0
                    if math.hypot(ox - ex, oz - ez) > _COVER_SEARCH_RADIUS:
                        continue
                    # Stand on the side of the obstacle facing AWAY from
                    # spawn — the obstacle then sits between this point and
                    # the player's likely approach.
                    away_x, away_z = ox - sx, oz - sz
                    mag = math.hypot(away_x, away_z)
                    if mag < 1e-6:
                        away_x, away_z, mag = 1.0, 0.0, 1.0
                    away_x, away_z = away_x / mag, away_z / mag
                    stand = br + _COVER_STANDOFF
                    points.append({
                        "position": [ox + away_x * stand, oy, oz + away_z * stand],
                        # Facing back toward spawn — the peek-and-shoot direction.
                        "cover_direction": [-away_x, 0.0, -away_z],
                    })
        if points:
            cover_points[enemy.name] = points

    messages_out = _finish()
    if cover_points:
        total = sum(len(v) for v in cover_points.values())
        messages_out.append(
            f"cover_points: baked {total} candidate point(s) across "
            f"{len(cover_points)} enemy group(s) from {len(obstacles)} "
            f"obstacle instance(s)")
    return messages_out
