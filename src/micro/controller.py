"""Character controller micro-generator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeField, CodeMethod,
    lint_code_fragment, lint_method, lint_usings, lint_fields,
    apply_extensions_safe_block,
)


ControllerVariant = Literal[
    # 3D person
    "first_person", "third_person", "top_down",
    # 2D
    "2d_platformer", "2d_topdown",
    # 2D side-scrolling brawler — 2d_platformer's Rigidbody2D movement base
    # (side-scroll run + jump) plus the fighting ability set below, with
    # Physics2D hit-detection (see Controller.cs.j2's variant.startswith("2d_")
    # branches on every OverlapSphere call site).
    "2d_fighting",
    # 2.5D — real 3D CharacterController + models, lateral-plane-locked input
    "platformer_2_5d",
    # Vehicles
    "vehicle_car", "vehicle_bike",
    # Wheelless ground craft: raycast hover suspension, thrust-driven, no
    # WheelColliders — hovercars/speeders whose models have no wheel bones.
    "vehicle_hover",
    # Special locomotion
    "flying", "swimming",
    # New genres
    "aircraft",   # fixed-wing arcade: pitch/roll/yaw/throttle via Rigidbody
    "fighting",   # third_person base + melee combat atoms (punch/kick/block/dodge)
    # No avatar at all: the player acts on the game through the cursor
    # (locomotion='none'). Emitted from its own template — PointerController.cs.j2
    # — because it is not this file's subject with the movement removed. Every
    # variant above answers "how does the player's body move"; this one answers
    # "what is the pointer over, what did it pick up, where did that land".
    "pointer",
    # Control Surface Stack — one player, several runtime-switchable modes
    # (see `ControllerMode`/`ControllerSchema.modes` below). Emitted from its
    # own template, ControllerMultiMode.cs.j2 — see docs/CONTROL_SURFACE_STACK.md.
    "multi_mode",
]

VARIANTS = list(ControllerVariant.__args__)   # type: ignore[attr-defined]

# The vocabulary a `ControllerMode` inside a multi_mode controller can be.
# Deliberately narrower than ControllerVariant — excludes 2D variants (dimension
# mismatch with a runtime-switchable 3D stack), `pointer` (bodiless) and
# `multi_mode` itself. Grows in later phases (docs/CONTROL_SURFACE_STACK.md):
# Phase 1 shipped on_foot + vehicle_car. Phase 1b adds "flying" — NOT the
# single-variant "flying" CharacterController free-fly (ascend/descend on
# _cc), which has no room for the fixed-wing controls a drivable
# aircraft/VTOL entity actually needs. It is a Rigidbody pitch/roll/rudder/
# throttle rig ported from the single-variant "aircraft" section instead, so
# an aircraft-family entity in a multi_mode registry gets real flight
# behaviour (plus optional flaps/landing_gear abilities), not a hover drone.
# swimming/vehicle_bike/vehicle_hover/zero_g_eva/boating are still Phase
# 1b(remainder)/2, not yet wired into ControllerMultiMode.cs.j2.
# NOTE (Phase 1c, selected/not yet built): swimming, vehicle_bike, fighting
# are next up — each reuses an existing backing store (CC or WheelCollider),
# no new components. vehicle_hover is blocked on a pre-existing default_fix
# bug (missing hover_* clamp block); aircraft needs nothing new (already
# covered by "flying" above); 2D kinds and "pointer" are architectural
# non-fits, not backlog items. Do NOT add these to this Literal without also
# adding their _controller_modes.j2 macro + ControllerMultiMode.cs.j2
# has_<kind> gate — see docs/CONTROL_SURFACE_STACK.md "Next three".
ControllerModeKind = Literal["on_foot", "vehicle_car", "flying"]

_MODE_KINDS: list[str] = list(ControllerModeKind.__args__)  # type: ignore[attr-defined]

# Which modes physically CANNOT coexist in one registry — at most one mode per
# family, so `default_fix` can hard-error a duplicate-family registry instead
# of silently keeping only the first. Extend alongside ControllerModeKind.
_MODE_FAMILY: dict[str, str] = {
    "on_foot":    "on_foot",
    "vehicle_car": "vehicle",
    "flying":      "flying",
}

# Mode-switch key: whitelisted to names that mean the SAME physical key in both
# UnityEngine.KeyCode (legacy Input) and UnityEngine.InputSystem.Key (Input
# System) — the two enums disagree on several names (KeyCode.LeftControl vs.
# Key.LeftCtrl), so only names that are spelled identically in both are safe to
# emit into either backend's branch of ControllerMultiMode.cs.j2.
VALID_MODE_SWITCH_KEYS: frozenset[str] = frozenset({
    "Tab", "Q", "W", "E", "R", "F", "G", "H", "J", "K", "L", "M", "N",
    "V", "X", "Z", "C", "B", "Escape",
})

# Full atom vocabulary — maps to template sections.
# Locomotion extras beyond WASD:
#   ascend/descend = up/down in swim/fly   boost = burst speed
#   dash = short invincible dash            wall_jump/glide/fly = movement abilities
# Aircraft:  pitch / roll_plane / throttle
# Shooting:  shoot / aim_down_sights / reload / cycle_weapon / throw_grenade / melee_attack
# Fighting:  primary_attack / secondary_attack / block / dodge / grab
# Interaction: interact / use_item / open_inventory / open_map / pause
# Vehicle:   nitro / brake
# Flying (multi_mode "flying" kind only — see ControllerModeKind): flaps /
#   landing_gear. Rudder is NOT here — like vehicle_car's steering, it is a
#   base flight control every "flying" mode gets for free, not an optional
#   ability to gate a template section behind.
Ability = Literal[
    # locomotion
    "jump", "double_jump", "sprint", "crouch", "dash", "wall_jump", "glide", "fly",
    "ascend", "descend", "boost",
    # aircraft
    "pitch", "roll_plane", "throttle",
    # shooting
    "shoot", "aim_down_sights", "reload", "cycle_weapon", "throw_grenade", "melee_attack",
    # fighting / brawler
    "primary_attack", "secondary_attack", "block", "dodge", "grab", "throw",
    # interaction
    "interact", "use_item", "open_inventory", "open_map", "pause",
    # vehicle
    "nitro", "brake",
    # flying (multi_mode "flying" kind only)
    "flaps", "landing_gear",
    # pointer (the `pointer` variant) — the verbs of a game with no avatar.
    # "grab" above is a brawler's grapple, not picking a card up off a board.
    "drag",
]

InputBackend  = Literal["input_system", "legacy"]
CameraCoupling = Literal["none", "standard", "cinemachine"]
GroundCheck   = Literal["raycast", "spherecast", "collider"]

_ALL_ABILITIES: set[str] = set(Ability.__args__)  # type: ignore[attr-defined]


def _clamp_ability_list(v: object) -> list[str]:
    """Shared by ControllerSchema.abilities and ControllerMode.abilities —
    unknown/duplicate entries are dropped, never raised on."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in v:
        s = str(item).strip().lower()
        if s in _ALL_ABILITIES and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class WeaponDef(BaseModel):
    """One entry in a shoot-ability controller's `weapons` loadout (see
    ControllerSchema.weapons). An unauthored (or pre-loadout) schema gets a
    single entry synthesized in default_fix from the legacy flat fire_rate/
    magazine_size/reload_time/shoot_range fields below, damage=25.0 matching
    TryShoot()'s historical hardcoded value — so nothing regresses."""
    name:          str   = "Weapon"
    damage:        float = 25.0
    fire_rate:     float = 0.15
    magazine_size: int   = 30
    reload_time:   float = 2.0
    shoot_range:   float = 100.0


class ControllerMode(BaseModel):
    """One entry in a multi_mode controller's `modes` registry — see
    docs/CONTROL_SURFACE_STACK.md. `abilities` here are per-mode (e.g. `nitro`
    only means something while `kind == "vehicle_car"`), pruned the same way
    ControllerSchema.default_fix prunes the single-variant abilities list."""
    kind:      str       = "on_foot"
    label:     str       = ""
    abilities: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _clamp_kind(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in _MODE_KINDS else "on_foot"

    @field_validator("abilities", mode="before")
    @classmethod
    def _clamp_mode_abilities(cls, v: object) -> list:
        return _clamp_ability_list(v)


class ControllerSchema(BaseModel):
    name:            str           = "MovementController"
    variant:         str           = "third_person"
    input_backend:   InputBackend  = "input_system"
    camera_coupling: CameraCoupling = "standard"
    abilities:       list[str]     = Field(default_factory=lambda: ["jump", "sprint"])
    ground_check:    GroundCheck   = "raycast"
    description:     str           = "Player movement controller"

    # ── Core movement ──────────────────────────────────────────────────────────
    move_speed:   float = 6.0
    sprint_speed: float = 10.0
    jump_force:   float = 8.0

    # ── Pointer (pointer variant only) ────────────────────────────────────────
    # Hover feedback is on by default because it is the only thing that tells a
    # player a board is interactive at all: with no avatar there is no walking
    # up to something to discover it responds.
    hover_highlight: bool = True

    # ── Mouse look (first_person variant) ─────────────────────────────────────
    # Without these the first_person rig emitted movement but no rotation at
    # all: the player faced its spawn direction forever and the camera — which
    # copies the player transform — stared into empty space. Yaw turns the body
    # (so WASD follows the look direction); pitch tilts the eye transform only,
    # clamped so the view cannot roll over backwards.
    look_sensitivity: float = 2.0
    pitch_min:        float = -85.0
    pitch_max:        float = 85.0
    eye_height:       float = 1.6
    lock_cursor:      bool  = True

    # ── Aircraft params (aircraft variant only) ────────────────────────────────
    max_thrust:       float = 80.0
    lift_coefficient: float = 30.0
    pitch_speed:      float = 60.0
    roll_speed:       float = 90.0
    yaw_speed:        float = 30.0
    throttle_rate:    float = 0.5
    stall_speed:      float = 15.0
    max_air_speed:    float = 60.0

    # ── Vehicle params (vehicle_* variants) ────────────────────────────────────
    max_motor_torque:      float = 1200.0
    max_steer_angle:       float = 28.0
    suspension_distance:   float = 0.25
    suspension_spring:     float = 35000.0
    suspension_damper:     float = 4500.0
    drift_slip_multiplier: float = 0.5
    wheel_radius:          float = 0.35

    # ── Bike params (vehicle_bike variant only) ───────────────────────────────
    # Two WheelColliders give a rigidbody no inherent roll stability, so the
    # rig freezes physical roll/pitch (Controller.cs.j2 Awake()) and fakes the
    # bank into a turn purely visually — these two fields are that lean, not a
    # physics quantity.
    max_lean_angle: float = 35.0   # degrees, full lean at speed
    lean_speed:     float = 180.0  # deg/s the visual lean can rotate toward target

    # ── Hover params (vehicle_hover variant only) ─────────────────────────────
    # Ride height is held by a 4-point raycast spring-damper, so the craft
    # floats over the road surface instead of resting on wheel contacts.
    hover_ride_height:  float = 1.2
    hover_spring:       float = 220.0
    hover_damper:       float = 18.0
    hover_thrust:       float = 4200.0
    hover_reverse:      float = 1400.0
    hover_boost_mult:   float = 2.4
    hover_boost_time:   float = 1.8
    hover_boost_cooldown: float = 4.0
    hover_turn_torque:  float = 900.0
    hover_lateral_grip: float = 6.0     # sideways velocity bleed (lane-keeping)
    hover_drag:         float = 0.35
    hover_max_speed:    float = 140.0

    # ── Shooter params (shoot ability) ─────────────────────────────────────────
    # Legacy flat fields — still the tuning source default_fix synthesizes the
    # single fallback WeaponDef from when `weapons` is left empty.
    fire_rate:    float = 0.15
    magazine_size: int  = 30
    reload_time:  float = 2.0
    shoot_range:  float = 100.0
    # Per-weapon loadout (cycle_weapon switches _weaponIndex through this).
    weapons: list[WeaponDef] = Field(default_factory=list)
    # Reserve ammo a pickup (inventory "restore_ammo" effect) refills —
    # ReloadRoutine() draws from this instead of an infinite refill.
    starting_reserve_ammo: int = 90

    # ── Fighting params (primary_attack / secondary_attack abilities) ──────────
    primary_damage:   float = 15.0
    primary_range:    float = 1.2
    primary_cooldown: float = 0.35
    secondary_damage:   float = 30.0
    secondary_range:    float = 1.6
    secondary_cooldown: float = 0.7
    block_reduction:  float = 0.7   # fraction of damage absorbed while blocking
    dodge_force:      float = 12.0
    dodge_cooldown:   float = 1.0
    dodge_i_frames:   float = 0.15  # invincibility window in seconds
    throw_damage:     float = 20.0
    throw_range:      float = 1.8   # pickup-detection radius for a nearby "Throwable"
    throw_force:      float = 12.0
    throw_cooldown:   float = 0.6

    # ── Interaction params (interact ability) ──────────────────────────────────
    interact_range: float = 2.5

    # ── Control Surface Stack (multi_mode variant only) ────────────────────────
    # A runtime-switchable registry of modes riding ONE player GameObject — see
    # docs/CONTROL_SURFACE_STACK.md. Every other field above (move_speed,
    # max_motor_torque, wheel_radius, ...) is still the tuning for whichever
    # mode uses it; `modes` only says which mode families are present and which
    # abilities apply to each.
    modes:           list[ControllerMode] = Field(default_factory=list)
    default_mode:    str                  = ""      # falls back to modes[0].kind
    mode_switch_key: str                  = "Tab"    # see VALID_MODE_SWITCH_KEYS

    # ── Prompt-specific extras (LLM authors from concept) ─────────────────────
    extra_usings:       list[str]        = Field(default_factory=list)
    extra_fields:       list[CodeField]  = Field(default_factory=list)
    awake_extra:        str              = ""
    update_extra:       str              = ""
    fixed_update_extra: str              = ""
    extra_methods:      list[CodeMethod] = Field(default_factory=list)

    # ── Schema Extensions (LLM field invention sandbox) ───────────────────────
    # See docs/SCHEMA_EXTENSIONS.md / CLAUDE.md "Schema Extensions" — the ONLY
    # place the LLM may invent a field outside the fixed surface above.
    # Sandboxed by apply_extensions_safe_block, not lint_extensible/lint_fields:
    # no pruning, no clamping, no renaming — a name collision is a loud abstain.
    extensions_fields:   list[CodeField]  = Field(default_factory=list)
    extensions_methods:  list[CodeMethod] = Field(default_factory=list)
    extensions_metadata: dict             = Field(default_factory=dict)

    meta: dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "third_person"

    @field_validator("abilities", mode="before")
    @classmethod
    def _clamp_abilities(cls, v: object) -> list:
        return _clamp_ability_list(v)


# ── Prompts ───────────────────────────────────────────────────────────────────

CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity character controller for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1–2 paragraphs:
- Movement model and variant (see VARIANT list below)
- Input source (Input System or legacy)
- Camera coupling, ground detection
- Which control atoms are needed (see ABILITIES list below)
- One paragraph on feel: acceleration, friction, response

VARIANT OPTIONS:
  first_person, third_person, top_down       — CharacterController-based 3D
  2d_platformer, 2d_topdown                  — Rigidbody2D
  platformer_2_5d                             — CharacterController, real 3D models/world,
                                                movement locked to a lateral (side-scroll) plane
  vehicle_car, vehicle_bike                   — WheelCollider rig
  flying, swimming                            — free 3D locomotion
  aircraft                                    — Rigidbody pitch/roll/yaw/throttle
  fighting                                    — third_person movement + melee atoms (3D)
  2d_fighting                                 — 2D side-scrolling brawler: 2d_platformer's
                                                Rigidbody2D run+jump plus the fighting atoms
                                                below (Cuphead/beat-em-up style — pick this,
                                                not "fighting", for a 2D brawler)
  multi_mode                                  — ONE player switching at runtime between
                                                several of the modes above (e.g. on-foot +
                                                a drivable car); only pick this when the
                                                brief explicitly asks to get in/out of a
                                                vehicle or switch locomotion mid-game

ABILITIES — choose any that match the game concept:
  LOCOMOTION: jump, double_jump, sprint, crouch, dash, wall_jump, glide, fly, ascend, descend, boost
  AIRCRAFT:   pitch, roll_plane, throttle
  SHOOTING:   shoot, aim_down_sights, reload, cycle_weapon, throw_grenade, melee_attack
  FIGHTING:   primary_attack, secondary_attack, block, dodge, grab, throw
    throw: pick up a nearby object tagged "Throwable" and launch it as a weapon
    (crate/bottle/barrel-style thrown props) — works for both "fighting" and
    "2d_fighting" variants.
  INTERACTION: interact, use_item, open_inventory, open_map, pause
  VEHICLE:    nitro, brake

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief below into this schema. Output ONLY valid JSON.

VARIANT MUST BE: {variant}

SCHEMA FIELD GUIDE:
  abilities — list of ability names from the full vocabulary above; drives which C# sections appear
  move_speed / sprint_speed / jump_force — tune to match the brief's feel
  fire_rate (s) / magazine_size / reload_time (s) / shoot_range — only matter when "shoot" in abilities
  weapons — optional list of {"name", "damage", "fire_rate", "magazine_size", "reload_time", "shoot_range"};
    leave empty for a single weapon (it synthesizes one from the flat fields above). Author 2+ entries
    only when "cycle_weapon" is in abilities and the brief names distinct weapons (e.g. pistol + shotgun).
  starting_reserve_ammo — only matters when "shoot" in abilities; ammo a reload draws from once the
    magazine is empty (an inventory's "restore_ammo" item effect refills this, not the magazine directly)
  primary_damage / primary_range / primary_cooldown — "primary_attack" (punch / light attack)
  secondary_damage / secondary_range / secondary_cooldown — "secondary_attack" (kick / heavy)
  block_reduction (0–1 fraction) — "block" ability
  dodge_force / dodge_cooldown / dodge_i_frames — "dodge" ability
  throw_damage / throw_range / throw_force / throw_cooldown — "throw" ability
    (throw_range is the pickup-detection radius for a nearby Throwable-tagged object)
  interact_range — "interact" ability sphere radius
  Aircraft keys (aircraft variant only): max_thrust / lift_coefficient / pitch_speed /
    roll_speed / yaw_speed / throttle_rate / stall_speed / max_air_speed
  Vehicle keys (vehicle_car / vehicle_bike only): max_motor_torque / max_steer_angle /
    suspension_distance / suspension_spring / suspension_damper / drift_slip_multiplier / wheel_radius
  Bike keys (vehicle_bike only): max_lean_angle (degrees, how far it banks into a turn) /
    lean_speed (deg/s the bank angle catches up to the target)
  Multi-mode keys (multi_mode variant ONLY):
    modes — list of {"kind": "on_foot"|"vehicle_car"|"flying", "label": str, "abilities": [...]},
      one entry per mode the player can switch into. Must have at least 2 entries with
      DIFFERENT kinds (a one-entry or duplicate-kind list is not a stack).
      on_foot abilities: jump, sprint only. vehicle_car abilities: nitro only.
      flying abilities: flaps, landing_gear only (pitch/roll/rudder/throttle are
      always on for a flying entry — no ability needed for those).
      (other ability words are pruned — this is the currently supported set, see
      docs/CONTROL_SURFACE_STACK.md)
      A "flying" entry reuses max_thrust/lift_coefficient/pitch_speed/roll_speed/
      yaw_speed/throttle_rate/stall_speed/max_air_speed (same fields the aircraft
      variant tunes) — set those to fit the flying entry's craft.
    default_mode — which entry's "kind" the player starts in
    mode_switch_key — one of: Tab, Q, W, E, R, F, G, H, J, K, L, M, N, V, X, Z, C, B, Escape
    (move_speed/sprint_speed/jump_force tune the on_foot entry; max_motor_torque/
    max_steer_angle/etc. tune the vehicle_car entry — same fields every other variant uses)

extra_* fields (LLM-authored prompt-specific extras):
  extra_fields       — extra [SerializeField] declarations (oxygen meter, grip level, …)
  awake_extra        — C# appended inside Awake()
  update_extra       — C# appended inside Update()
  fixed_update_extra — C# appended inside FixedUpdate()
  extra_methods      — helper methods appended at end of class
  extra_usings       — extra namespaces

extensions (only if extra_* above genuinely isn't enough — most prompts need
none of this; leave all three empty):
  extensions_fields   — new [SerializeField] declarations, same shape as extra_fields
  extensions_methods  — new helper methods, same shape as extra_methods
  extensions_metadata — flat {"key": "value"} of NON-CODE notes, never C#
  If you need new fields, put them inside extensions. It is sandboxed, not
  lint-hardened the way extra_* is — a name colliding with another field
  anywhere in the schema is dropped rather than repaired.

C# RULES for extra_* bodies:
  Reference baseline fields directly: moveSpeed, sprintSpeed, jumpForce, _grounded,
  _rb (2D/vehicle/aircraft), _cc (3D person), _velocity (3D person).
  For shooting: _currentAmmo, _isReloading, _lastFireTime, _isAiming, _weaponIndex, _grenadesLeft.
  For fighting: _isBlocking, _isDodging, _isInvincible, _primaryCooldown, _secondaryCooldown.
  No `using` inside method bodies — put them in extra_usings.
  No braces around method bodies in extra_methods.body — template adds them.
  Unity 6 with Cinemachine 3 uses `CinemachineCamera` from the
  `Unity.Cinemachine` namespace. Do not emit the obsolete
  `CinemachineVirtualCamera` type; if a camera field is needed, declare
  `CinemachineCamera` and put `Unity.Cinemachine` in extra_usings.

────────── FEW-SHOT: underwater scuba explorer ──────────
{{
  "name": "ScubaExplorerController",
  "variant": "swimming",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "abilities": ["sprint", "ascend", "descend", "interact"],
  "ground_check": "raycast",
  "move_speed": 4.0,
  "sprint_speed": 9.0,
  "jump_force": 0.0,
  "interact_range": 2.0,
  "description": "Underwater scuba controller with boost and oxygen depletion.",
  "extra_fields": [
    {{"name": "maxOxygen",            "type": "float",                "default": "100f", "header": "Oxygen"}},
    {{"name": "oxygenDepletionRate",  "type": "float",                "default": "1.5f"}},
    {{"name": "OnOxygenEmpty",        "type": "UnityEngine.Events.UnityEvent", "header": "Events"}}
  ],
  "awake_extra": "_oxygen = maxOxygen;",
  "update_extra": "float _rate = _sprintHeld ? oxygenDepletionRate * 2.5f : oxygenDepletionRate;\\n_oxygen = Mathf.Max(0f, _oxygen - _rate * Time.deltaTime);\\nif (_oxygen <= 0f) OnOxygenEmpty?.Invoke();",
  "extra_fields": [{{"name": "_oxygen", "type": "float", "default": "0f"}}],
  "extra_usings": [],
  "extra_methods": [],
  "fixed_update_extra": ""
}}

────────── FEW-SHOT: 3D fighting brawler ──────────
{{
  "name": "BrawlerController",
  "variant": "fighting",
  "abilities": ["jump", "sprint", "primary_attack", "secondary_attack", "block", "dodge", "grab"],
  "move_speed": 5.0,
  "sprint_speed": 8.5,
  "jump_force": 7.0,
  "primary_damage": 18.0,
  "primary_range": 1.3,
  "primary_cooldown": 0.3,
  "secondary_damage": 35.0,
  "secondary_range": 1.8,
  "secondary_cooldown": 0.65,
  "block_reduction": 0.75,
  "dodge_force": 14.0,
  "dodge_cooldown": 0.9,
  "dodge_i_frames": 0.15,
  "description": "Street brawler with full melee combo system.",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "ground_check": "spherecast",
  "extra_usings": [], "extra_fields": [], "awake_extra": "", "update_extra": "",
  "fixed_update_extra": "", "extra_methods": []
}}

────────── FEW-SHOT: 2D side-scrolling brawler (Cuphead-style) ──────────
{{
  "name": "PlayerBrawlerController",
  "variant": "2d_fighting",
  "abilities": ["jump", "primary_attack", "secondary_attack", "throw"],
  "move_speed": 5.5,
  "sprint_speed": 5.5,
  "jump_force": 9.0,
  "primary_damage": 12.0,
  "primary_range": 1.0,
  "primary_cooldown": 0.25,
  "secondary_damage": 22.0,
  "secondary_range": 1.3,
  "secondary_cooldown": 0.55,
  "throw_damage": 20.0,
  "throw_range": 1.8,
  "throw_force": 12.0,
  "throw_cooldown": 0.6,
  "description": "2D side-scrolling brawler: light/heavy punch plus throwing crates and bottles at enemies.",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "ground_check": "raycast",
  "extra_usings": [], "extra_fields": [], "awake_extra": "", "update_extra": "",
  "fixed_update_extra": "", "extra_methods": []
}}

────────── FEW-SHOT: on-foot exploration with a drivable car ──────────
{{
  "name": "PlayerController",
  "variant": "multi_mode",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "abilities": [],
  "ground_check": "raycast",
  "move_speed": 6.0,
  "sprint_speed": 10.0,
  "jump_force": 8.0,
  "max_motor_torque": 1400.0,
  "max_steer_angle": 30.0,
  "suspension_distance": 0.25,
  "suspension_spring": 35000.0,
  "suspension_damper": 4500.0,
  "drift_slip_multiplier": 0.5,
  "wheel_radius": 0.35,
  "description": "GTA-style player that walks on foot and can drive a car.",
  "modes": [
    {{"kind": "on_foot",    "label": "On Foot", "abilities": ["jump", "sprint"]}},
    {{"kind": "vehicle_car", "label": "Driving", "abilities": ["nitro"]}}
  ],
  "default_mode": "on_foot",
  "mode_switch_key": "Tab",
  "extra_usings": [], "extra_fields": [], "awake_extra": "", "update_extra": "",
  "fixed_update_extra": "", "extra_methods": []
}}

────────── NOW DO IT FOR THE BRIEF BELOW ──────────

BRIEF:
{concept}

OUTPUT this JSON (omit no keys):
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "input_backend": "input_system",
  "camera_coupling": "standard",
  "abilities": [],
  "ground_check": "raycast",
  "description": "One sentence.",
  "move_speed": 6.0,
  "sprint_speed": 10.0,
  "jump_force": 8.0,
  "max_thrust": 80.0,
  "lift_coefficient": 30.0,
  "pitch_speed": 60.0,
  "roll_speed": 90.0,
  "yaw_speed": 30.0,
  "throttle_rate": 0.5,
  "stall_speed": 15.0,
  "max_air_speed": 60.0,
  "max_motor_torque": 1200.0,
  "max_steer_angle": 28.0,
  "suspension_distance": 0.25,
  "suspension_spring": 35000.0,
  "suspension_damper": 4500.0,
  "drift_slip_multiplier": 0.5,
  "wheel_radius": 0.35,
  "max_lean_angle": 35.0,
  "lean_speed": 180.0,
  "fire_rate": 0.15,
  "magazine_size": 30,
  "reload_time": 2.0,
  "shoot_range": 100.0,
  "weapons": [],
  "starting_reserve_ammo": 90,
  "primary_damage": 15.0,
  "primary_range": 1.2,
  "primary_cooldown": 0.35,
  "secondary_damage": 30.0,
  "secondary_range": 1.6,
  "secondary_cooldown": 0.7,
  "block_reduction": 0.7,
  "dodge_force": 12.0,
  "dodge_cooldown": 1.0,
  "dodge_i_frames": 0.15,
  "throw_damage": 20.0,
  "throw_range": 1.8,
  "throw_force": 12.0,
  "throw_cooldown": 0.6,
  "interact_range": 2.5,
  "modes": [],
  "default_mode": "",
  "mode_switch_key": "Tab",
  "extra_usings": [],
  "extra_fields": [],
  "awake_extra": "",
  "update_extra": "",
  "fixed_update_extra": "",
  "extra_methods": [],
  "extensions_fields": [],
  "extensions_methods": [],
  "extensions_metadata": {{}}
}}

Author REAL prompt-specific behavior in the extras. Output ONLY the JSON.
"""


# ── Pipeline ──────────────────────────────────────────────────────────────────

class ControllerPipeline(UnityMicroPipeline):
    kind            = "controller"
    display_name    = "Unity Character Controller"
    schema_cls      = ControllerSchema
    variants        = VARIANTS
    default_variant = "third_person"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "controller"

    # Field names the template always emits — extra_fields with these names are
    # stripped by the fixer to avoid double-declaration.
    _RESERVED_FIELD_NAMES = {
        # movement core
        "movespeed", "sprintspeed", "jumpforce", "groundraylen", "groundmask",
        # locomotion state
        "_rb", "_cc", "_velocity", "_grounded", "_airjumpsleft", "maxairjumps",
        "flyspeed", "_sprintHeld", "_moveInput", "_jumpThisFrame", "_crouching",
        "crouchheight", "crouchspeed",
        # aircraft fields
        "maxthrust", "liftcoefficient", "pitchspeed", "rollspeed", "yawspeed",
        "throttlerate", "stallspeed", "maxairspeed", "_throttle",
        # vehicle rig
        "maxmotortorque", "maxsteerangle", "suspensiondistance",
        "suspensionspring", "suspensiondamper", "driftslipmultiplier",
        "wheelradius", "handbraketorque", "_wheels", "_wheelvisuals",
        "_headlights", "_brakelight", "_smoke", "_basesidestiffness",
        "_lightson", "_steerinput", "_throttleinput",
        # bike lean
        "maxleanangle", "leanspeed", "_currentlean", "_leanpivot",
        # shooting
        "firerate", "magazinesize", "reloadtime", "shootrange",
        "_lastfiretime", "_currentammo", "_isreloading", "_isaiming",
        "adsfovmultiplier", "adsspeedmultiplier", "_weaponindex",
        "_grenadesLeft", "maxgrenades", "throwforce",
        "_meleeCooldown",
        "weapons", "weaponnames", "weapondamage", "weaponfirerate",
        "weaponmagazinesize", "weaponreloadtime", "weaponrange",
        "startingreserveammo", "_reserveammo", "addammo", "reserveammo",
        # fighting
        "primarydamage", "primaryrange", "primarycooldown",
        "secondarydamage", "secondaryrange", "secondarycooldown",
        "blockreduction", "dodgeforce", "dodgecooldown", "dodgeiframes",
        "_primarycooldown", "_secondarycooldown", "_isblocking",
        "_isdodging", "_isinvincible", "_dodgecooldown", "_grabcooldown",
        "throwdamage", "throwrange", "throwforce", "throwcooldown",
        "_throwcooldown", "_carriedobject", "_facing",
        # interaction
        "interactrange", "_inventoryopen", "_mapopen", "_ispaused",
        # vehicle nitro/brake
        "_nitroCooldown", "_nitroActive",
        # multi_mode (ControllerMultiMode.cs.j2)
        "defaultmode", "modeswitchkey", "_modes", "_currentmode",
        "_carvisualroot", "_carbodycollider",
        # multi_mode "flying" kind (max/lift/pitch/roll/yaw/throttle/stall/
        # air-speed fields and "_throttle" are already reserved above)
        "_rudderinput", "_flapsdown", "_geardown",
        "flapsliftmultiplier", "geardragmultiplier",
    }

    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.InputSystem", "System.Collections"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "MovementController")

        # flaps/landing_gear only render inside a multi_mode "flying" entry
        # (ControllerMultiMode.cs.j2's flying macros) — every other variant,
        # including the single-variant "aircraft"/"flying", has no template
        # section for them, so surviving into schema.abilities there would be
        # a silent gap (present in the schema, absent from the build).
        if variant != "multi_mode":
            schema.abilities = [a for a in schema.abilities
                                if a not in ("flaps", "landing_gear")]

        # Variant-specific ability pruning
        if variant.startswith("2d_") or variant == "platformer_2_5d":
            schema.abilities = [a for a in schema.abilities
                                if a not in ("glide", "pitch", "roll_plane", "throttle")]
        if variant.startswith("vehicle_"):
            # Vehicle is its own movement system — locomotion abilities don't apply
            schema.abilities = [a for a in schema.abilities
                                if a in ("nitro", "brake", "open_inventory", "open_map", "pause")]
        if variant == "aircraft":
            # Flight uses its own movement; inject the flight atoms if omitted
            for ab in ("pitch", "roll_plane", "throttle"):
                if ab not in schema.abilities:
                    schema.abilities.append(ab)
            # Remove conflicting locomotion atoms
            schema.abilities = [a for a in schema.abilities
                                if a not in ("jump", "double_jump", "wall_jump", "fly",
                                              "ascend", "descend", "sprint")]
        if variant == "swimming":
            # Ascend/descend replace jump for swim
            if "jump" in schema.abilities:
                schema.abilities = [a for a in schema.abilities if a != "jump"]
            for ab in ("ascend", "descend"):
                if ab not in schema.abilities:
                    schema.abilities.append(ab)
        if variant == "flying":
            # Free-fly: ascend/descend override jump
            schema.abilities = [a for a in schema.abilities
                                if a not in ("jump", "double_jump")]
        if variant == "pointer":
            # There is no body, so every locomotion and combat atom is
            # meaningless here — keep only what a cursor can actually do. Drag
            # is added when nothing survives, because a board you can hover but
            # never move anything on is a picture.
            schema.abilities = [a for a in schema.abilities
                                if a in ("drag", "interact", "use_item",
                                         "open_inventory", "open_map", "pause")]
            if "drag" not in schema.abilities:
                schema.abilities.append("drag")

        if variant == "multi_mode":
            schema = self._fix_multi_mode(schema)

        # The LLM sometimes authors extra_*/extra_methods code that calls a
        # fighting ability's own method/fields (TryThrow(), _throwCooldown, ...)
        # without also adding that ability to `abilities`. The base harden()'s
        # binding check (bind_authored_code, template_field_types) does a
        # STATIC scan of Controller.cs.j2's source text — it finds
        # `_throwCooldown` declared somewhere under the "throw" ability's own
        # {% if %} block and treats it as a permanently known symbol, so it
        # never flags the reference as unbound even when THIS schema's
        # abilities list would never actually render that declaration.
        # Confirmed live, StreetBeat2D: TryThrowObject() called
        # TryThrow()/_carriedObject/_throwCooldown with "throw" absent from
        # abilities — 4 x CS0103. Same "build, don't delete" philosophy as
        # build_missing_fields (code_fields.py): the LLM's own code is
        # evidence of real intent, so add the missing ability rather than
        # neutralizing every reference to it.
        _fighting_ability_signals: dict[str, tuple[str, ...]] = {
            "throw":           ("trythrow", "_throwcooldown", "_carriedobject"),
            "grab":            ("trygrab", "_grabcooldown"),
            "primary_attack":  ("tryprimaryattack", "_primarycooldown"),
            "secondary_attack": ("trysecondaryattack", "_secondarycooldown"),
            "block":           ("_isblocking",),
            "dodge":           ("dodgeroutine", "_isdodging", "_isinvincible"),
        }
        _authored_blob = "\n".join([
            schema.awake_extra or "", schema.update_extra or "", schema.fixed_update_extra or "",
        ] + [m.body or "" for m in schema.extra_methods]).lower()
        for _ability, _signals in _fighting_ability_signals.items():
            if _ability in schema.abilities:
                continue
            if any(re.search(rf"\b{re.escape(s)}\b", _authored_blob) for s in _signals):
                schema.abilities.append(_ability)

        # fly binds Shift; drop sprint to avoid key conflict
        if "fly" in schema.abilities and "sprint" in schema.abilities:
            schema.abilities = [a for a in schema.abilities if a != "sprint"]

        # cycle_weapon switches between weapon-loadout entries — meaningless
        # without "shoot", and SwitchWeapon() reclamps _currentAmmo against
        # the shoot ability's own fields, so cycle_weapon-without-shoot would
        # be a compile-time symbol gap (CS0103), not just a no-op.
        if "cycle_weapon" in schema.abilities and "shoot" not in schema.abilities:
            schema.abilities.append("shoot")

        # Speed sanity
        if schema.move_speed <= 0:
            schema.move_speed = 6.0
        if schema.sprint_speed <= schema.move_speed:
            schema.sprint_speed = schema.move_speed * 1.6
        if schema.jump_force < 0:
            schema.jump_force = 0.0

        # Aircraft param clamping
        schema.max_thrust       = max(10.0,   min(2000.0, schema.max_thrust))
        schema.lift_coefficient = max(0.0,    min(200.0,  schema.lift_coefficient))
        schema.pitch_speed      = max(5.0,    min(180.0,  schema.pitch_speed))
        schema.roll_speed       = max(5.0,    min(360.0,  schema.roll_speed))
        schema.yaw_speed        = max(5.0,    min(180.0,  schema.yaw_speed))
        schema.throttle_rate    = max(0.05,   min(5.0,    schema.throttle_rate))
        schema.stall_speed      = max(0.0,    min(100.0,  schema.stall_speed))
        schema.max_air_speed    = max(10.0,   min(500.0,  schema.max_air_speed))

        # Vehicle param clamping
        schema.max_motor_torque      = max(100.0,   min(8000.0,   schema.max_motor_torque))
        schema.max_steer_angle       = max(5.0,     min(50.0,     schema.max_steer_angle))
        schema.suspension_distance   = max(0.05,    min(0.6,      schema.suspension_distance))
        schema.suspension_spring     = max(5000.0,  min(120000.0, schema.suspension_spring))
        schema.suspension_damper     = max(500.0,   min(20000.0,  schema.suspension_damper))
        schema.drift_slip_multiplier = max(0.1,     min(1.0,      schema.drift_slip_multiplier))
        schema.wheel_radius          = max(0.1,     min(1.5,      schema.wheel_radius))

        # Bike param clamping
        schema.max_lean_angle = max(5.0,  min(70.0,  schema.max_lean_angle))
        schema.lean_speed     = max(30.0, min(720.0, schema.lean_speed))

        # Shooter clamping
        schema.starting_reserve_ammo = max(0, min(9999, schema.starting_reserve_ammo))
        schema.fire_rate     = max(0.0,  min(5.0,   schema.fire_rate))
        schema.magazine_size = max(1,    min(999,   schema.magazine_size))
        schema.reload_time   = max(0.1,  min(10.0,  schema.reload_time))
        schema.shoot_range   = max(1.0,  min(1000.0, schema.shoot_range))

        # Weapon loadout — a shoot-ability schema with no authored `weapons`
        # gets exactly one synthesized entry from the flat fields above,
        # damage=25.0 matching TryShoot()'s historical hardcoded value, so an
        # unauthored schema renders byte-identical behavior. Authored entries
        # get the same bounds as the flat fields, unconditionally — same as
        # fire_rate etc. above, which clamp regardless of whether "shoot"
        # ended up in abilities.
        if "shoot" in schema.abilities and not schema.weapons:
            schema.weapons = [WeaponDef(
                name="Weapon", damage=25.0,
                fire_rate=schema.fire_rate, magazine_size=schema.magazine_size,
                reload_time=schema.reload_time, shoot_range=schema.shoot_range,
            )]
        for w in schema.weapons:
            w.name          = (w.name or "Weapon").strip()[:40] or "Weapon"
            w.damage        = max(0.0, min(9999.0, w.damage))
            w.fire_rate     = max(0.0, min(5.0,    w.fire_rate))
            w.magazine_size = max(1,   min(999,    w.magazine_size))
            w.reload_time   = max(0.1, min(10.0,   w.reload_time))
            w.shoot_range   = max(1.0, min(1000.0, w.shoot_range))

        # Fighting clamping
        schema.primary_damage    = max(0.0,  min(9999.0, schema.primary_damage))
        schema.primary_range     = max(0.1,  min(10.0,   schema.primary_range))
        schema.primary_cooldown  = max(0.05, min(5.0,    schema.primary_cooldown))
        schema.secondary_damage  = max(0.0,  min(9999.0, schema.secondary_damage))
        schema.secondary_range   = max(0.1,  min(10.0,   schema.secondary_range))
        schema.secondary_cooldown = max(0.05, min(5.0,   schema.secondary_cooldown))
        schema.block_reduction   = max(0.0,  min(1.0,    schema.block_reduction))
        schema.dodge_force       = max(0.0,  min(100.0,  schema.dodge_force))
        schema.dodge_cooldown    = max(0.1,  min(10.0,   schema.dodge_cooldown))
        schema.dodge_i_frames    = max(0.0,  min(1.0,    schema.dodge_i_frames))
        schema.throw_damage      = max(0.0,  min(9999.0, schema.throw_damage))
        schema.throw_range       = max(0.3,  min(10.0,   schema.throw_range))
        schema.throw_force       = max(0.0,  min(100.0,  schema.throw_force))
        schema.throw_cooldown    = max(0.05, min(5.0,    schema.throw_cooldown))

        schema.interact_range = max(0.1, min(20.0, schema.interact_range))

        # Lint pass on LLM-authored extras
        from .code_fields import _KNOWN_NAMESPACES
        schema.extra_usings = lint_usings(
            schema.extra_usings,
            already_in_template=self._TEMPLATE_USINGS,
            allowed=_KNOWN_NAMESPACES,
        )
        # Controller.cs.j2 renders awake_extra, so an initializer that
        # references instance state (CS0236) is re-homed into Awake() instead
        # of dropped — see `initializer_instance_refs` in code_fields.
        _field_inits: list[str] = []
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES,
            hoist_into=_field_inits,
        )
        schema.awake_extra        = lint_code_fragment(schema.awake_extra,        what="Awake()")
        if _field_inits:
            schema.awake_extra = "\n".join(
                _field_inits + ([schema.awake_extra] if schema.awake_extra else []))
        schema.update_extra       = lint_code_fragment(schema.update_extra,       what="Update()")
        schema.fixed_update_extra = lint_code_fragment(schema.fixed_update_extra, what="FixedUpdate()")
        taken: set[str] = set()
        schema.extra_methods = [
            m2 for m in schema.extra_methods
            if (m2 := lint_method(m, taken_names=taken)) is not None
        ]

        schema = apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)

        return schema

    def _fix_multi_mode(self, schema: ControllerSchema) -> ControllerSchema:
        """Per-mode repair for the multi_mode variant — de-dupes the registry,
        prunes each mode's abilities the same way default_fix prunes the
        single-variant list above, and resolves default_mode/mode_switch_key.
        Never raises: an unresolvable registry is recorded in schema.meta for
        the playability gate (`_check_multi_mode_registry`) to surface loudly,
        not silently discarded.
        """
        seen_kinds: set[str] = set()
        deduped: list[ControllerMode] = []
        dropped_dupes: list[str] = []
        for m in schema.modes:
            if m.kind in seen_kinds:
                dropped_dupes.append(m.kind)
                continue
            seen_kinds.add(m.kind)
            deduped.append(m)
        schema.modes = deduped

        # Pruned to exactly what ControllerMultiMode.cs.j2 (Phase 1) renders —
        # an ability that survived here but had no template section would be a
        # silent gap (present in the schema, absent from the build). Grows in
        # step with the template as later phases add mode kinds/abilities.
        for m in schema.modes:
            if m.kind == "vehicle_car":
                m.abilities = [a for a in m.abilities if a == "nitro"]
            elif m.kind == "flying":
                m.abilities = [a for a in m.abilities if a in ("flaps", "landing_gear")]
            else:  # on_foot
                m.abilities = [a for a in m.abilities if a in ("jump", "sprint")]

        notes: dict = {}
        if dropped_dupes:
            notes["dropped_duplicate_kinds"] = dropped_dupes
        if schema.default_mode not in seen_kinds:
            prior = schema.default_mode
            schema.default_mode = schema.modes[0].kind if schema.modes else "on_foot"
            notes["default_mode_fallback"] = (
                f"default_mode {prior!r} not in registered modes "
                f"{sorted(seen_kinds)!r} — fell back to {schema.default_mode!r}")
        if schema.mode_switch_key not in VALID_MODE_SWITCH_KEYS:
            notes["mode_switch_key_fallback"] = (
                f"mode_switch_key {schema.mode_switch_key!r} is not usable "
                "identically in both input backends — fell back to 'Tab'")
            schema.mode_switch_key = "Tab"
        if notes:
            schema.meta["multi_mode"] = notes

        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # The pointer variant has its own template: Controller.cs.j2 is a
        # movement rig branching on which KIND of body it drives, and a game
        # with no body belongs in none of those branches. multi_mode is the
        # same reasoning in the other direction — MORE than one body kind on
        # the same class — so it also gets its own template rather than a
        # second full branch bolted onto Controller.cs.j2.
        tpl = ("PointerController.cs.j2" if variant == "pointer"
               else "ControllerMultiMode.cs.j2" if variant == "multi_mode"
               else "Controller.cs.j2")
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template(tpl).render(s=schema, variant=variant),
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

        if "throw" in schema.abilities:
            # Shared, unparametrized helper for whatever gets thrown — see
            # ThrownObject.cs.j2's own header comment for the (narrow, accepted)
            # duplicate-class risk if a project ever composed two throw-capable
            # controllers, which the current compose graph never does (one
            # player controller per project).
            (scripts_dir / "ThrownObject.cs").write_text(
                env.get_template("ThrownObject.cs.j2").render(),
                encoding="utf-8",
            )
            count += 1

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
