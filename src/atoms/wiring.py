"""
Wiring atoms — Unity-only event-seam primitives.

Each atom represents one typed C# subscription between two subsystems.
These atoms are the building blocks that GameWiring.cs uses to connect the
composed micro subsystems at scene startup.

Domain: Unity ONLY (engines=["unity"]).
GTA5 has no MonoBehaviour event model; its equivalent is in backends/gta5.py.
"""
from .base import AtomDef, AtomParam

WIRING_ATOMS: list[AtomDef] = [
    AtomDef(
        name="WireHealthToHUD",
        category="wiring",
        description="Connect Health.OnHealthChanged → HUD.SetHealth and sync the initial value.",
        engines=["unity"],
        duration="instant",
        side_effects=["hud_reflects_live_hp"],
        params=[
            AtomParam(name="health_var", type="string", default="_health",
                      description="GameWiring field name for the health component"),
            AtomParam(name="hud_var",    type="string", default="_hud",
                      description="GameWiring field name for the HUD component"),
        ],
    ),
    AtomDef(
        name="WireHealthToFSM",
        category="wiring",
        description="Connect Health.OnDeath → FSM.Kill to drive the Dead state on player death.",
        engines=["unity"],
        duration="instant",
        side_effects=["fsm_transitions_on_death"],
        params=[
            AtomParam(name="health_var",       type="string", default="_health",
                      description="GameWiring field name for the health component"),
            AtomParam(name="statemachine_var", type="string", default="_statemachine",
                      description="GameWiring field name for the FSM component"),
        ],
    ),
    AtomDef(
        name="WireHealthToAudio",
        category="wiring",
        description=(
            "Connect Health.OnDamaged/OnDeath/OnHealed → AudioDirector.Play using "
            "stored Action delegates so -= can properly unsubscribe."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["audio_responds_to_hp_changes"],
        params=[
            AtomParam(name="health_var", type="string", default="_health"),
            AtomParam(name="audio_var",  type="string", default="_audio"),
            AtomParam(name="cue_hurt",   type="sfx_id", default="player_hurt"),
            AtomParam(name="cue_death",  type="sfx_id", default="player_death"),
            AtomParam(name="cue_heal",   type="sfx_id", default="checkpoint_save_led"),
        ],
    ),
    AtomDef(
        name="WireFSMToAnimator",
        category="wiring",
        description=(
            "Connect FSM.OnStateChanged → GameWiring.OnFSMStateChanged, which drives "
            "Animator.CrossFade per state."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["animator_mirrors_fsm_state"],
        params=[
            AtomParam(name="statemachine_var", type="string", default="_statemachine"),
            AtomParam(name="animator_var",     type="string", default="_animator"),
        ],
    ),
    AtomDef(
        name="WireInputToController",
        category="wiring",
        description=(
            "Assign the InputReader ScriptableObject to PlayerController via SetInputReader, "
            "enabling typed event-driven input instead of polled legacy Input."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["controller_uses_input_system"],
        params=[
            AtomParam(name="input_var",      type="string", default="_inputSO"),
            AtomParam(name="controller_var", type="string", default="_controller"),
        ],
    ),
    AtomDef(
        name="WireInputToFSM",
        category="wiring",
        description=(
            "Connect InputReader.OnMove/OnJump → FSM.SetMove/SetJump using stored "
            "Action delegates. SetJump stored so -= does not silently no-op."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["fsm_receives_typed_input"],
        params=[
            AtomParam(name="input_var",        type="string", default="_inputSO"),
            AtomParam(name="statemachine_var", type="string", default="_statemachine"),
        ],
    ),
    AtomDef(
        name="WireGroundedBridge",
        category="wiring",
        description=(
            "Per-frame Update bridge: push Controller.IsGrounded to FSM.SetGrounded "
            "and Animator.SetGrounded. Runs every frame — placed in Update(), not Awake()."
        ),
        engines=["unity"],
        duration="until_complete",   # lives in Update loop
        side_effects=["fsm_and_animator_receive_grounded_state"],
        params=[
            AtomParam(name="controller_var",   type="string", default="_controller"),
            AtomParam(name="statemachine_var", type="string", default="_statemachine"),
            AtomParam(name="animator_var",     type="string", default="_animator"),
        ],
    ),
    AtomDef(
        name="WireTriggerToSave",
        category="wiring",
        description=(
            "Add a CheckpointSaveRelay component to the trigger GameObject so "
            "OnTriggerActivate calls SaveSystem.Save()."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["checkpoint_saves_on_trigger"],
        params=[
            AtomParam(name="trigger_var",  type="string", default="_trigger"),
            AtomParam(name="saveload_var", type="string", default="_saveload"),
        ],
    ),
]
