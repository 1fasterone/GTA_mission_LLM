"""Character controller atoms — per-tick input/movement primitive opcodes.

These are NOT goal-directed atoms (MoveTo goes somewhere; Move applies velocity).
They are the raw opcodes a character controller executes each frame/step:
look, move, rotate, jump, sprint, crouch.
"""

from .base import AtomDef, AtomParam

CONTROLLER_ATOMS: list[AtomDef] = [
    AtomDef(
        name="Move",
        category="controller",
        description="Apply directional velocity to the entity for one tick.",
        duration="instant",
        side_effects=["position"],
        aliases=["move", "apply_velocity", "walk", "strafe"],
        engines=["unity"],
        params=[
            AtomParam(name="dir_x",  type="float", default=0.0,  description="Horizontal direction (-1 to 1)"),
            AtomParam(name="dir_z",  type="float", default=1.0,  description="Forward/back direction (-1 to 1)"),
            AtomParam(name="speed",  type="float", default=6.0,  description="Units per second"),
        ],
    ),
    AtomDef(
        name="Rotate",
        category="controller",
        description="Apply a yaw rotation delta to the entity this tick.",
        duration="instant",
        side_effects=["rotation"],
        aliases=["rotate", "turn", "yaw"],
        engines=["unity"],
        params=[
            AtomParam(name="euler_y_delta", type="float", default=0.0,  description="Degrees to rotate around Y axis"),
            AtomParam(name="speed",         type="float", default=1.0,  description="Rotation speed multiplier"),
        ],
    ),
    AtomDef(
        name="Jump",
        category="controller",
        description="Apply an upward impulse force to the entity's Rigidbody.",
        duration="instant",
        side_effects=["velocity"],
        aliases=["jump", "leap", "vault"],
        engines=["unity"],
        params=[
            AtomParam(name="force", type="float", default=8.0, description="Upward impulse magnitude"),
        ],
    ),
    AtomDef(
        name="Sprint",
        category="controller",
        description="Toggle sprint state, broadcasting a speed multiplier to the movement system.",
        duration="instant",
        side_effects=["speed"],
        aliases=["sprint", "run", "dash_speed"],
        engines=["unity"],
        params=[
            AtomParam(name="active",     type="bool",  default=True, description="True = sprinting"),
            AtomParam(name="multiplier", type="float", default=1.7,  description="Speed multiplier when active"),
        ],
    ),
    AtomDef(
        name="Crouch",
        category="controller",
        description="Toggle crouch state, scaling the CapsuleCollider height and visual scale.",
        duration="instant",
        side_effects=["scale", "collision_shape"],
        aliases=["crouch", "duck", "prone"],
        engines=["unity"],
        params=[
            AtomParam(name="active",       type="bool",  default=True, description="True = crouching"),
            AtomParam(name="height_scale", type="float", default=0.55, description="Collider + visual height scale when crouched"),
        ],
    ),
    AtomDef(
        name="Look",
        category="controller",
        description="Apply pitch and yaw look rotation from input deltas.",
        duration="instant",
        side_effects=["rotation"],
        aliases=["look", "aim", "look_input", "mouse_look"],
        engines=["unity"],
        params=[
            AtomParam(name="pitch",       type="float", default=0.0, description="Vertical look delta (degrees)"),
            AtomParam(name="yaw",         type="float", default=0.0, description="Horizontal look delta (degrees)"),
            AtomParam(name="sensitivity", type="float", default=2.0, description="Input sensitivity multiplier"),
        ],
    ),
]
