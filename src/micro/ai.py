"""AI micro-generator — NavMesh patrol/chase, FSM, behaviour tree, flocking, etc."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import console, to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, csharp_ident, lint_extensible, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


AIVariant = Literal[
    "navmesh_patrol", "chase_player", "fsm_guard",
    "behaviour_tree_lite", "flocking", "cover_shooter", "flying_swarm",
    "waypoint_follow",
]
VARIANTS = list(AIVariant.__args__)   # type: ignore[attr-defined]

NavBackend = Literal["navmesh", "kinematic", "transform_step", "kinematic_2d"]


# ── FSM roles: what a state DOES, independent of what it is called ────────────
# AIState.name used to reach the emitted enum while AI.cs.j2's switch hardcoded
# Idle/Chase/Attack, so the moment the model named its own states the build broke
# with CS0117. Generating the switch from the states fixes the compile — but a
# switch over arbitrary names has no behaviour to put in the cases.
#
# `role` is the bridge. The model keeps its own vocabulary in the enum (Pursue,
# Sweep, Bail) while the generator emits real code from a fixed, engine-valid set
# of behaviours. A name it invents is variety; the role it maps to is what makes
# that variety compile and act.
AIRole = Literal["idle", "patrol", "alert", "chase", "attack", "flee", "dead", "hurt"]
_VALID_ROLES = frozenset(AIRole.__args__)   # type: ignore[attr-defined]

# Name -> role. Ordered longest-first within each role so "retreat" cannot be
# claimed by a shorter substring of another role's vocabulary.
_ROLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("patrol", "patrol"), ("wander", "patrol"), ("roam", "patrol"),
    ("sweep", "patrol"), ("sentry", "patrol"), ("guard", "patrol"),
    ("route", "patrol"), ("circuit", "patrol"), ("prowl", "patrol"),
    ("circle", "patrol"), ("orbit", "patrol"), ("drift", "patrol"),
    ("alert", "alert"), ("suspicious", "alert"), ("investigat", "alert"),
    ("search", "alert"), ("scan", "alert"), ("aware", "alert"),
    ("detect", "alert"), ("notice", "alert"), ("warn", "alert"),
    ("freeze", "alert"), ("listen", "alert"), ("watch", "alert"),
    ("observ", "alert"), ("ambush", "alert"), ("halt", "alert"),
    ("chase", "chase"), ("pursue", "chase"), ("pursuit", "chase"),
    ("hunt", "chase"), ("seek", "chase"), ("follow", "chase"),
    ("charge", "chase"), ("approach", "chase"), ("track", "chase"),
    ("intercept", "chase"), ("close", "chase"), ("engage", "chase"),
    ("stalk", "chase"), ("converge", "chase"),
    ("attack", "attack"), ("strike", "attack"), ("fire", "attack"),
    ("shoot", "attack"), ("melee", "attack"), ("assault", "attack"),
    ("slam", "attack"), ("bite", "attack"), ("combat", "attack"),
    ("lunge", "attack"), ("swoop", "attack"), ("ram", "attack"),
    ("flee", "flee"), ("retreat", "flee"), ("escape", "flee"),
    ("evade", "flee"), ("withdraw", "flee"), ("bail", "flee"),
    ("run", "flee"), ("scatter", "flee"), ("cover", "flee"),
    ("dead", "dead"), ("die", "dead"), ("death", "dead"),
    ("destroy", "dead"), ("disable", "dead"), ("expire", "dead"),
    ("stagger", "hurt"), ("flinch", "hurt"), ("stunned", "hurt"),
    ("stun", "hurt"), ("recoil", "hurt"), ("hurt", "hurt"), ("hit react", "hurt"),
    ("idle", "idle"), ("wait", "idle"), ("rest", "idle"),
    ("stand", "idle"), ("hold", "idle"), ("dormant", "idle"),
)

# Match keys that are stems rather than words — the state name uses the display
# form so the emitted enum never reads `Investigat`.
_KEYWORD_DISPLAY: dict[str, str] = {
    "investigat": "Investigate", "pursuit": "Pursue", "observ": "Observe",
    "suspicious": "Suspicious",
}

# Sensor predicates a transition may fire on. Deliberately a CLOSED enum of
# expressions the template knows how to emit, never raw C#: an authored
# condition string is exactly where a hallucinated identifier becomes CS0103,
# and the whole point of this change is that the model's invention cannot break
# the build. The `when` vocabulary is the safe surface it gets instead.
TransitionWhen = Literal[
    "target_detected", "target_lost", "in_attack_range",
    "out_of_attack_range", "alert_elapsed", "always", "took_damage",
    "heard_noise", "ally_alerted",
]
_VALID_WHEN = frozenset(TransitionWhen.__args__)   # type: ignore[attr-defined]

_WHEN_EXPR: dict[str, str] = {
    # HasPerceivedTarget() is the perception model (vision cone + raycast LOS,
    # hearing, and a short post-loss memory window) — see AI.cs.j2's
    # UpdatePerception()/CanSeeTarget(). Only rendered for navmesh_patrol/
    # fsm_guard/behaviour_tree_lite; those are the only variants whose
    # transitions can reference it (see AISchema's perception fields below).
    "target_detected":     "HasPerceivedTarget()",
    "target_lost":         "!HasPerceivedTarget()",
    "in_attack_range":     "_target != null && dist < attackRadius",
    "out_of_attack_range": "_target == null || dist > attackRadius",
    # "Time in the current state has elapsed" — generic despite the name (any
    # state can use it as a timeout exit, not only literal "alert" states; a
    # "hurt" stagger state reuses it the same way).
    "alert_elapsed":       "_stateTime >= alertSeconds",
    "always":              "true",
    # Edge-triggered: true for exactly the one Update() after OnHurt() fires
    # (Health.cs.j2's TakeDamage calls it via SendMessage), then AI.cs.j2's
    # Update() clears it right after CheckTransitions evaluates — see AI.cs.j2.
    "took_damage":         "_tookDamage",
    # Edge-triggered, recomputed fresh every UpdatePerception(): the target's
    # estimated speed crossed noiseSpeedThreshold within hearingRadius this
    # frame (sprinting footsteps, not a raycast/LOS signal — sound isn't
    # blocked by vision_obstruction in this model).
    "heard_noise":          "_heardNoise",
    # Edge-triggered by a squadmate's own AlertSquad() call (see
    # squad_alert_radius below) — reset the same way took_damage is.
    "ally_alerted":         "_allyAlerted",
}

def _state_ident(s: str, fallback: str = "State") -> str:
    """PascalCase C# identifier for a state name — see code_fields.csharp_ident."""
    return csharp_ident(s, fallback)


def infer_role(name: str, description: str = "") -> str:
    """Map a state name (then its description) onto a behaviour role.

    Deterministic and total — every name resolves. An unrecognised name becomes
    "idle" rather than being dropped: the state still appears in the enum and the
    switch, so the model's vocabulary survives even when its meaning does not.
    """
    for haystack in (str(name).lower(), str(description).lower()):
        for keyword, role in _ROLE_KEYWORDS:
            if keyword in haystack:
                return role
    return "idle"


class AIState(BaseModel):
    name:        str
    description: str = ""
    role:        str = ""    # "" → inferred from name/description by default_fix

    @field_validator("role", mode="before")
    @classmethod
    def _clamp_role(cls, v) -> str:
        s = str(v or "").strip().lower()
        return s if s in _VALID_ROLES else ""

    @property
    def ident(self) -> str:
        return _state_ident(self.name)


class AITransition(BaseModel):
    from_state: str
    to_state:   str
    when:       str = "always"

    @field_validator("when", mode="before")
    @classmethod
    def _clamp_when(cls, v) -> str:
        s = str(v or "").strip().lower()
        return s if s in _VALID_WHEN else "always"

    @property
    def condition(self) -> str:
        return _WHEN_EXPR[self.when]


class AISchema(CodeExtensibleSchema):
    name:               str  = "EnemyAI"
    variant:            str  = "navmesh_patrol"
    detection_radius:   float = 8.0
    attack_radius:      float = 1.5
    patrol_point_count: int   = 4
    move_speed:         float = 3.5

    # ── flying_swarm hover (airborne enemies) ─────────────────────────────────
    # Seeking the target's raw position parks a "floating" enemy at the
    # player's feet; these keep it in the air at head height and drifting.
    hover_height:       float = 4.0
    hover_spread:       float = 5.0   # vertical band the swarm occupies
    bob_amplitude:      float = 0.6
    bob_speed:          float = 1.2
    nav_backend:        NavBackend = "navmesh"
    states:             list[AIState] = Field(default_factory=list)
    transitions:        list[AITransition] = Field(default_factory=list)
    initial_state:      str   = ""
    alert_seconds:      float = 1.5   # how long an `alert` state holds before committing
    target_tag:         str   = "Player"
    use_health_hook:    bool  = True
    description:        str   = "Enemy AI"
    meta:               dict  = Field(default_factory=dict)

    # ── waypoint_follow (racing line / route runner) ──────────────────────────
    waypoint_parent_name: str   = "Waypoints"   # EntitySpawner emits this container
    slowdown_angle:       float = 35.0          # corners sharper than this slow it down
    rubber_band_strength: float = 0.5           # 0..1 — adapt speed to keep pace with target

    # ── Perception (navmesh_patrol / fsm_guard / behaviour_tree_lite only) ────
    # detection_radius above is the outer range; these narrow WHAT within that
    # range actually counts as "noticed" — see AI.cs.j2's CanSeeTarget()/
    # UpdatePerception(). chase_player/cover_shooter/flocking/flying_swarm/
    # waypoint_follow have no detection GATE today (they act unconditionally
    # once a target exists) so perception does not apply to them.
    vision_angle:          float = 120.0  # degrees, full cone width centred on facing; 360 = old omniscient sphere
    vision_obstruction:    bool  = True   # raycast-check line of sight; a wall between AI and target hides it regardless of angle/range
    eye_height:            float = 1.6    # world-up offset for both ends of the LOS raycast
    hearing_radius:        float = 6.0    # a target moving faster than noise_speed_threshold is heard even out of sight; 0 disables hearing
    noise_speed_threshold: float = 5.5    # m/s — sprinting, not walking; estimated from the target's own position delta, no source event needed
    memory_seconds:        float = 3.0    # keep heading for the last known position this long after losing perception before giving up; 0 = old instant give-up
    squad_alert_radius:    float = 10.0   # same-archetype allies within this radius are alerted the instant this AI perceives a target; 0 disables squad alerting

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

STATES — for the fsm_guard / behaviour_tree_lite variants these become a real C#
enum and a real switch, so name them in THIS game's own language ("Prowl",
"Spooked", "Lunge") rather than generic ones. Each needs a `role`, which is what
the generator emits code for:
  idle    — hold position and scan
  patrol  — walk the patrol points
  alert   — stop and face the target without closing
  chase   — path toward the target
  attack  — strike while in range
  flee    — move away from the target
  dead    — terminal; disables the behaviour
  hurt    — brief stagger/flinch after taking damage; holds position, then
            resumes chasing (needs a "took_damage" transition into it — see
            below — or omit it entirely if the brief has no hit-react beat)
A state whose role is omitted gets one inferred from its name.

TRANSITIONS — the graph between those states. `when` MUST be one of these exact
values; it is not free-form code:
  target_detected | target_lost | in_attack_range | out_of_attack_range
  alert_elapsed   | always      | took_damage     | heard_noise | ally_alerted
took_damage fires for one frame whenever this AI's Health component takes
damage (wired automatically, no field needed) — use it for transitions INTO a
"hurt" state. alert_elapsed also works as a generic state-timeout for hurt's
way back out (not just literal "alert" states). heard_noise fires when the
target is moving fast (sprinting) within hearing range but not yet seen — use
it for a distinct "something's out there" state (e.g. an "Investigate" state)
if the brief wants sound and sight to read differently; target_detected
already covers BOTH sight and sound if you don't need that distinction.
ally_alerted fires when a nearby same-archetype ally spots the target first.
Omit transitions entirely to get a default graph built from the roles.

PERCEPTION — for navmesh_patrol/fsm_guard/behaviour_tree_lite only (the other
variants have no detection gate to tune). Set these from whatever the brief
said about detection, above:
  vision_angle          degrees, cone width centred on facing (120 = alert
                         guard, 360 = omniscient/old behaviour)
  vision_obstruction     true unless the brief explicitly wants X-ray sight
  hearing_radius          0 disables hearing; a few meters past detection_radius
                         if the brief mentions noise/sound/footsteps
  memory_seconds          how long it keeps searching a lost target's last
                         known spot (0 = gives up instantly)
  squad_alert_radius      0 disables ally alerting; >0 for "one guard sees you,
                         the group reacts"

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
    {{ "name": "Idle",   "role": "idle",   "description": "Standing still, scanning for the player." }},
    {{ "name": "Chase",  "role": "chase",  "description": "Pathing toward the player at full speed." }},
    {{ "name": "Attack", "role": "attack", "description": "Within attack radius — strike then back off." }}
  ],
  "transitions": [
    {{ "from_state": "Idle",   "to_state": "Chase",  "when": "target_detected" }},
    {{ "from_state": "Chase",  "to_state": "Attack", "when": "in_attack_range" }},
    {{ "from_state": "Attack", "to_state": "Chase",  "when": "out_of_attack_range" }},
    {{ "from_state": "Chase",  "to_state": "Idle",   "when": "target_lost" }}
  ],
  "initial_state": "Idle",
  "alert_seconds": 1.5,
  "target_tag": "Player",
  "use_health_hook": true,
  "waypoint_parent_name": "Waypoints",
  "slowdown_angle": 35.0,
  "rubber_band_strength": 0.5,
  "vision_angle": 120.0,
  "vision_obstruction": true,
  "hearing_radius": 6.0,
  "memory_seconds": 3.0,
  "squad_alert_radius": 10.0,
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
        "_statetime", "alertseconds", "_tookdamage",
        # Perception (vision/hearing/memory/squad — see AISchema, AI.cs.j2)
        "visionangle", "visionobstruction", "eyeheight", "hearingradius",
        "noisespeedthreshold", "memoryseconds", "squadalertradius",
        "_targetvisible", "_heardnoise", "_wasperceiving", "_allyalerted",
        "_lastknownposition", "_searchtimer", "_prevtargetpos",
        "_haveprevtargetpos", "_squad",
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
        if variant in ("fsm_guard", "behaviour_tree_lite"):
            for note in self._lint_fsm(schema, variant):
                console.print(f"  [yellow]WARN[/]  {note}")
        schema.alert_seconds = max(0.0, min(30.0, schema.alert_seconds))

        # Perception — clamped regardless of variant (harmless on variants
        # that don't render it; keeps the schema itself always self-consistent).
        schema.vision_angle          = max(1.0, min(360.0, schema.vision_angle))
        schema.eye_height            = max(0.0, schema.eye_height)
        schema.hearing_radius        = max(0.0, schema.hearing_radius)
        schema.noise_speed_threshold = max(0.1, schema.noise_speed_threshold)
        schema.memory_seconds        = max(0.0, min(60.0, schema.memory_seconds))
        schema.squad_alert_radius    = max(0.0, schema.squad_alert_radius)
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    # ── FSM repair ────────────────────────────────────────────────────────────

    def _lint_fsm(self, schema, variant) -> list[str]:
        """Make `states` + `transitions` into a graph that provably compiles.

        Everything here is deterministic repair over content the model authored:
        names are sanitised rather than rejected, roles are inferred rather than
        demanded, and a dangling transition is dropped with its reason named.
        Never raises — a broken graph costs its broken edges, not the whole run.
        """
        notes: list[str] = []

        # 1. Sanitise + de-duplicate names. This is the CS0117 fix at its root:
        #    whatever the model called a state, the enum member and every switch
        #    label are rendered from the SAME sanitised identifier.
        seen: dict[str, str] = {}          # ident -> original name
        renames: dict[str, str] = {}       # original name -> final ident
        kept: list[AIState] = []
        for st in schema.states:
            ident = _state_ident(st.name)
            if ident == f"{schema.name}State":     # would shadow the enum type
                ident += "Mode"
            if ident in seen:
                notes.append(f"ai state {st.name!r} dropped: collides with "
                             f"{seen[ident]!r} after C# name sanitising")
                continue
            seen[ident] = st.name
            renames[str(st.name)] = ident
            if st.name != ident:
                notes.append(f"ai state {st.name!r} renamed to {ident} "
                             f"(valid C# enum member)")
            st.name = ident
            if not st.role:
                st.role = infer_role(ident, st.description)
            kept.append(st)
        if not kept:      # every name was unusable — fall back to the default set
            kept = [AIState(name="Idle",   description="Scan for the target.", role="idle"),
                    AIState(name="Chase",  description="Move toward the target.", role="chase"),
                    AIState(name="Attack", description="Strike the target.", role="attack")]
            notes.append("ai: no usable state names survived; default Idle/Chase/"
                         "Attack graph emitted instead")
        schema.states = kept
        names = {st.name for st in kept}

        # 2. This variant has no patrol helper (patrolPoints and PatrolStep are
        #    emitted only for navmesh_patrol/fsm_guard), so a patrol role here
        #    would call a method that does not exist — CS0103. Degrade to idle.
        if variant == "behaviour_tree_lite":
            for st in kept:
                if st.role == "patrol":
                    st.role = "idle"
                    notes.append(f"ai state {st.name}: patrol role degraded to idle "
                                 f"({variant} emits no patrol helper)")

        # 3. Drop transitions that reference a state this FSM does not have.
        def _resolve(ref: str) -> str | None:
            r = str(ref)
            if r in names:
                return r
            if r in renames and renames[r] in names:
                return renames[r]
            ident = _state_ident(r)
            return ident if ident in names else None

        valid: list[AITransition] = []
        for t in schema.transitions:
            src, dst = _resolve(t.from_state), _resolve(t.to_state)
            if src is None or dst is None:
                missing = t.from_state if src is None else t.to_state
                notes.append(f"ai transition {t.from_state}->{t.to_state} dropped: "
                             f"no state named {missing!r}")
                continue
            if src == dst:
                notes.append(f"ai transition {src}->{dst} dropped: self-loop")
                continue
            t.from_state, t.to_state = src, dst
            valid.append(t)
        schema.transitions = valid

        # 3b. "ally_alerted" only compiles when squad_alert_radius > 0 (that is
        #     what gates _allyAlerted/_squad/AlertSquad in AI.cs.j2) — an
        #     authored transition using it with squad disabled would be
        #     CS0103, not a soft failure. Drop it the same loud way a dangling
        #     endpoint is dropped above, rather than let it reach codegen.
        if schema.squad_alert_radius <= 0:
            kept_transitions, dropped = [], []
            for t in schema.transitions:
                if t.when == "ally_alerted":
                    dropped.append(f"{t.from_state}->{t.to_state}")
                else:
                    kept_transitions.append(t)
            if dropped:
                schema.transitions = kept_transitions
                notes.append("ai transition(s) " + ", ".join(dropped)
                             + " dropped: when=\"ally_alerted\" needs "
                               "squad_alert_radius > 0")

        # 4. No usable authored graph -> synthesise one from the roles. Built from
        #    the states that actually exist, so it is never the hardcoded triple.
        if not schema.transitions:
            schema.transitions = self._default_transitions(
                kept, squad_enabled=schema.squad_alert_radius > 0)

        # 5. initial_state must name a real state.
        init = _resolve(schema.initial_state) if schema.initial_state else None
        if init is None:
            home = next((st.name for st in kept if st.role in ("patrol", "idle")), None)
            init = home or kept[0].name
            if schema.initial_state:
                notes.append(f"ai initial_state {schema.initial_state!r} is not a "
                             f"state; using {init}")
        schema.initial_state = init

        # 6. Abstain loudly on states nothing can reach: they render (the model's
        #    vocabulary is preserved) but will never run, and that is worth saying.
        reachable = {schema.initial_state} | {t.to_state for t in schema.transitions}
        orphans = [st.name for st in kept if st.name not in reachable]
        if orphans:
            notes.append("ai: no transition reaches " + ", ".join(orphans)
                         + " — emitted but unreachable at runtime")
        return notes

    @staticmethod
    def _default_transitions(states: list[AIState], *,
                             squad_enabled: bool = False) -> list[AITransition]:
        """The role graph: home -> (alert) -> chase -> attack, and back out.

        Only edges whose endpoints both exist are emitted, so a two-state FSM
        gets a two-state graph rather than references to states it never had.

        `squad_enabled` must match schema.squad_alert_radius > 0 — an
        "ally_alerted" edge only compiles when AI.cs.j2 actually emits
        _allyAlerted/_squad/AlertSquad (see _lint_fsm's matching guard on the
        AUTHORED-transition path, which drops the same when-value the same way).
        """
        first: dict[str, str] = {}
        for st in states:
            first.setdefault(st.role, st.name)
        home   = first.get("patrol") or first.get("idle")
        alert  = first.get("alert")
        chase  = first.get("chase")
        attack = first.get("attack")
        flee   = first.get("flee")
        hurt   = first.get("hurt")

        out: list[AITransition] = []
        def add(a, b, when):
            if a and b:
                out.append(AITransition(from_state=a, to_state=b, when=when))

        # Outbound: home wakes on detection, through alert if one exists.
        # ally_alerted rides the same edges as target_detected — a squadmate's
        # own sighting/hearing wakes this AI exactly like its own senses would
        # (see AI.cs.j2's AlertSquad()/ReceiveAllyAlert(), squad_alert_radius).
        if alert:
            add(home,  alert, "target_detected")
            if squad_enabled:
                add(home, alert, "ally_alerted")
            add(alert, chase or attack, "alert_elapsed")
            add(alert, home,  "target_lost")
        else:
            add(home, chase or attack, "target_detected")
            if squad_enabled:
                add(home, chase or attack, "ally_alerted")
        add(chase,  attack, "in_attack_range")
        add(attack, chase or home, "out_of_attack_range")
        # Inbound: give up and go home.
        add(chase, home, "target_lost")
        # A flee state has no honest sensor in this micro (the AI tracks no health
        # of its own), so it is only ever reached by an authored transition.
        # Reachability is reported by the caller rather than faked here.
        if flee:
            add(flee, home, "target_lost")
        # A hurt/stagger state DOES have an honest sensor (Health.cs.j2's
        # TakeDamage calls OnHurt() via SendMessage — see AI.cs.j2) — any state
        # that can plausibly be hit interrupts into it, and it times out back to
        # chase (or home, if there's no chase state) via the same "elapsed"
        # timer alert uses.
        if hurt:
            for src in (home, alert, chase, attack):
                add(src, hurt, "took_damage")
            add(hurt, chase or home, "alert_elapsed")
        return out

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
