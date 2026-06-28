"""Character controller atoms — per-tick input/movement primitive opcodes.

NOT goal-directed (MoveTo goes somewhere; Move applies velocity this frame).
These are the raw opcodes a character controller executes each tick.

Groups:
  LOCOMOTION  — Move, Rotate, Jump, Sprint, Crouch, Look, Ascend, Descend, Boost, Dash
  AIRCRAFT    — Pitch, RollPlane, Throttle
  SHOOTING    — Shoot, AimDownSights, Reload, CycleWeapon, ThrowGrenade, MeleeAttack
  FIGHTING    — PrimaryAttack, SecondaryAttack, Block, Dodge, Grab
  INTERACTION — Interact, UseItem, OpenInventory, OpenMap, Pause
  VEHICLE     — Brake, Nitro
"""

from .base import AtomDef, AtomParam

CONTROLLER_ATOMS: list[AtomDef] = [

    # ── LOCOMOTION ─────────────────────────────────────────────────────────────

    AtomDef(
        name="Move",
        category="controller",
        description="Apply directional velocity to the entity for one tick (WASD always).",
        duration="instant",
        side_effects=["position"],
        aliases=["move", "apply_velocity", "walk", "strafe"],
        engines=["unity"],
        params=[
            AtomParam(name="dir_x",  type="float", default=0.0, description="Horizontal direction (-1 to 1)"),
            AtomParam(name="dir_z",  type="float", default=1.0, description="Forward/back direction (-1 to 1)"),
            AtomParam(name="speed",  type="float", default=6.0, description="Units per second"),
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
        description="Apply an upward impulse force to the entity's Rigidbody (Space).",
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
        description="Multiply move speed while held (LeftShift).",
        duration="until_complete",
        side_effects=["speed"],
        aliases=["sprint", "run", "dash_speed"],
        engines=["unity"],
        params=[
            AtomParam(name="multiplier", type="float", default=1.7, description="Speed multiplier when active"),
        ],
    ),
    AtomDef(
        name="Crouch",
        category="controller",
        description="Scale collider and move speed down while held (LeftCtrl).",
        duration="until_complete",
        side_effects=["scale", "collision_shape", "speed"],
        aliases=["crouch", "duck", "prone"],
        engines=["unity"],
        params=[
            AtomParam(name="height_scale", type="float", default=0.55, description="Collider height scale when crouched"),
            AtomParam(name="speed",        type="float", default=3.0,  description="Move speed while crouching"),
        ],
    ),
    AtomDef(
        name="Look",
        category="controller",
        description="Apply pitch and yaw look rotation from mouse/stick input.",
        duration="instant",
        side_effects=["rotation"],
        aliases=["look", "aim", "look_input", "mouse_look"],
        engines=["unity"],
        params=[
            AtomParam(name="sensitivity", type="float", default=2.0, description="Input sensitivity multiplier"),
        ],
    ),
    AtomDef(
        name="Ascend",
        category="controller",
        description="Move up along world Y — swimming/flying up (Space).",
        duration="instant",
        side_effects=["position"],
        aliases=["ascend", "rise", "fly_up", "swim_up"],
        engines=["unity"],
        params=[
            AtomParam(name="speed", type="float", default=4.0, description="Ascent speed in units/sec"),
        ],
    ),
    AtomDef(
        name="Descend",
        category="controller",
        description="Move down along world Y — swimming/flying down (LeftCtrl/C).",
        duration="instant",
        side_effects=["position"],
        aliases=["descend", "sink", "fly_down", "swim_down"],
        engines=["unity"],
        params=[
            AtomParam(name="speed", type="float", default=4.0, description="Descent speed in units/sec"),
        ],
    ),
    AtomDef(
        name="Boost",
        category="controller",
        description="Burst speed increase for a limited duration (Q or LeftShift).",
        duration="duration_param",
        side_effects=["speed", "vfx"],
        aliases=["boost", "turbo", "nitro_burst"],
        engines=["unity"],
        params=[
            AtomParam(name="multiplier", type="float", default=2.5, description="Speed multiplier during boost"),
            AtomParam(name="duration",   type="float", default=0.4, description="Burst duration in seconds"),
            AtomParam(name="cooldown",   type="float", default=2.0, description="Cooldown before next boost"),
        ],
    ),
    AtomDef(
        name="Dash",
        category="controller",
        description="Quick directional dash with brief invincibility (Q or double-tap dir).",
        duration="duration_param",
        side_effects=["position", "velocity"],
        aliases=["dash", "roll_evade", "quick_step"],
        engines=["unity"],
        params=[
            AtomParam(name="force",    type="float", default=15.0, description="Impulse force in dash direction"),
            AtomParam(name="duration", type="float", default=0.15, description="Dash window in seconds"),
            AtomParam(name="cooldown", type="float", default=1.0,  description="Cooldown before next dash"),
            AtomParam(name="i_frames", type="float", default=0.1,  description="Invincible seconds during dash"),
        ],
    ),

    # ── AIRCRAFT ──────────────────────────────────────────────────────────────

    AtomDef(
        name="Pitch",
        category="controller",
        description="Rotate aircraft nose up/down around local X (W/S in flight mode).",
        duration="instant",
        side_effects=["rotation"],
        aliases=["pitch", "nose_up", "nose_down", "elevator"],
        engines=["unity"],
        params=[
            AtomParam(name="pitch_speed", type="float", default=60.0, description="Degrees/sec at full input"),
        ],
    ),
    AtomDef(
        name="RollPlane",
        category="controller",
        description="Bank/roll the aircraft around local Z (A/D in flight mode).",
        duration="instant",
        side_effects=["rotation"],
        aliases=["roll_plane", "bank", "aileron"],
        engines=["unity"],
        params=[
            AtomParam(name="roll_speed", type="float", default=90.0, description="Degrees/sec at full input"),
        ],
    ),
    AtomDef(
        name="Throttle",
        category="controller",
        description="Adjust engine throttle level (LeftShift=up, LeftCtrl=down, or scroll).",
        duration="instant",
        side_effects=["speed"],
        aliases=["throttle", "engine_power", "thrust"],
        engines=["unity"],
        params=[
            AtomParam(name="rate",       type="float", default=0.5,  description="Throttle change per second"),
            AtomParam(name="max_thrust", type="float", default=80.0, description="Max forward force (Newtons)"),
        ],
    ),

    # ── SHOOTING ──────────────────────────────────────────────────────────────

    AtomDef(
        name="Shoot",
        category="controller",
        description="Fire the current weapon via raycast or projectile (Mouse0 / RT).",
        duration="instant",
        side_effects=["audio", "vfx", "damage"],
        aliases=["shoot", "fire", "fire_weapon", "primary_fire"],
        engines=["unity"],
        params=[
            AtomParam(name="fire_rate",   type="float", default=0.15, description="Seconds between shots"),
            AtomParam(name="magazine",    type="int",   default=30,   description="Ammo per magazine"),
            AtomParam(name="shoot_range", type="float", default=100.0, description="Raycast range in world units"),
        ],
    ),
    AtomDef(
        name="AimDownSights",
        category="controller",
        description="Enter ADS, narrowing FOV and reducing sway (Mouse1 / LT).",
        duration="until_complete",
        side_effects=["camera", "speed"],
        aliases=["aim_down_sights", "ads", "zoom", "iron_sights"],
        engines=["unity"],
        params=[
            AtomParam(name="fov_multiplier",   type="float", default=0.6,  description="FOV scale while aiming"),
            AtomParam(name="speed_multiplier", type="float", default=0.55, description="Move speed scale while aiming"),
        ],
    ),
    AtomDef(
        name="Reload",
        category="controller",
        description="Refill the weapon magazine after a timed delay (R).",
        duration="duration_param",
        side_effects=["ammo"],
        aliases=["reload", "refill", "reload_weapon"],
        engines=["unity"],
        params=[
            AtomParam(name="reload_time", type="float", default=2.0, description="Seconds to complete reload"),
        ],
    ),
    AtomDef(
        name="CycleWeapon",
        category="controller",
        description="Switch to the next or previous weapon in the loadout (scroll / E & Q).",
        duration="instant",
        side_effects=["weapon_slot"],
        aliases=["cycle_weapon", "switch_weapon", "next_weapon"],
        engines=["unity"],
        params=[
            AtomParam(name="weapon_count", type="int", default=3, description="Number of weapons in the loadout"),
        ],
    ),
    AtomDef(
        name="ThrowGrenade",
        category="controller",
        description="Lob a grenade prefab from the player's position (G).",
        duration="instant",
        side_effects=["projectile", "vfx"],
        aliases=["throw_grenade", "grenade", "frag", "lob"],
        engines=["unity"],
        params=[
            AtomParam(name="throw_force",  type="float", default=15.0, description="Launch speed in m/s"),
            AtomParam(name="max_grenades", type="int",   default=3,    description="Max grenade count"),
        ],
    ),
    AtomDef(
        name="MeleeAttack",
        category="controller",
        description="Quick melee swing with the held weapon — for shooters (V).",
        duration="duration_param",
        side_effects=["damage", "animation"],
        aliases=["melee_attack", "butt_stroke", "whip"],
        engines=["unity"],
        params=[
            AtomParam(name="damage",   type="float", default=25.0, description="Damage on hit"),
            AtomParam(name="range",    type="float", default=1.5,  description="Hit range in meters"),
            AtomParam(name="cooldown", type="float", default=0.8,  description="Cooldown in seconds"),
        ],
    ),

    # ── FIGHTING ──────────────────────────────────────────────────────────────

    AtomDef(
        name="PrimaryAttack",
        category="controller",
        description="Punch or light attack (J or Mouse0 in fighter context).",
        duration="duration_param",
        side_effects=["damage", "animation"],
        aliases=["primary_attack", "punch", "light_attack", "jab"],
        engines=["unity"],
        params=[
            AtomParam(name="damage",   type="float", default=15.0, description="Damage on hit"),
            AtomParam(name="range",    type="float", default=1.2,  description="Hit range in meters"),
            AtomParam(name="cooldown", type="float", default=0.35, description="Cooldown in seconds"),
        ],
    ),
    AtomDef(
        name="SecondaryAttack",
        category="controller",
        description="Kick or heavy attack — slower, higher damage (K or Mouse1 release).",
        duration="duration_param",
        side_effects=["damage", "animation"],
        aliases=["secondary_attack", "kick", "heavy_attack", "roundhouse"],
        engines=["unity"],
        params=[
            AtomParam(name="damage",   type="float", default=30.0, description="Damage on hit"),
            AtomParam(name="range",    type="float", default=1.6,  description="Hit range in meters"),
            AtomParam(name="cooldown", type="float", default=0.7,  description="Cooldown in seconds"),
        ],
    ),
    AtomDef(
        name="Block",
        category="controller",
        description="Hold to enter blocking stance, absorbing a fraction of damage (R or RMB).",
        duration="until_complete",
        side_effects=["damage_reduction"],
        aliases=["block", "guard", "parry", "defend"],
        engines=["unity"],
        params=[
            AtomParam(name="damage_reduction", type="float", default=0.7, description="Fraction absorbed (0–1)"),
        ],
    ),
    AtomDef(
        name="Dodge",
        category="controller",
        description="Quick invincible sidestep in the move direction (Q or double-tap dir).",
        duration="duration_param",
        side_effects=["position", "animation"],
        aliases=["dodge", "evade", "sidestep", "roll_evade"],
        engines=["unity"],
        params=[
            AtomParam(name="force",    type="float", default=12.0, description="Impulse in dodge direction"),
            AtomParam(name="duration", type="float", default=0.2,  description="Dodge window in seconds"),
            AtomParam(name="cooldown", type="float", default=1.0,  description="Cooldown before next dodge"),
            AtomParam(name="i_frames", type="float", default=0.15, description="Invincibility window in seconds"),
        ],
    ),
    AtomDef(
        name="Grab",
        category="controller",
        description="Attempt to grapple or grab a nearby opponent (G).",
        duration="duration_param",
        side_effects=["constraint", "animation"],
        aliases=["grab", "grapple", "throw_opponent", "suplex"],
        engines=["unity"],
        params=[
            AtomParam(name="range",    type="float", default=1.5, description="Grab range in meters"),
            AtomParam(name="cooldown", type="float", default=2.0, description="Cooldown in seconds"),
        ],
    ),

    # ── INTERACTION ───────────────────────────────────────────────────────────

    AtomDef(
        name="Interact",
        category="controller",
        description="Raycast for nearby interactable objects and call Interact() via SendMessage (E or F).",
        duration="instant",
        side_effects=["world_state"],
        aliases=["interact", "use", "pick_up", "talk", "activate"],
        engines=["unity"],
        params=[
            AtomParam(name="range", type="float", default=2.5, description="Interaction sphere radius in meters"),
        ],
    ),
    AtomDef(
        name="UseItem",
        category="controller",
        description="Consume or activate the selected inventory item (Q or X).",
        duration="instant",
        side_effects=["inventory", "vfx"],
        aliases=["use_item", "consume", "activate_item"],
        engines=["unity"],
        params=[],
    ),
    AtomDef(
        name="OpenInventory",
        category="controller",
        description="Toggle the inventory panel via UnityEvent (Tab or I).",
        duration="instant",
        side_effects=["ui"],
        aliases=["open_inventory", "toggle_inventory", "inventory_panel"],
        engines=["unity"],
        params=[],
    ),
    AtomDef(
        name="OpenMap",
        category="controller",
        description="Toggle the in-game map overlay via UnityEvent (M).",
        duration="instant",
        side_effects=["ui"],
        aliases=["open_map", "toggle_map", "minimap"],
        engines=["unity"],
        params=[],
    ),
    AtomDef(
        name="Pause",
        category="controller",
        description="Freeze Time.timeScale and fire OnPauseToggle UnityEvent (Escape).",
        duration="instant",
        side_effects=["time", "ui"],
        aliases=["pause", "toggle_pause", "esc_menu"],
        engines=["unity"],
        params=[],
    ),

    # ── VEHICLE EXTRAS ────────────────────────────────────────────────────────

    AtomDef(
        name="Brake",
        category="controller",
        description="Apply dedicated brake torque to all wheels, independent of throttle (B or S).",
        duration="until_complete",
        side_effects=["velocity"],
        aliases=["brake", "foot_brake", "decelerate"],
        engines=["unity"],
        params=[
            AtomParam(name="torque", type="float", default=3000.0, description="Brake torque per wheel"),
        ],
    ),
    AtomDef(
        name="Nitro",
        category="controller",
        description="Timed NOS burst that multiplies motor torque (N).",
        duration="duration_param",
        side_effects=["speed", "vfx"],
        aliases=["nitro", "nos", "nitrous"],
        engines=["unity"],
        params=[
            AtomParam(name="multiplier", type="float", default=2.0,  description="Torque multiplier during nitro"),
            AtomParam(name="duration",   type="float", default=3.0,  description="Burst duration in seconds"),
            AtomParam(name="cooldown",   type="float", default=10.0, description="Recharge time in seconds"),
        ],
    ),
]
