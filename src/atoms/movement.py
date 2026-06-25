"""Movement atoms — positional / rotational changes."""

from .base import AtomDef, AtomParam

MOVEMENT_ATOMS: list[AtomDef] = [
    AtomDef(
        name="MoveTo",
        category="movement",
        description="Move the entity to a world-space position at a given speed.",
        duration="until_complete",
        side_effects=["position"],
        aliases=["move_to", "walk_to", "run_to", "go_to"],
        params=[
            AtomParam(name="x",         type="float", default=0.0,  description="World X"),
            AtomParam(name="y",         type="float", default=0.0,  description="World Y"),
            AtomParam(name="z",         type="float", default=0.0,  description="World Z"),
            AtomParam(name="speed",     type="float", default=5.0,  description="Units per second"),
            AtomParam(name="tolerance", type="float", default=0.1,  description="Arrival distance"),
        ],
    ),
    AtomDef(
        name="MoveToEntity",
        category="movement",
        description="Move toward a named entity until within stop distance.",
        duration="until_complete",
        side_effects=["position"],
        aliases=["move_to_entity", "chase", "follow_entity", "approach"],
        params=[
            AtomParam(name="entity",    type="entity_ref", required=True, description="Target entity name"),
            AtomParam(name="speed",     type="float", default=5.0,  description="Units per second"),
            AtomParam(name="stop_dist", type="float", default=1.0,  description="Stop when this close"),
        ],
    ),
    AtomDef(
        name="RotateTo",
        category="movement",
        description="Rotate the entity to face a world Y-axis angle.",
        duration="until_complete",
        side_effects=["rotation"],
        aliases=["rotate_to", "face_direction", "turn_to"],
        params=[
            AtomParam(name="euler_y",   type="float", default=0.0,   description="Target Y rotation (degrees)"),
            AtomParam(name="speed",     type="float", default=90.0,  description="Degrees per second"),
        ],
    ),
    AtomDef(
        name="Teleport",
        category="movement",
        description="Instantly set the entity's world position (no interpolation).",
        duration="instant",
        side_effects=["position"],
        aliases=["teleport", "warp", "set_position", "snap_to"],
        params=[
            AtomParam(name="x", type="float", default=0.0),
            AtomParam(name="y", type="float", default=0.0),
            AtomParam(name="z", type="float", default=0.0),
        ],
    ),
    AtomDef(
        name="Follow",
        category="movement",
        description="Move toward a named entity each frame, maintaining a trailing distance.",
        duration="until_complete",
        side_effects=["position"],
        aliases=["follow", "trail", "escort"],
        params=[
            AtomParam(name="target",   type="entity_ref", required=True, description="Entity to follow"),
            AtomParam(name="distance", type="float", default=2.0,  description="Follow-behind distance"),
            AtomParam(name="speed",    type="float", default=4.0,  description="Units per second"),
        ],
    ),
]
