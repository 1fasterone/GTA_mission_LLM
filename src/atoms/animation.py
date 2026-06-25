"""Animation atoms — single clip control."""

from .base import AtomDef, AtomParam

ANIMATION_ATOMS: list[AtomDef] = [
    AtomDef(
        name="PlayAnimation",
        category="animation",
        description="Play a named animation clip on the entity's Animator.",
        duration="duration_param",
        side_effects=["animator_state"],
        aliases=["play_anim", "play_animation", "trigger_anim", "animate"],
        params=[
            AtomParam(name="clip",   type="anim_clip", required=True, description="Animation clip name"),
            AtomParam(name="layer",  type="int",   default=0,    description="Animator layer index"),
            AtomParam(name="speed",  type="float", default=1.0,  description="Playback speed multiplier"),
        ],
    ),
    AtomDef(
        name="StopAnimation",
        category="animation",
        description="Stop the Animator or return it to the idle/default state.",
        duration="instant",
        side_effects=["animator_state"],
        aliases=["stop_anim", "stop_animation", "reset_anim", "idle"],
        params=[
            AtomParam(name="layer", type="int", default=0, description="Animator layer index"),
        ],
    ),
    AtomDef(
        name="BlendAnimation",
        category="animation",
        description="Cross-fade between two animation clips at a given blend weight.",
        duration="duration_param",
        side_effects=["animator_state"],
        aliases=["blend_anim", "crossfade", "lerp_animation"],
        params=[
            AtomParam(name="clip_a",    type="anim_clip", required=True,  description="Base clip name"),
            AtomParam(name="clip_b",    type="anim_clip", required=True,  description="Target clip name"),
            AtomParam(name="weight",    type="float", default=0.5,         description="Blend weight 0..1"),
            AtomParam(name="duration",  type="float", default=0.25,        description="Crossfade duration"),
        ],
    ),
]
