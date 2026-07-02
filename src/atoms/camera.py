"""Camera atoms — direct camera manipulation."""

from .base import AtomDef, AtomParam

CAMERA_ATOMS: list[AtomDef] = [
    AtomDef(
        name="CameraLookAt",
        category="camera",
        description="Point the main camera at a named entity.",
        duration="instant",
        side_effects=["camera_rotation"],
        aliases=["camera_look_at", "look_at", "aim_camera", "focus_camera"],
        params=[
            AtomParam(name="target", type="entity_ref", required=True, description="Entity to look at"),
        ],
    ),
    AtomDef(
        name="CameraShake",
        category="camera",
        description="Apply an impulse shake to the camera for a given intensity and duration.",
        duration="duration_param",
        side_effects=["camera_position"],
        aliases=["camera_shake", "shake_cam", "screen_shake", "impact_shake"],
        params=[
            AtomParam(name="intensity", type="float", default=1.0, description="Shake magnitude"),
            AtomParam(name="duration",  type="float", default=0.3, description="Shake duration seconds"),
        ],
    ),
    AtomDef(
        name="CameraOffset",
        category="camera",
        description="Translate the camera by a world-space offset vector.",
        duration="instant",
        side_effects=["camera_position"],
        aliases=["camera_offset", "move_camera", "nudge_camera"],
        params=[
            AtomParam(name="x", type="float", default=0.0),
            AtomParam(name="y", type="float", default=0.0),
            AtomParam(name="z", type="float", default=0.0),
        ],
    ),
    AtomDef(
        name="CameraFOV",
        category="camera",
        description="Set the main camera's field of view.",
        duration="instant",
        side_effects=["camera_fov"],
        aliases=["set_fov", "camera_fov", "zoom_camera", "field_of_view"],
        params=[
            AtomParam(name="fov",      type="float", default=60.0, description="Target FOV in degrees"),
            AtomParam(name="duration", type="float", default=0.0,  description="Lerp duration (0 = instant)"),
        ],
    ),
]
