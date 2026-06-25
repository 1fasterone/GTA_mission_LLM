"""Physics body atoms — direct manipulation of Rigidbody and Collider state.

These are the primitive opcodes for physics interaction:
force, impulse, gravity, and collision toggling.
All target the Rigidbody / Collider components on the entity's GameObject.
"""

from .base import AtomDef, AtomParam

PHYSICS_ATOMS: list[AtomDef] = [
    AtomDef(
        name="ApplyForce",
        category="physics",
        description="Add a continuous world-space force to the entity's Rigidbody (ForceMode.Force).",
        duration="instant",
        side_effects=["velocity", "acceleration"],
        aliases=["apply_force", "add_force", "push", "thrust"],
        engines=["unity"],
        params=[
            AtomParam(name="x", type="float", default=0.0, description="Force X component (world space)"),
            AtomParam(name="y", type="float", default=0.0, description="Force Y component (world space)"),
            AtomParam(name="z", type="float", default=0.0, description="Force Z component (world space)"),
        ],
    ),
    AtomDef(
        name="ApplyImpulse",
        category="physics",
        description="Add an instant impulse to the entity's Rigidbody (ForceMode.Impulse).",
        duration="instant",
        side_effects=["velocity"],
        aliases=["apply_impulse", "impulse", "kick", "knockback"],
        engines=["unity"],
        params=[
            AtomParam(name="x", type="float", default=0.0, description="Impulse X component (world space)"),
            AtomParam(name="y", type="float", default=5.0, description="Impulse Y component (world space)"),
            AtomParam(name="z", type="float", default=0.0, description="Impulse Z component (world space)"),
        ],
    ),
    AtomDef(
        name="SetGravity",
        category="physics",
        description="Enable or disable gravity on the entity's Rigidbody.",
        duration="instant",
        side_effects=["gravity"],
        aliases=["set_gravity", "gravity", "toggle_gravity", "zero_g"],
        engines=["unity"],
        params=[
            AtomParam(name="enabled", type="bool",  default=True, description="True = gravity on"),
            AtomParam(name="scale",   type="float", default=1.0,  description="Gravity scale multiplier (1 = normal; unused when disabled)"),
        ],
    ),
    AtomDef(
        name="ToggleCollision",
        category="physics",
        description="Enable or disable all Collider components on the entity.",
        duration="instant",
        side_effects=["collision"],
        aliases=["toggle_collision", "set_collision", "disable_collider", "enable_collider", "ghost"],
        engines=["unity"],
        params=[
            AtomParam(name="enabled", type="bool", default=True, description="True = collisions on"),
        ],
    ),
]
