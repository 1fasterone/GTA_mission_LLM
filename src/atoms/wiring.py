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
            "stored Action delegates so -= can properly unsubscribe. cue_hurt/"
            "cue_death/cue_heal are None unless the caller matched a real cue id "
            "from this project's resolved audio bank — a role with no match is "
            "omitted entirely, never wired to a fabricated generic id."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["audio_responds_to_hp_changes"],
        params=[
            AtomParam(name="health_var", type="string", default="_health"),
            AtomParam(name="audio_var",  type="string", default="_audio"),
            AtomParam(name="cue_hurt",   type="sfx_id", default=None,
                      description="Matched cue id for the hurt role, or None to omit"),
            AtomParam(name="cue_death",  type="sfx_id", default=None,
                      description="Matched cue id for the death role, or None to omit"),
            AtomParam(name="cue_heal",   type="sfx_id", default=None,
                      description="Matched cue id for the heal role, or None to omit"),
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
        name="WireTriggerToObjectives",
        category="wiring",
        description=(
            "Connect TriggerZone.OnActivated -> MissionObjectives.ReportProgress for a "
            "`reach` objective, so entering the zone advances the mission. objective_id "
            "is always a real id from this project's authored mission, never synthesized."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["reach_objective_advances_on_zone_enter"],
        params=[
            AtomParam(name="trigger_var",    type="string", default="_trigger"),
            AtomParam(name="objectives_var", type="string", default="_objectives"),
            AtomParam(name="delegate_var",   type="string", default="_onReachObjective",
                      description="Stored Action field so OnDestroy can unsubscribe"),
            AtomParam(name="objective_id",   type="string", default=None,
                      description="Authored objective id to advance; None omits the seam"),
        ],
    ),
    AtomDef(
        name="WireInteractableToObjectives",
        category="wiring",
        description=(
            "Connect Interactable.OnInteract -> MissionObjectives.ReportProgress for a "
            "`collect` or `interact` objective. Each interaction advances progress by "
            "one, so an objective count of N needs N pickups in the scene."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["collect_objective_advances_on_pickup"],
        params=[
            AtomParam(name="interactable_var", type="string", default="_interactable"),
            AtomParam(name="objectives_var",   type="string", default="_objectives"),
            AtomParam(name="delegate_var",     type="string", default="_onCollectObjective"),
            AtomParam(name="objective_id",     type="string", default=None,
                      description="Authored objective id to advance; None omits the seam"),
        ],
    ),
    AtomDef(
        name="WireSpawnerToObjectives",
        category="wiring",
        description=(
            "Connect Spawner.OnDespawned -> MissionObjectives.ReportProgress for an "
            "`eliminate` objective. The spawner raises OnDespawned once per tracked "
            "instance that leaves play, which is the only honest kill signal a composed "
            "project has: no enemy Health component is composed."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["eliminate_objective_advances_on_enemy_despawn"],
        params=[
            AtomParam(name="spawner_var",    type="string", default="_spawner"),
            AtomParam(name="objectives_var", type="string", default="_objectives"),
            AtomParam(name="delegate_var",   type="string", default="_onEliminateObjective"),
            AtomParam(name="objective_id",   type="string", default=None,
                      description="Authored objective id to advance; None omits the seam"),
        ],
    ),
    AtomDef(
        name="WireObjectivesToHUD",
        category="wiring",
        description=(
            "Connect MissionObjectives.OnObjectiveProgress AND .OnObjectiveComplete -> a "
            "HUD objective readout, via a caller-supplied refresh method that reads "
            "ActiveObjective/CompletedRequired/TotalRequired. Both events are bound "
            "because ReportProgress raises OnObjectiveProgress before it marks the "
            "objective complete, so a progress-only binding shows a stale active "
            "objective. Fires once at Awake so the readout is correct before the first "
            "gameplay event."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["hud_shows_current_objective_and_tally"],
        params=[
            AtomParam(name="objectives_var", type="string", default="_objectives"),
            AtomParam(name="hud_var",        type="string", default="_hud"),
            AtomParam(name="refresh_method", type="string", default="RefreshObjectiveHud",
                      description="GameWiring method that pushes the current objective "
                                  "into the HUD; the caller emits it as a helper"),
        ],
    ),
    AtomDef(
        name="WireInventoryToHUD",
        category="wiring",
        description=(
            "Connect Inventory.OnChanged -> a HUD counter readout showing "
            "Inventory.TotalCount. `setter` is the widget setter name the HUD "
            "provably declares — the composer passes only a setter read back from "
            "the post-lint widget list, never a guessed one."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["hud_reflects_carried_item_count"],
        params=[
            AtomParam(name="inventory_var", type="string", default="_inventory"),
            AtomParam(name="hud_var",       type="string", default="_hud"),
            AtomParam(name="setter",        type="string", default="SetCarried",
                      description="HUD counter setter to call with TotalCount"),
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
