"""Character controller micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import MicroPipeline, to_pascal
from .code_fields import (
    CodeField, CodeMethod,
    lint_code_fragment, lint_method, lint_usings, lint_fields,
)


ControllerVariant = Literal[
    "first_person", "third_person", "top_down",
    "2d_platformer", "2d_topdown",
    "vehicle_car", "vehicle_bike",
    "flying", "swimming",
]

VARIANTS = list(ControllerVariant.__args__)   # type: ignore[attr-defined]

Ability = Literal["jump", "double_jump", "sprint", "crouch", "dash", "wall_jump", "glide", "fly"]
InputBackend = Literal["input_system", "legacy"]
CameraCoupling = Literal["none", "standard", "cinemachine"]
GroundCheck = Literal["raycast", "spherecast", "collider"]


class ControllerSchema(BaseModel):
    name:            str = "MovementController"
    variant:         str = "third_person"
    input_backend:   InputBackend  = "input_system"
    camera_coupling: CameraCoupling = "standard"
    abilities:       list[Ability] = Field(default_factory=lambda: ["jump", "sprint"])
    ground_check:    GroundCheck   = "raycast"
    move_speed:      float = 6.0
    sprint_speed:    float = 10.0
    jump_force:      float = 8.0
    description:     str = "Player movement controller"

    # ── Vehicle params (vehicle_* variants; ignored otherwise) ────────────────
    # Drive a real WheelCollider rig that the template self-constructs in Awake:
    # body box + 4 suspended wheels + headlights + brake light + drift smoke.
    max_motor_torque:      float = 1200.0
    max_steer_angle:       float = 28.0
    suspension_distance:   float = 0.25
    suspension_spring:     float = 35000.0
    suspension_damper:     float = 4500.0
    drift_slip_multiplier: float = 0.5    # sideways grip scale while handbrake held
    wheel_radius:          float = 0.35

    # ── Prompt-specific extras (the LLM authors these from the concept) ──
    # These are how prompt details actually reach the rendered .cs file. The
    # baseline controller above is the *skeleton*; these are the *behavior*.
    extra_usings:       list[str]        = Field(default_factory=list)
    extra_fields:       list[CodeField]  = Field(default_factory=list)
    awake_extra:        str              = ""
    update_extra:       str              = ""
    fixed_update_extra: str              = ""
    extra_methods:      list[CodeMethod] = Field(default_factory=list)

    meta:            dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "third_person"

    @field_validator("abilities", mode="before")
    @classmethod
    def _clamp_abilities(cls, v: list) -> list:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in v:
            s = str(item).strip().lower()
            if s in Ability.__args__ and s not in seen:   # type: ignore[attr-defined]
                seen.add(s)
                out.append(s)
        return out


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity character controller for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Movement model (rigidbody vs CharacterController vs custom)
- Input source (Input System actions or legacy axes)
- Camera coupling, ground detection, abilities
- One paragraph on feel: acceleration, friction, air control

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief below into this schema. Output ONLY JSON.

You are filling a schema that drives a Unity C# controller. The template
already emits the BASELINE movement for the chosen variant — you do NOT
re-write that. Instead, you author the PROMPT-SPECIFIC behavior using the
`extra_*` fields:

  extra_fields:       extra [SerializeField] declarations (oxygen, boost mult, …)
  awake_extra:        C# statements appended inside Awake()
  update_extra:       C# statements appended inside Update()
  fixed_update_extra: C# statements appended inside FixedUpdate()
  extra_methods:      helper methods appended to the class
  extra_usings:       extra namespaces (e.g. UnityEngine.UI)

C# RULES:
- Reference baseline fields directly: moveSpeed, sprintSpeed, jumpForce,
  _grounded (3D), _rb (2D/vehicle), _cc (3D person), _velocity (3D person).
- Use the Input System (Keyboard.current.leftShiftKey.isPressed style)
  unless input_backend is "legacy".
- No `using` lines inside method bodies — put them in extra_usings.
- No braces around method bodies in `extra_methods.body` — the template
  adds them.

VARIANT MUST BE: {variant}

ABILITIES — choose any subset of: jump, double_jump, sprint, crouch, dash, wall_jump, glide, fly
INPUT_BACKEND — input_system | legacy
CAMERA_COUPLING — none | standard | cinemachine
GROUND_CHECK — raycast | spherecast | collider
VEHICLE KEYS — only matter for vehicle_car / vehicle_bike (template builds a real
WheelCollider rig: suspension, handbrake drift, headlights H, brake light, smoke).
Tune from the brief: drift_slip_multiplier (lower = looser drift), suspension_spring/
damper (stiffness), max_motor_torque (power), max_steer_angle.

────────── FEW-SHOT EXAMPLE ──────────
If the brief is "underwater scuba explorer with shift-to-boost and an
oxygen meter that depletes over time", you produce:

{{
  "name": "ScubaExplorerController",
  "variant": "swimming",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "abilities": ["sprint"],
  "ground_check": "raycast",
  "move_speed": 4.0,
  "sprint_speed": 9.0,
  "jump_force": 0.0,
  "description": "First-person scuba controller with shift-to-boost and oxygen depletion.",
  "extra_usings": ["UnityEngine.Events"],
  "extra_fields": [
    {{"name": "maxOxygen",          "type": "float", "default": "100f",  "header": "Oxygen", "tooltip": "Oxygen capacity in seconds of breath."}},
    {{"name": "oxygenDepletionRate","type": "float", "default": "1.5f",  "tooltip": "Oxygen lost per second underwater."}},
    {{"name": "boostMultiplier",    "type": "float", "default": "2.2f",  "header": "Boost"}},
    {{"name": "boostCostPerSecond", "type": "float", "default": "4f",    "tooltip": "Extra oxygen drain while boosting."}},
    {{"name": "OnOxygenEmpty",      "type": "UnityEvent", "header": "Events"}}
  ],
  "awake_extra": "_oxygen = maxOxygen;",
  "update_extra": "bool boosting = Keyboard.current != null && Keyboard.current.leftShiftKey.isPressed && _oxygen > 0f;\\nif (boosting) {{ moveSpeed = sprintSpeed * boostMultiplier / 2f; }} else {{ moveSpeed = 4f; }}\\nDeplete(boosting ? boostCostPerSecond : oxygenDepletionRate);",
  "fixed_update_extra": "",
  "extra_methods": [
    {{
      "signature": "private void Deplete(float ratePerSecond)",
      "body": "_oxygen = Mathf.Max(0f, _oxygen - ratePerSecond * Time.deltaTime);\\nif (_oxygen <= 0f && OnOxygenEmpty != null) OnOxygenEmpty.Invoke();"
    }},
    {{
      "signature": "public void RefillOxygen(float amount)",
      "body": "_oxygen = Mathf.Min(maxOxygen, _oxygen + amount);"
    }}
  ]
}}

(That JSON declares the ONE private float `_oxygen` it uses via an extra_field
indirectly — actually no: declare it in extra_fields with type=float and
default=0f. The template makes every extra_field a [SerializeField] private
member, so they're all reachable from method bodies.)

────────── NOW DO IT FOR THE BRIEF BELOW ──────────

BRIEF:
{concept}

OUTPUT this JSON (keep the same key order; omit no keys):
{{
  "name": "PascalCase controller name (no spaces)",
  "variant": "{variant}",
  "input_backend": "...",
  "camera_coupling": "...",
  "abilities": ["..."],
  "ground_check": "...",
  "move_speed": 6.0,
  "sprint_speed": 10.0,
  "jump_force": 8.0,
  "max_motor_torque": 1200.0,
  "max_steer_angle": 28.0,
  "suspension_distance": 0.25,
  "suspension_spring": 35000.0,
  "suspension_damper": 4500.0,
  "drift_slip_multiplier": 0.5,
  "wheel_radius": 0.35,
  "description": "One sentence.",
  "extra_usings": [],
  "extra_fields": [],
  "awake_extra": "",
  "update_extra": "",
  "fixed_update_extra": "",
  "extra_methods": []
}}

Author REAL prompt-specific behavior in the extras. Output ONLY the JSON. No prose.
"""


class ControllerPipeline(MicroPipeline):
    kind           = "controller"
    display_name   = "Unity Character Controller"
    schema_cls     = ControllerSchema
    variants       = VARIANTS
    default_variant = "third_person"
    concept_prompt = CONCEPT_PROMPT
    schema_prompt  = SCHEMA_PROMPT
    template_subdir = "controller"

    # Names the template ALREADY emits — extra_fields with these names get
    # dropped to avoid double-declaration. Keep in sync with Controller.cs.j2.
    _RESERVED_FIELD_NAMES = {
        "movespeed", "sprintspeed", "jumpforce", "groundraylen", "groundmask",
        "_rb", "_cc", "_velocity", "_grounded", "_airjumpsleft", "maxairjumps",
        "flyspeed",
        # vehicle rig (vehicle_* variants)
        "maxmotortorque", "maxsteerangle", "suspensiondistance",
        "suspensionspring", "suspensiondamper", "driftslipmultiplier",
        "wheelradius", "handbraketorque", "_wheels", "_wheelvisuals",
        "_headlights", "_brakelight", "_smoke", "_basesidestiffness", "_lightson",
    }
    # Namespaces the template already emits.
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.InputSystem"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "MovementController")
        # 2D variants — strip 3D-only abilities
        if variant.startswith("2d_"):
            schema.abilities = [a for a in schema.abilities if a not in ("glide",)]
        # vehicle variants — abilities don't apply
        if variant.startswith("vehicle_"):
            schema.abilities = []
        # fly binds Shift (ascend); drop sprint so the key isn't double-bound.
        if "fly" in schema.abilities and "sprint" in schema.abilities:
            schema.abilities = [a for a in schema.abilities if a != "sprint"]
        if schema.move_speed <= 0:    schema.move_speed = 6.0
        if schema.sprint_speed <= schema.move_speed:
            schema.sprint_speed = schema.move_speed * 1.6
        if schema.jump_force < 0:     schema.jump_force = 0.0

        # Vehicle params — clamp to physically sane ranges so a hallucinated
        # value can't produce an undriveable or exploding car.
        schema.max_motor_torque      = max(100.0, min(8000.0,  schema.max_motor_torque))
        schema.max_steer_angle       = max(5.0,   min(50.0,    schema.max_steer_angle))
        schema.suspension_distance   = max(0.05,  min(0.6,     schema.suspension_distance))
        schema.suspension_spring     = max(5000.0, min(120000.0, schema.suspension_spring))
        schema.suspension_damper     = max(500.0, min(20000.0, schema.suspension_damper))
        schema.drift_slip_multiplier = max(0.1,   min(1.0,     schema.drift_slip_multiplier))
        schema.wheel_radius          = max(0.1,   min(1.5,     schema.wheel_radius))

        # ── Prompt-specific lint pass ──
        # LLMs author imperfect C#. Strip md fences, reject Python tokens,
        # auto-close braces, cap method length, dedupe usings/fields/methods.
        from .code_fields import _KNOWN_NAMESPACES
        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
            allowed=_KNOWN_NAMESPACES,
        )
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES
        )
        schema.awake_extra        = lint_code_fragment(schema.awake_extra,        what="Awake() body")
        schema.update_extra       = lint_code_fragment(schema.update_extra,       what="Update() body")
        schema.fixed_update_extra = lint_code_fragment(schema.fixed_update_extra, what="FixedUpdate() body")
        taken_methods: set[str] = set()
        cleaned_methods = []
        for m in schema.extra_methods:
            m2 = lint_method(m, taken_names=taken_methods)
            if m2 is not None:
                cleaned_methods.append(m2)
        schema.extra_methods = cleaned_methods

        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env  = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        cs_path = scripts_dir / f"{schema.name}.cs"
        cs_path.write_text(
            env.get_template("Controller.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )

        count = 1
        if schema.input_backend == "input_system":
            input_dir = out_dir / "Input"
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / f"{schema.name}.inputactions").write_text(
                env.get_template("InputActions.inputactions.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        # README
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
