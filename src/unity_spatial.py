"""
Spatial primitives — one per GAME SHAPE, selected deterministically (pipeline
Step 5, ahead of src/unity_layout.py's packing).

A spatial primitive answers "what shape is the space this game is played in?"
before anything answers "where does each group go in it". The compiler used to
have exactly one answer to both: pack circles on the ground plane and bake
`y = 0.0` for every instance (unity_layout._bake_positions). That is correct
for a game played ON a floor and silently wrong for one played IN a volume —
a skydiving brief compiled to an aircraft, five formation divers, the altitude
gates and the landing pad all spiral-packed side by side on the floor, because
the only spatial primitive in the compiler was a floor.

The primitives:

  ground_plane     the play space is a surface; content is packed around the
                   player's spawn. Walk/drive games. (Historic behavior — a
                   schema that selects this is laid out byte-identically to
                   before this module existed.)
  route_corridor   the play space is a ribbon; content lines the route it
                   follows. Selected by the presence of an authored path
                   route, which is a stronger statement about shape than
                   locomotion is.
  airspace_column  the play space is a vertical volume above a floor; content
                   is distributed across ALTITUDE BANDS and only has to avoid
                   its own band in plan view. Fly locomotion.
  depth_column     the same volume mirrored below a surface — sea level at
                   y=0, content descending. Swim locomotion.

Selection is a pure function of the schema's own declared shape (`game_type`,
`locomotion`, whether a route exists) — no prompt keywords, no LLM, no RNG.
Placement WITHIN a primitive stays in unity_layout.py; this module only decides
which primitive is in play and what altitude each group sits at.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .unity_schema import EntitySpec

# ── the primitives ────────────────────────────────────────────────────────────

GROUND_PLANE = "ground_plane"
ROUTE_CORRIDOR = "route_corridor"
AIRSPACE_COLUMN = "airspace_column"
DEPTH_COLUMN = "depth_column"
# Ascent along a face rather than movement through open volume — the play
# space is the SURFACE of a wall and the things attached to it.
WALL_SURFACE = "wall_surface"

# The two primitives whose content occupies a volume rather than a surface.
VOLUME_PRIMITIVES = frozenset({AIRSPACE_COLUMN, DEPTH_COLUMN})

# Which archetype (src/unity_layout_archetype.py, the layout pass's own
# selector) corresponds to which primitive in this vocabulary. The layout pass
# is authoritative — it reads far more evidence than locomotion alone — and
# stamps its choice on the schema; `select_primitive` below stays as the
# fallback for callers running before layout has happened.
_PRIMITIVE_BY_ARCHETYPE: dict[tuple[str, str], str] = {
    ("VerticalCorridor", "air"):   AIRSPACE_COLUMN,
    ("VerticalCorridor", "water"): DEPTH_COLUMN,
    ("BoundedVolume",    "air"):   AIRSPACE_COLUMN,
    ("BoundedVolume",    "water"): DEPTH_COLUMN,
}


def primitive_for_archetype(archetype: str, medium: str,
                            has_route: bool = False) -> str:
    """Translate a layout archetype into this module's vocabulary, so the
    playability evaluator and the layout pass describe the same scene."""
    if archetype == "VerticalSurface":
        return WALL_SURFACE
    hit = _PRIMITIVE_BY_ARCHETYPE.get((archetype, (medium or "").lower()))
    if hit:
        return hit
    return ROUTE_CORRIDOR if has_route else GROUND_PLANE

# Locomotion -> primitive, when no authored route overrides it. Locomotion is
# the schema's own statement about how the player moves through the space, and
# is already deterministically classified from the brief
# (src/unity_archetypes.py's _LOCOMOTION table).
_PRIMITIVE_BY_LOCOMOTION = {
    "fly":  AIRSPACE_COLUMN,
    "swim": DEPTH_COLUMN,
}

# ── vertical extent ───────────────────────────────────────────────────────────
# Ceiling of the playable volume, in metres above (airspace) or below (depth)
# the floor. Sized so a fly/swim controller crosses it in tens of seconds
# rather than instantly or never, and so the bands below stay far enough apart
# to read as separate altitudes.
_CEILING = {AIRSPACE_COLUMN: 240.0, DEPTH_COLUMN: 90.0}
# Lowest band an AIRBORNE group may occupy. Content below this is close enough
# to the floor that it reads as ground clutter rather than as something the
# player flies to, and would fight the terrain for space.
_FLOOR_CLEARANCE = {AIRSPACE_COLUMN: 25.0, DEPTH_COLUMN: 8.0}
# Two groups closer together than this in Y are not meaningfully in different
# bands — below it the band split is collapsed and the groups are packed apart
# in XZ instead (the ground-plane behavior), so a volume game with many groups
# degrades to a correct layout rather than an invisibly-stacked one.
_MIN_BAND_GAP = 12.0

# ── what stays on the floor ───────────────────────────────────────────────────
# Kinds that ARE the floor of the volume, or are anchored to it. These keep
# y=0 under every primitive: an airspace game still has terrain, a landing
# pad, and buildings, and lifting them to a band would leave the volume with
# no bottom. Deliberately a denylist of surface/structure kinds rather than an
# allowlist of airborne ones — an unrecognized kind is content, and content
# belongs in the volume the game is played in.
_GROUNDED_KINDS: frozenset[str] = frozenset({
    "terrain", "ground", "road", "track", "street", "path", "rail", "water",
    "building", "wall", "floor", "platform", "ramp", "tree", "rock", "decor",
    "crowd", "furniture", "scenery",
})

# Altitude TIER per kind, 0.0 (floor) .. 1.0 (ceiling), for groups whose
# `elevation` the schema did not author. Structural, not semantic: it says
# where a KIND of thing lives in a volume, the same way _KIND_DEFAULTS in
# unity_concept_entities.py says how many of it there are. Anything unlisted
# takes _DEFAULT_TIER.
#
# The ordering that matters: whatever carries the player sits above the things
# it drops/dives toward, and pickups sit between them so they're on the route
# down. An LLM-authored `elevation` overrides all of this.
_TIER_BY_KIND: dict[str, float] = {
    "npc":         1.00,   # the carrier — a jump plane, a mothership
    "vehicle":     0.95,
    "boss":        0.80,
    "player":      0.75,   # entered from the carrier, descends from here
    "enemy":       0.65,
    "creature":    0.65,
    "character":   0.60,
    "companion":   0.60,
    "collectible": 0.45,   # strung down the descent, not parked at one height
    "pickup":      0.45,
    "item":        0.45,
    "obstacle":    0.35,
    "hazard":      0.35,
    "fx":          0.55,
    "prop":        0.25,
}
_DEFAULT_TIER = 0.40


def select_primitive(game_type: str, locomotion: str,
                     has_route: bool = False) -> str:
    """The spatial primitive this schema's declared shape calls for.

    An authored route wins over locomotion: a path is an explicit statement
    about where the play space goes, whereas locomotion only says how the
    player moves along it (a flying racer still races down its course).
    """
    if has_route:
        return ROUTE_CORRIDOR
    return _PRIMITIVE_BY_LOCOMOTION.get((locomotion or "").lower(),
                                        GROUND_PLANE)


def is_grounded(entity: EntitySpec) -> bool:
    """True when this group is part of the floor of the volume and must stay
    at y=0 whatever primitive is in play."""
    return (entity.kind or "").strip().lower() in _GROUNDED_KINDS


def _tier(entity: EntitySpec) -> float:
    return _TIER_BY_KIND.get((entity.kind or "").strip().lower(), _DEFAULT_TIER)


def tier_of(entity: EntitySpec) -> float:
    """Where this KIND of thing lives in a vertical space, 0.0 (floor) ..
    1.0 (ceiling).

    Public because the layout primitives order their own bands by it: the
    carrier sits above what it drops toward, pickups sit on the route between
    them. Before this was shared, VerticalCorridor stratified by footprint
    SIZE, which is arbitrary with respect to altitude — it put a jump plane
    below its own jumper and kelp near the surface.
    """
    return _tier(entity)


def assign_elevations(entities: Iterable[EntitySpec],
                      primitive: str) -> dict[str, float]:
    """Map entity-group name -> world-space Y for its instances.

    A surface primitive (ground_plane / route_corridor) derives nothing: it
    returns only the elevations a schema AUTHORED explicitly, which is {} for
    every project that doesn't use the field — so a surface game is laid out
    exactly as it was before spatial primitives existed. Honouring an authored
    elevation here is what keeps a deliberate floating platform in a walking
    game from being silently flattened back onto the floor.

    For a volume primitive:
      * groups whose kind IS the floor (see _GROUNDED_KINDS) stay at 0.0;
      * a group with an authored non-zero `elevation` keeps it verbatim —
        that is the schema's intent channel and always wins;
      * everything else takes the altitude of its kind's tier, clamped into
        [floor_clearance, ceiling].

    depth_column mirrors the same tiers below y=0, so tier 1.0 is the surface
    and tier 0.0 the seabed — "high tier" always means "furthest from the
    thing the player ends up at".
    """
    if primitive not in VOLUME_PRIMITIVES:
        return {e.name: round(e.elevation, 3) for e in entities
                if abs(e.elevation) > 1e-6}

    ceiling = _CEILING[primitive]
    clearance = _FLOOR_CLEARANCE[primitive]
    sign = -1.0 if primitive == DEPTH_COLUMN else 1.0
    span = ceiling - clearance

    out: dict[str, float] = {}
    for e in entities:
        if is_grounded(e):
            out[e.name] = 0.0
            continue
        if abs(e.elevation) > 1e-6:
            out[e.name] = round(e.elevation, 3)
            continue
        out[e.name] = round(sign * (clearance + span * _tier(e)), 3)
    return out


def band_key(elevation: float) -> int:
    """Band index an elevation falls in. Two groups sharing a band must be
    packed apart in XZ; groups in different bands may share XZ because
    _MIN_BAND_GAP already separates them vertically.

    Quantizing rather than comparing pairwise keeps the packer's input a
    simple partition and makes the result independent of entity order.
    """
    return int(round(elevation / _MIN_BAND_GAP))


def describe(primitive: str, elevations: Optional[dict[str, float]] = None) -> str:
    """One-line fixer log message naming the primitive in play."""
    if primitive not in VOLUME_PRIMITIVES:
        return ""
    lifted = sum(1 for y in (elevations or {}).values() if abs(y) > 1e-6)
    axis = "altitude" if primitive == AIRSPACE_COLUMN else "depth"
    return (f"Spatial primitive '{primitive}': the play space is a volume, not "
            f"a floor — {lifted} entity group(s) placed at {axis} instead of "
            "packed onto the ground plane")
