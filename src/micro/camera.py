"""Camera rig micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


CameraVariant = Literal[
    "follow", "follow_2d", "follow_2_5d", "orbit", "first_person",
    "climbing_follow",
    "cinemachine_3p", "cinemachine_topdown",
    "rts_pan_zoom", "vehicle_chase", "cinematic_dolly",
    # Frames a BOARD instead of following a body. Every variant above resolves
    # a follow target by tag and returns early when it finds none, so in a game
    # that declared it has no avatar (locomotion='none') they all sit at the
    # scene origin doing nothing. This one fits the content it can see and
    # holds it, which is the only camera behaviour a board needs.
    "board_framing",
]
VARIANTS = list(CameraVariant.__args__)   # type: ignore[attr-defined]

Vector3 = tuple[float, float, float]
Range2 = tuple[float, float]

CameraCategory = Literal[
    "player", "vehicle", "environment", "combat", "cinematic", "2d",
]
CameraAnchorType = Literal[
    "player_root", "player_eye", "vehicle_root", "spline", "room_node",
    "target_midpoint", "camera_focus", "board",
]
CameraActivationKind = Literal[
    "always", "game_type", "genre", "locomotion", "event",
]
CameraRotationMode = Literal[
    "look_at_anchor", "look_ahead", "fixed_angle", "orbit", "follow_anchor",
]
CameraRollBehavior = Literal["follow", "damp", "ignore"]
CameraHeightRules = Literal["anchor", "fixed", "dynamic", "terrain_aware"]
CameraResponseCurve = Literal["none", "speed", "zoom", "event"]
CameraSmoothing = Literal["none", "lerp", "spring", "critically_damped", "slerp"]
CameraCollisionMode = Literal["none", "push_in", "slide", "snap"]
CameraTargetType = Literal["anchor", "camera_focus", "midpoint", "spline", "reference"]
CameraBlendCurve = Literal["linear", "smoothstep", "ease_in", "ease_out", "ease_in_out"]
CameraStackingRules = Literal["priority_stack", "state_machine", "exclusive"]
CameraAxisLock = Literal["none", "x", "y", "xy"]


def _number(value: object, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return fallback


def _vector3(value: object, fallback: Vector3) -> Vector3:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return fallback
    try:
        values = tuple(_number(item, fallback[index]) for index, item in enumerate(value))
    except (IndexError, TypeError, ValueError):
        return fallback
    return values  # type: ignore[return-value]


def _range2(value: object, fallback: Range2) -> Range2:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return fallback
    try:
        values = tuple(_number(item, fallback[index]) for index, item in enumerate(value))
    except (IndexError, TypeError, ValueError):
        return fallback
    return (min(values), max(values))


class CameraActivationRule(BaseModel):
    kind:  CameraActivationKind = "always"
    value: str = ""


class CameraAnchorContract(BaseModel):
    type:      CameraAnchorType = "player_root"
    transform: str = "Player"
    offset:    Vector3 = (0.0, 0.0, 0.0)

    @field_validator("offset", mode="before")
    @classmethod
    def _normalize_offset(cls, value: object) -> Vector3:
        return _vector3(value, (0.0, 0.0, 0.0))


class CameraPositionContract(BaseModel):
    base:             Vector3 = (0.0, 3.0, -6.0)
    min_distance:     float = 0.0
    max_distance:     float = 0.0
    collision_radius: float = 0.25
    height_rules:     CameraHeightRules = "anchor"

    @field_validator("base", mode="before")
    @classmethod
    def _normalize_base(cls, value: object) -> Vector3:
        return _vector3(value, (0.0, 3.0, -6.0))

    @field_validator("min_distance", "max_distance", "collision_radius", mode="before")
    @classmethod
    def _nonnegative(cls, value: object) -> float:
        try:
            return max(0.0, _number(value, 0.0))
        except (TypeError, ValueError):
            return 0.0


class CameraRotationContract(BaseModel):
    mode:          CameraRotationMode = "look_at_anchor"
    pitch_clamp:   Range2 = (-89.0, 89.0)
    yaw_clamp:     Range2 = (-180.0, 180.0)
    roll_behavior: CameraRollBehavior = "ignore"

    @field_validator("pitch_clamp", mode="before")
    @classmethod
    def _normalize_pitch(cls, value: object) -> Range2:
        return _range2(value, (-89.0, 89.0))

    @field_validator("yaw_clamp", mode="before")
    @classmethod
    def _normalize_yaw(cls, value: object) -> Range2:
        return _range2(value, (-180.0, 180.0))


class CameraFovContract(BaseModel):
    base:           float = 60.0
    response_curve: CameraResponseCurve = "none"
    clamp:          Range2 = (40.0, 90.0)

    @field_validator("base", mode="before")
    @classmethod
    def _positive_base(cls, value: object) -> float:
        try:
            return max(1.0, _number(value, 60.0))
        except (TypeError, ValueError):
            return 60.0

    @field_validator("clamp", mode="before")
    @classmethod
    def _normalize_clamp(cls, value: object) -> Range2:
        return _range2(value, (40.0, 90.0))


class CameraSpatialContract(BaseModel):
    position: CameraPositionContract = Field(default_factory=CameraPositionContract)
    rotation: CameraRotationContract = Field(default_factory=CameraRotationContract)
    fov:      CameraFovContract = Field(default_factory=CameraFovContract)


class CameraLagSettings(BaseModel):
    follow: float = 0.0
    turn:   float = 0.0

    @field_validator("follow", "turn", mode="before")
    @classmethod
    def _nonnegative(cls, value: object) -> float:
        try:
            return max(0.0, _number(value, 0.0))
        except (TypeError, ValueError):
            return 0.0


class CameraSpeedResponse(BaseModel):
    fov_scale:      float = 0.0
    distance_scale: float = 0.0
    angle_scale:    float = 0.0


class CameraAccelerationResponse(BaseModel):
    shake: float = 0.0
    tilt:  float = 0.0


class CameraEnvironmentalResponse(BaseModel):
    drag:     float = 0.0
    inertia:  float = 0.0


class CameraCollisionContract(BaseModel):
    mode:          CameraCollisionMode = "none"
    probe_count:   int = 6
    recovery_speed: float = 5.0

    @field_validator("probe_count", mode="before")
    @classmethod
    def _positive_probe_count(cls, value: object) -> int:
        try:
            return max(1, int(_number(value, 6.0)))
        except (TypeError, ValueError):
            return 6

    @field_validator("recovery_speed", mode="before")
    @classmethod
    def _nonnegative_recovery(cls, value: object) -> float:
        try:
            return max(0.0, _number(value, 5.0))
        except (TypeError, ValueError):
            return 5.0


class CameraMotionContract(BaseModel):
    position_smoothing:    CameraSmoothing = "critically_damped"
    rotation_smoothing:    CameraSmoothing = "slerp"
    lag_settings:          CameraLagSettings = Field(default_factory=CameraLagSettings)
    speed_response:        CameraSpeedResponse = Field(default_factory=CameraSpeedResponse)
    acceleration_response: CameraAccelerationResponse = Field(
        default_factory=CameraAccelerationResponse)
    environmental_response: CameraEnvironmentalResponse = Field(
        default_factory=CameraEnvironmentalResponse)
    collision: CameraCollisionContract = Field(default_factory=CameraCollisionContract)


class CameraTargetRef(BaseModel):
    type: CameraTargetType = "anchor"
    ref:  str = "Player"


class CameraTargetBlendRules(BaseModel):
    weight:   float = 1.0
    priority: int = 0


class CameraLockOnRules(BaseModel):
    enabled:    bool = False
    orbit_angle: float = 0.0
    zoom:        float = 0.0


class CameraTargetingContract(BaseModel):
    primary_target:    CameraTargetRef = Field(default_factory=CameraTargetRef)
    secondary_targets: list[CameraTargetRef] = Field(default_factory=list)
    blend_rules:      CameraTargetBlendRules = Field(default_factory=CameraTargetBlendRules)
    lock_on_rules:    CameraLockOnRules = Field(default_factory=CameraLockOnRules)


class CameraCinematicContract(BaseModel):
    camera_path:       str = ""
    auto_pan_speed:    float = 0.0
    auto_pan_direction: Vector3 = (0.0, 0.0, 0.0)
    auto_zoom:         float = 0.0
    cut_rule:          Literal["snap", "blend", "fade"] = "blend"
    event_triggers:    list[str] = Field(default_factory=list)

    @field_validator("auto_pan_direction", mode="before")
    @classmethod
    def _normalize_pan_direction(cls, value: object) -> Vector3:
        return _vector3(value, (0.0, 0.0, 0.0))


class CameraParallaxLayer(BaseModel):
    layer:     str = ""
    multiplier: float = 1.0


class CameraTwoDContract(BaseModel):
    axis_lock:         CameraAxisLock = "xy"
    dead_zone:         tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    parallax_layers:   list[CameraParallaxLayer] = Field(default_factory=list)
    auto_scroll_speed: float = 0.0
    orthographic_size: float = 6.0
    depth:             float = -10.0
    zoom_min:          float = 2.0
    zoom_max:          float = 60.0

    @field_validator("dead_zone", mode="before")
    @classmethod
    def _normalize_dead_zone(cls, value: object) -> tuple[float, float, float, float]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return (0.0, 0.0, 0.0, 0.0)
        try:
            values = tuple(_number(item, 0.0) for item in value)
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0, 0.0)
        return values  # type: ignore[return-value]

    @field_validator("orthographic_size", "zoom_min", "zoom_max", mode="before")
    @classmethod
    def _positive_zoom(cls, value: object) -> float:
        try:
            return max(0.1, _number(value, 6.0))
        except (TypeError, ValueError):
            return 6.0


class CameraValidationContract(BaseModel):
    horizon_alignment_check:  bool = True
    forward_vector_check:     bool = True
    collision_envelope_check: bool = True
    distance_clamp_check:     bool = True
    rotation_clamp_check:     bool = True
    fov_clamp_check:          bool = True
    angle_tolerance_degrees:  float = 2.0
    distance_tolerance:       float = 0.05


class CameraBlendingContract(BaseModel):
    blend_in_time:  float = 0.3
    blend_out_time: float = 0.3
    blend_curve:    CameraBlendCurve = "smoothstep"
    interrupt_rules: list[str] = Field(default_factory=list)
    stacking_rules:  CameraStackingRules = "priority_stack"


class CameraDebugContract(BaseModel):
    name:      str = "Camera"
    color:     Vector3 = (1.0, 1.0, 1.0)
    gizmos:    bool = True
    telemetry: bool = True

    @field_validator("color", mode="before")
    @classmethod
    def _normalize_color(cls, value: object) -> Vector3:
        return _vector3(value, (1.0, 1.0, 1.0))


class CameraRigSchema(CodeExtensibleSchema):
    name:                str   = "CameraRig"
    variant:             str   = "follow"
    mode_id:             str   = ""
    category:            CameraCategory = "player"
    priority:            int   = 10
    activation_rules:    list[CameraActivationRule] = Field(default_factory=list)
    anchor:              CameraAnchorContract = Field(default_factory=CameraAnchorContract)
    spatial:             CameraSpatialContract = Field(default_factory=CameraSpatialContract)
    motion:              CameraMotionContract = Field(default_factory=CameraMotionContract)
    targeting:           CameraTargetingContract = Field(default_factory=CameraTargetingContract)
    cinematic:           CameraCinematicContract = Field(default_factory=CameraCinematicContract)
    two_d:               CameraTwoDContract = Field(default_factory=CameraTwoDContract)
    validation:          CameraValidationContract = Field(default_factory=CameraValidationContract)
    blending:            CameraBlendingContract = Field(default_factory=CameraBlendingContract)
    debug:               CameraDebugContract = Field(default_factory=CameraDebugContract)

    # Legacy flat fields remain accepted so old schema fixtures and existing
    # hand-authored micro calls continue to validate. default_fix() migrates
    # them into the typed contract before any template renders.
    follow_target_tag:   str   = "Player"
    offset_x:            float = 0.0
    offset_y:            float = 3.0
    offset_z:            float = -6.0
    smooth_time:         float = 0.15
    collision_avoidance: bool  = False
    lock_on:             bool  = False
    fov_base:            float = 60.0
    fov_sprint:          float = 70.0
    description:         str   = "Camera rig"
    meta:                dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "follow"

    @field_validator("mode_id", mode="before")
    @classmethod
    def _coerce_mode_id(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("priority", mode="before")
    @classmethod
    def _clamp_priority(cls, value: object) -> int:
        try:
            return max(0, int(_number(value, 10.0)))
        except (TypeError, ValueError):
            return 10


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity camera rig for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- The stable CameraModeID, category, priority and activation condition
- The anchor type/transform/offset and primary target
- Position/rotation/FOV rules, smoothing, collision and validation tolerances
- For 2D, the axis lock, dead zone and orthographic framing

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON matching this schema. Output JSON only.

VARIANT MUST BE: {variant}

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "mode_id": "ThirdPersonChase",
  "category": "player",
  "priority": 10,
  "activation_rules": [{{"kind": "always", "value": ""}}],
  "anchor": {{"type": "player_root", "transform": "Player", "offset": [0.0, 0.0, 0.0]}},
  "spatial": {{
    "position": {{"base": [0.0, 3.0, -6.0], "min_distance": 0.0, "max_distance": 0.0, "collision_radius": 0.25, "height_rules": "anchor"}},
    "rotation": {{"mode": "look_at_anchor", "pitch_clamp": [-89.0, 89.0], "yaw_clamp": [-180.0, 180.0], "roll_behavior": "ignore"}},
    "fov": {{"base": 60.0, "response_curve": "none", "clamp": [40.0, 90.0]}}
  }},
  "motion": {{
    "position_smoothing": "critically_damped",
    "rotation_smoothing": "slerp",
    "collision": {{"mode": "none", "probe_count": 6, "recovery_speed": 5.0}}
  }},
  "targeting": {{
    "primary_target": {{"type": "camera_focus", "ref": "CameraFocus"}},
    "secondary_targets": [],
    "blend_rules": {{"weight": 1.0, "priority": 0}},
    "lock_on_rules": {{"enabled": false, "orbit_angle": 0.0, "zoom": 0.0}}
  }},
  "two_d": {{"axis_lock": "xy", "dead_zone": [0.0, 0.0, 0.0, 0.0], "parallax_layers": [], "auto_scroll_speed": 0.0, "orthographic_size": 6.0, "depth": -10.0, "zoom_min": 2.0, "zoom_max": 60.0}},
  "validation": {{"horizon_alignment_check": true, "forward_vector_check": true, "collision_envelope_check": true, "distance_clamp_check": true, "rotation_clamp_check": true, "fov_clamp_check": true, "angle_tolerance_degrees": 2.0, "distance_tolerance": 0.05}},
  "blending": {{"blend_in_time": 0.3, "blend_out_time": 0.3, "blend_curve": "smoothstep", "interrupt_rules": [], "stacking_rules": "priority_stack"}},
  "debug": {{"name": "Camera", "color": [1.0, 1.0, 1.0], "gizmos": true, "telemetry": true}},
  "follow_target_tag": "Player",
  "offset_x": 0.0,
  "offset_y": 3.0,
  "offset_z": -6.0,
  "smooth_time": 0.15,
  "collision_avoidance": false,
  "lock_on": false,
  "fov_base": 60.0,
  "fov_sprint": 70.0,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class CameraRigPipeline(UnityMicroPipeline):
    kind            = "camera"
    display_name    = "Unity Camera Rig"
    schema_cls      = CameraRigSchema
    variants        = VARIANTS
    default_variant = "follow"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "camera"

    _RESERVED_FIELD_NAMES = {
        "followtag", "offset", "smoothtime", "occlusionmask", "lockon",
        "fovbase", "fovsprint", "_target", "_vel", "_cam",
    }
    _TEMPLATE_USINGS = {"UnityEngine"}

    _MODE_IDS = {
        "follow": "ThirdPersonChase",
        "follow_2d": "Follow2D",
        "follow_2_5d": "Follow2_5D",
        "orbit": "Orbit",
        "first_person": "FirstPerson",
        "climbing_follow": "ClimbingFollow",
        "cinemachine_3p": "ThirdPersonChase",
        "cinemachine_topdown": "TopDown",
        "rts_pan_zoom": "RTSPanZoom",
        "vehicle_chase": "VehicleChase",
        "cinematic_dolly": "CinematicDolly",
        "board_framing": "BoardFraming",
    }
    _MODE_CATEGORIES: dict[str, CameraCategory] = {
        "vehicle_chase": "vehicle",
        "cinematic_dolly": "cinematic",
        "board_framing": "2d",
        "follow_2d": "2d",
    }

    def _migrate_legacy_contract(self, schema, variant: str):
        """Populate Camera Contract v1 from the legacy flat camera fields.

        Older schema fixtures only know ``offset_*``, ``smooth_time`` and
        ``fov_*``. New fills can author the nested contract directly. The
        explicit-field check keeps the two forms backward compatible while
        making the nested contract authoritative whenever it is present.
        """
        fields = schema.model_fields_set
        if not schema.mode_id:
            schema.mode_id = self._MODE_IDS.get(variant, "Follow")
        if "category" not in fields:
            schema.category = self._MODE_CATEGORIES.get(variant, "player")

        if variant == "first_person" and "spatial" not in fields:
            schema.offset_x = schema.offset_y = schema.offset_z = 0.0

        if "anchor" not in fields:
            anchor_type: CameraAnchorType = (
                "player_eye" if variant == "first_person"
                else "vehicle_root" if variant == "vehicle_chase"
                else "board" if variant == "board_framing"
                else "player_root"
            )
            schema.anchor = CameraAnchorContract(
                type=anchor_type,
                transform="Player" if anchor_type != "board" else "Board",
            )

        if "spatial" not in fields:
            base = (schema.offset_x, schema.offset_y, schema.offset_z)
            if variant == "cinemachine_topdown" and base == (0.0, 3.0, -6.0):
                base = (0.0, 12.0, -4.0)
            if variant == "follow_2d":
                base = (schema.offset_x, schema.offset_y, 0.0)
            rotation_mode: CameraRotationMode = (
                "follow_anchor" if variant == "first_person"
                else "fixed_angle" if variant in ("follow_2d", "board_framing")
                else "look_at_anchor"
            )
            schema.spatial = CameraSpatialContract(
                position=CameraPositionContract(base=base),
                rotation=CameraRotationContract(mode=rotation_mode),
                fov=CameraFovContract(
                    base=schema.fov_base,
                    clamp=(40.0, max(90.0, schema.fov_sprint + 10.0)),
                ),
            )

        if "motion" not in fields:
            schema.motion = CameraMotionContract(
                position_smoothing="critically_damped",
                rotation_smoothing="slerp",
                collision=CameraCollisionContract(
                    mode="push_in" if schema.collision_avoidance else "none",
                ),
            )

        if "targeting" not in fields:
            primary = (
                CameraTargetRef(type="anchor", ref="Player/Eye")
                if variant == "first_person"
                else CameraTargetRef(type="anchor", ref="Board")
                if variant == "board_framing"
                else CameraTargetRef(type="camera_focus", ref="CameraFocus")
            )
            schema.targeting = CameraTargetingContract(
                primary_target=primary,
                lock_on_rules=CameraLockOnRules(enabled=schema.lock_on),
            )

        if "two_d" not in fields and variant in ("follow_2d", "board_framing"):
            schema.two_d = CameraTwoDContract(
                axis_lock="xy",
                orthographic_size=6.0,
                depth=-10.0,
            )

        # Keep the old flat members synchronized for legacy templates and tests.
        base = schema.spatial.position.base
        schema.offset_x, schema.offset_y, schema.offset_z = base
        schema.fov_base = schema.spatial.fov.base
        schema.collision_avoidance = schema.motion.collision.mode != "none"
        schema.lock_on = schema.targeting.lock_on_rules.enabled
        return schema

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "CameraRig")
        if schema.smooth_time <= 0:    schema.smooth_time = 0.15
        if schema.fov_base    <= 0:    schema.fov_base    = 60.0
        if schema.fov_sprint  < schema.fov_base:
            schema.fov_sprint = schema.fov_base
        # Racing keeps the existing camera schema but has a distinct visual
        # baseline: low enough to read the bike and curbing, and wide enough
        # to sell speed. Only replace the generic schema defaults; authored
        # values remain authoritative.
        if variant == "vehicle_chase":
            if (schema.offset_x, schema.offset_y, schema.offset_z) == (0.0, 3.0, -6.0):
                schema.offset_y = 1.35
                schema.offset_z = -5.25
            if (schema.fov_base, schema.fov_sprint) == (60.0, 70.0):
                schema.fov_base = 78.0
                schema.fov_sprint = 88.0
        # First-person rig is rooted on the head — zero out follow offsets
        if variant == "first_person":
            schema.offset_x = schema.offset_y = schema.offset_z = 0.0
        schema = self._migrate_legacy_contract(schema, variant)
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("CameraRig.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
