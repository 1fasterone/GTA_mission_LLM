"""AI micro-generator — NavMesh patrol/chase, FSM, behaviour tree, flocking, etc."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, UNITY_EXTRA_INSTRUCTIONS,
)


AIVariant = Literal[
    "navmesh_patrol", "chase_player", "fsm_guard",
    "behaviour_tree_lite", "flocking", "cover_shooter", "flying_swarm",
    "waypoint_follow",
]
VARIANTS = list(AIVariant.__args__)   # type: ignore[attr-defined]

NavBackend = Literal["navmesh", "kinematic", "transform_step", "kinematic_2d"]


class AIState(BaseModel):
    name:        str
    description: str = ""


class AISchema(CodeExtensibleSchema):
    name:               str  = "EnemyAI"
    variant:            str  = "navmesh_patrol"
    detection_radius:   float = 8.0
    attack_radius:      float = 1.5
    patrol_point_count: int   = 4
    move_speed:         float = 3.5
    nav_backend:        NavBackend = "navmesh"
    states:             list[AIState] = Field(default_factory=list)
    target_tag:         str   = "Player"
    use_health_hook:    bool  = True
    description:        str   = "Enemy AI"
    meta:               dict  = Field(default_factory=dict)

    # ── waypoint_follow (racing line / route runner) ──────────────────────────
    waypoint_parent_name: str   = "Waypoints"   # EntitySpawner emits this container
    slowdown_angle:       float = 35.0          # corners sharper than this slow it down
    rubber_band_strength: float = 0.5           # 0..1 — adapt speed to keep pace with target

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "navmesh_patrol"


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity AI behaviour for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Detection model (sight cones, hearing, distance)
- What happens after detection (chase, flee, take cover)
- Pathing approach (NavMesh, kinematic velocity, scripted patrol)
- Any unique states or transitions

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
NAV_BACKEND — navmesh | kinematic | transform_step

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "detection_radius": 8.0,
  "attack_radius": 1.5,
  "patrol_point_count": 4,
  "move_speed": 3.5,
  "nav_backend": "navmesh",
  "states": [
    {{ "name": "Idle",   "description": "Standing still, scanning for the player." }},
    {{ "name": "Chase",  "description": "Pathing toward the player at full speed." }},
    {{ "name": "Attack", "description": "Within attack radius — strike then back off." }}
  ],
  "target_tag": "Player",
  "use_health_hook": true,
  "waypoint_parent_name": "Waypoints",
  "slowdown_angle": 35.0,
  "rubber_band_strength": 0.5,
  "description": "One sentence."
}}

(waypoint_* keys only matter for the waypoint_follow variant — a route/racing-line
runner that follows children of the named container, slows for sharp corners, and
rubber-bands its speed to stay competitive with the target.)
""" + UNITY_EXTRA_INSTRUCTIONS


class AIPipeline(UnityMicroPipeline):
    kind            = "ai"
    display_name    = "Unity AI Behaviour"
    schema_cls      = AISchema
    variants        = VARIANTS
    default_variant = "navmesh_patrol"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "ai"

    _RESERVED_FIELD_NAMES = {
        "detectionradius", "attackradius", "targettag", "movespeed",
        "patrolpoints", "_patrolindex", "_agent", "_target", "_attacktimer",
        "_state", "neighbourradius", "separationweight", "alignmentweight",
        "cohesionweight", "_flock", "_velocity",
        "waypointparentname", "slowdownangle", "rubberbandstrength",
        "_waypoints", "_wpindex",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.AI", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "EnemyAI")
        if schema.detection_radius <= 0:   schema.detection_radius = 8.0
        if schema.attack_radius   <= 0:    schema.attack_radius   = 1.5
        if schema.patrol_point_count < 1:  schema.patrol_point_count = 1
        if schema.move_speed <= 0:         schema.move_speed = 3.5

        # Variant-specific nav backend. A composed 2D project (game_type threaded
        # in via schema.meta, same convention as trigger.py/interactable.py) has
        # no baked NavMesh — Unity's built-in NavMeshAgent is 3D-only — so these
        # variants fall back to direct transform-seeking in the XY plane instead.
        is_2d = str((schema.meta or {}).get("game_type", "")).lower() in ("2d", "2d_topdown")
        if variant in ("navmesh_patrol", "chase_player", "fsm_guard",
                       "behaviour_tree_lite", "cover_shooter"):
            schema.nav_backend = "kinematic_2d" if is_2d else "navmesh"
        if variant in ("flocking", "flying_swarm"):
            schema.nav_backend = "kinematic"
        if variant == "waypoint_follow":
            # Route runner steps transforms directly — works with no NavMesh bake.
            schema.nav_backend = "transform_step"
            if not schema.waypoint_parent_name.strip():
                schema.waypoint_parent_name = "Waypoints"
            schema.slowdown_angle       = max(5.0, min(90.0, schema.slowdown_angle))
            schema.rubber_band_strength = max(0.0, min(1.0,  schema.rubber_band_strength))

        # FSM/BT variants need at least three states
        if variant in ("fsm_guard", "behaviour_tree_lite") and not schema.states:
            schema.states = [
                AIState(name="Idle",   description="Stand still, scan for the target."),
                AIState(name="Chase",  description="Move toward the target."),
                AIState(name="Attack", description="Within attack range — strike."),
            ]
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("AI.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1

        if variant in ("fsm_guard", "behaviour_tree_lite"):
            (scripts_dir / f"{schema.name}State.cs").write_text(
                env.get_template("AIState.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
