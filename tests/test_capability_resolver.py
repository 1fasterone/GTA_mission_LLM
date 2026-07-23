"""
Capability resolver tests — what schema.capabilities is allowed to inject.

schema.atoms is a ONE-SHOT Start() sequence (unity_generator's "Global atom
sequencer" → GameSequencer.cs). Scene-init capabilities belong there; the
controller and physics capabilities do NOT — they describe continuous, per-frame
behaviour that the composed subsystems implement from the same typed params, and
folding them in produces a sequence that reads like it defines player movement
while being unable to drive it.
"""

import pytest

from src.capability_resolver import (
    SUBSYSTEM_OWNED_CAPABILITIES,
    capabilities_to_active_microgens,
    capabilities_to_atoms,
    resolve_capabilities,
    resolve_microgens,
)
from src.unity_compose import subsystems_for
from src.unity_generator import UnityProjectGenerator
from src.unity_schema import ProjectCapabilities, UnitySchema

CONTROLLER_ATOMS = {"Look", "Move", "Sprint", "Jump", "Crouch"}
PHYSICS_ATOMS    = {"SetGravity", "ToggleCollision"}


def caps(**kw) -> ProjectCapabilities:
    return ProjectCapabilities.model_validate(kw)


FULL = dict(
    controller={"movement": True, "jump": True, "sprint": True, "crouch": True,
                "speed": 6.0, "jump_force": 8.0},
    physics={"gravity": True, "collision": "dynamic", "mass": 80.0},
    hud={"show_health": True, "show_objective": True},
    input={"bindings": [{"action": "Fire", "key": "Mouse0"}]},
    model={"lod": "auto", "animations": {"Idle": "Idle"}},
    animation={"rig_type": "humanoid", "clips": {"Run": "Run"}},
    dialog={"system": "branching"},
    ai={"pathfinding": True, "behavior": "chase"},
    vfx={"particles": True},
)


def schema_with(capabilities=None, **kw) -> UnitySchema:
    return UnitySchema.model_validate({
        "project_name": "P", "game_type": "3D", "platform": "pc",
        "render_pipeline": "URP",
        **({"capabilities": capabilities} if capabilities else {}),
        **kw,
    })


# ── The rule ──────────────────────────────────────────────────────────────────

class TestSubsystemOwnedCapabilitiesAreNotFolded:
    def test_controller_and_physics_are_the_owned_set(self):
        assert SUBSYSTEM_OWNED_CAPABILITIES == {"controller", "physics"}

    def test_controller_atoms_never_reach_schema_atoms(self):
        schema, _ = resolve_capabilities(schema_with(caps(**FULL)))
        assert not (CONTROLLER_ATOMS & {a.atom for a in schema.atoms})

    def test_physics_atoms_never_reach_schema_atoms(self):
        schema, _ = resolve_capabilities(schema_with(caps(**FULL)))
        assert not (PHYSICS_ATOMS & {a.atom for a in schema.atoms})

    def test_controller_only_capabilities_inject_nothing(self):
        schema, msgs = resolve_capabilities(
            schema_with(caps(controller={"speed": 9.0}, physics={"gravity": False})))
        assert schema.atoms == []
        assert any("left out of schema.atoms" in m for m in msgs)

    def test_no_game_sequencer_for_controller_only_capabilities(self):
        """The whole point: a project whose only capability is the player's
        controller no longer emits a one-shot sequencer script at all."""
        schema, _ = resolve_capabilities(schema_with(caps(controller={"speed": 9.0})))
        files = UnityProjectGenerator(template_dir="templates/unity").generate(schema, {})
        assert not [p for p in files if "GameSequencer" in p]

    def test_scene_init_capabilities_are_still_folded(self):
        schema, msgs = resolve_capabilities(schema_with(caps(**FULL)))
        names = {a.atom for a in schema.atoms}
        assert {"ShowHUD", "BindAction", "SetModel", "EnableNavMesh",
                "SetAIBehavior", "EnableParticles"} <= names
        assert any("scene-init atoms injected" in m for m in msgs)

    def test_explicit_empty_exclude_still_returns_the_full_mapping(self):
        names = {a.atom for a in capabilities_to_atoms(caps(**FULL),
                                                       exclude=frozenset())}
        assert CONTROLLER_ATOMS <= names
        assert PHYSICS_ATOMS & names

    def test_no_capabilities_is_a_no_op(self):
        schema, msgs = resolve_capabilities(schema_with())
        assert schema.atoms == [] and msgs == []


# ── The behaviour is not lost — it moved to the subsystem that can run it ─────

class TestControllerCapabilityStillReachesTheController:
    def test_typed_params_drive_the_composed_controller(self):
        capabilities = caps(controller={"speed": 9.0, "sprint": True,
                                        "sprint_multiplier": 2.0, "jump": True,
                                        "jump_force": 12.0, "crouch": True})
        ctrl = next(s for s in subsystems_for("3D", ["physics"],
                                              capabilities=capabilities)
                    if s.kind == "controller")
        assert ctrl.options["move_speed"] == 9.0
        assert ctrl.options["sprint_speed"] == 18.0
        assert ctrl.options["jump_force"] == 12.0
        assert set(ctrl.options["abilities"]) == {"jump", "sprint", "crouch"}

    def test_controller_and_physics_are_still_reported_as_active_microgens(self):
        kinds = capabilities_to_active_microgens(caps(**FULL))
        assert "controller" in kinds and "physics" in kinds


# ── Author-supplied atoms are untouched ───────────────────────────────────────

class TestAuthoredAtomsSurvive:
    def test_schema_atoms_are_preserved_as_a_suffix(self):
        schema, _ = resolve_capabilities(schema_with(
            caps(hud={"show_health": True}),
            atoms=[{"atom": "ScreenFade", "args": {"alpha": 0.0}}]))
        assert schema.atoms[-1].atom == "ScreenFade"
        assert schema.atoms[0].atom == "ShowHUD"

    def test_a_controller_only_project_keeps_its_authored_atoms(self):
        schema, _ = resolve_capabilities(schema_with(
            caps(controller={"speed": 9.0}),
            atoms=[{"atom": "ScreenFade", "args": {"alpha": 0.0}}]))
        assert [a.atom for a in schema.atoms] == ["ScreenFade"]

    def test_microgen_atoms_are_unaffected(self):
        schema, _ = resolve_microgens(schema_with(
            microgens=[{"kind": "intro",
                        "atoms": [{"atom": "Wait", "args": {"seconds": 1.0}}]}]))
        assert [a.atom for a in schema.atoms] == ["Wait"]


# ── Anti-regression: nothing may reintroduce the fold ─────────────────────────

@pytest.mark.parametrize("collision", ["none", "static", "kinematic", "dynamic"])
def test_no_collision_setting_can_toggle_collision_at_start(collision):
    """A capability declaring collision:"none" used to emit
    ToggleCollision(enabled:false) into the start sequence."""
    schema, _ = resolve_capabilities(schema_with(
        caps(physics={"gravity": False, "collision": collision})))
    assert not [a for a in schema.atoms if a.atom == "ToggleCollision"]
    assert not [a for a in schema.atoms if a.atom == "SetGravity"]
