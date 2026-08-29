"""Tests for the Unity micro-generators (controller/camera/physics/hud)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.micro import REGISTRY
from src.micro.controller     import ControllerPipeline, ControllerSchema
from src.micro.camera         import CameraRigPipeline,  CameraRigSchema
from src.micro.physics        import PhysicsPipeline,    PhysicsSchema
from src.micro.hud            import HUDPipeline,        HUDSchema
from src.micro.input_actions  import InputPipeline,      InputSchema, InputMap, InputAction
from src.micro.audio          import AudioPipeline,      AudioSchema
from src.micro.ai             import AIPipeline,         AISchema, AIState
from src.micro.spawner        import SpawnerPipeline,    SpawnerSchema
from src.micro.saveload       import SaveLoadPipeline,   SaveLoadSchema
from src.micro.inventory      import InventoryPipeline,  InventorySchema
from src.micro.dialogue       import DialoguePipeline,   DialogueSchema, DialogueNode, DialogueChoice
from src.micro.menu           import MenuPipeline,        MenuSchema
from src.micro.interactable   import InteractablePipeline, InteractableSchema
from src.micro.transition     import TransitionPipeline,  TransitionSchema
from src.micro.health         import HealthPipeline,       HealthSchema
from src.micro.trigger        import TriggerPipeline,      TriggerSchema
from src.micro.shader         import ShaderPipeline,       ShaderSchema


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dummy_config():
    """Pipeline ctor needs LMStudio config, but tests never call the LLM."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_has_all_known():
    assert set(REGISTRY) == {
        "controller", "camera", "physics", "hud",
        "input", "audio", "ai", "spawner", "saveload", "inventory", "dialogue",
        "menu", "interactable", "transition", "health",
        # geometry pipeline (Blender -> FBX); not an LLM concept/schema micro
        "model",
        # behavior-graph micros
        "statemachine", "trigger", "animator",
        # NPC dialog + Higgs Audio v2 TTS
        "npc_dialog",
        # VFX — particle systems, sequences, screen effects, impact bursts
        "vfx",
        # Lighting — light rig + the RenderSettings environment guard
        "lighting",
        # Shader — runtime material-property controllers (MaterialPropertyBlock)
        "shader",
        # Mission objective tracker (schema.missions)
        "objectives",
        # Minimal behavior-tree interpreter (schema.behavior_trees)
        "behavior_tree",
        # Rules, progression and player instructions — the briefing, the step
        # panel and the next-action hint bar. Composed once per PROJECT from
        # the mission's objectives, not once per subsystem: every other micro
        # is a mechanism, and this is the layer that tells a human what to do
        # with them.
        "rules",
        # The domain model and the legal-move logic behind what `rules` tells
        # the player to do: where every piece is, whether the move it is being
        # dragged into is legal, and the command parser that reaches the same
        # move core. UI-first games only — an embodied game's verbs are the
        # controller's.
        "board",
        # Moving/oscillating/falling platforms — the runtime-scripted half of
        # platforming; static step-up platforms need no script (just correct
        # entity placement), so this only covers platforms that move.
        "platform",
        # Mountable 2D bike — a standalone entity (not the 3D-only Control
        # Surface Stack's multi_mode) the player mounts/dismounts; while
        # ridden it drives itself and the player's own controller stands
        # down via Controller.cs.j2's SetMovementLocked hook.
        "bike",
        # 2D sprite frame animation — generates character frames via a local
        # ComfyUI backend (src/sprites/) and wires the runtime frame-swap
        # component; see docs discussion in the 2D-brawler plan.
        "sprite_animator",
    }


@pytest.mark.parametrize("kind,cls", list(REGISTRY.items()))
def test_pipeline_metadata_set(kind, cls):
    assert cls.kind == kind
    assert cls.variants, f"{kind}: no variants declared"
    assert cls.default_variant in cls.variants
    # The geometry pipeline (model) maps prompt -> params, not the LLM
    # concept/schema two-step, so it has no concept_prompt/schema_prompt contract.
    if kind == "model":
        return
    assert "{user_prompt}" in cls.concept_prompt
    assert "{variant}"     in cls.concept_prompt
    assert "{theme_block}" in cls.concept_prompt
    assert "{concept}"      in cls.schema_prompt
    assert "{schema_hints}" in cls.schema_prompt


# ── Schema validation ───────────────────────────────────────────────────────

def test_controller_schema_clamps_unknown_variant():
    s = ControllerSchema(name="X", variant="bogus")
    assert s.variant == "third_person"


def test_controller_schema_clamps_abilities():
    s = ControllerSchema(name="X", abilities=["jump", "bogus", "DASH", "jump"])
    assert s.abilities == ["jump", "dash"]


def test_camera_schema_default_variant_clamped():
    s = CameraRigSchema(name="C", variant="not-real")
    assert s.variant == "follow"


def test_physics_schema_clamp():
    s = PhysicsSchema(name="P", variant="weird")
    assert s.variant == "rigidbody_3d"


def test_hud_schema_clamp():
    s = HUDSchema(name="H", variant="not-a-thing")
    assert s.variant == "health_bar"


def test_hud_schema_clamps_echoed_option_menu():
    """Regression: some models (e.g. deepseek-coder) copy the prompt's option
    menu verbatim into the JSON instead of picking one value. The backend /
    anchor / binding fields must clamp these to a valid literal rather than
    raising a ValidationError."""
    s = HUDSchema(
        name="H",
        variant="full_hud_bundle",
        backend="uGUI | UIToolkit",
        anchor="TL | TR | BL | BR | center",
        binding="events | find",
    )
    assert s.backend == "uGUI"
    assert s.anchor == "TL"
    assert s.binding == "events"


def test_hud_schema_enum_case_insensitive():
    s = HUDSchema(name="H", backend="uitoolkit", binding="FIND", anchor="br")
    assert s.backend == "UIToolkit"
    assert s.binding == "find"
    assert s.anchor == "BR"


def test_hud_schema_enum_garbage_falls_back_to_default():
    s = HUDSchema(name="H", backend="???", anchor="somewhere", binding="maybe")
    assert s.backend == "uGUI"
    assert s.anchor == "TL"
    assert s.binding == "events"


# ── Fixers ───────────────────────────────────────────────────────────────────

def test_controller_fixer_strips_glide_in_2d(dummy_config):
    p = ControllerPipeline(dummy_config)
    s = ControllerSchema(name="X", variant="2d_platformer", abilities=["jump", "glide"])
    s = p.default_fix(s, "2d_platformer")
    assert "glide" not in s.abilities


def test_controller_fixer_clears_abilities_for_vehicle(dummy_config):
    p = ControllerPipeline(dummy_config)
    s = ControllerSchema(name="X", variant="vehicle_car", abilities=["jump", "sprint"])
    s = p.default_fix(s, "vehicle_car")
    assert s.abilities == []


def test_camera_fixer_zeros_first_person_offset(dummy_config):
    p = CameraRigPipeline(dummy_config)
    s = CameraRigSchema(name="C", variant="first_person", offset_x=3, offset_y=3, offset_z=-6)
    s = p.default_fix(s, "first_person")
    assert (s.offset_x, s.offset_y, s.offset_z) == (0.0, 0.0, 0.0)


def test_physics_fixer_trigger_zone_forces_trigger(dummy_config):
    p = PhysicsPipeline(dummy_config)
    s = PhysicsSchema(name="P", variant="trigger_zone", is_trigger=False)
    s = p.default_fix(s, "trigger_zone")
    assert s.is_trigger is True


def test_hud_fixer_centers_crosshair(dummy_config):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="H", variant="crosshair", anchor="TL")
    s = p.default_fix(s, "crosshair")
    assert s.anchor == "center"


# ── End-to-end write_files (bypasses LLM) ───────────────────────────────────

@pytest.mark.parametrize("variant", ControllerPipeline.variants)
def test_controller_renders_every_variant(tmp_path, dummy_config, variant):
    p = ControllerPipeline(dummy_config)
    s = ControllerSchema(name="TestCtl", variant=variant,
                         abilities=["jump", "sprint"] if not variant.startswith("vehicle_") else [])
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count >= 2
    cs = (tmp_path / "Scripts" / "TestCtl.cs").read_text(encoding="utf-8")
    assert "public class TestCtl" in cs
    assert f"Variant: {variant}" in cs


@pytest.mark.parametrize("variant", ["swimming", "flying"])
def test_aquatic_aerial_controllers_drop_ground_check(tmp_path, dummy_config, variant):
    """Fix #5: swimming/flying have no ground, so they must NOT emit the walker
    ground-check scaffolding (groundRayLen / CheckGrounded3D / IsGrounded)."""
    p = ControllerPipeline(dummy_config)
    s = p.default_fix(ControllerSchema(name="Aqua", variant=variant), variant)
    cs = (lambda: (p.write_files(s, variant, tmp_path),
                   (tmp_path / "Scripts" / "Aqua.cs").read_text(encoding="utf-8"))[1])()
    assert "IsGrounded" not in cs
    assert "CheckGrounded3D" not in cs
    assert "groundRayLen" not in cs
    assert "_grounded" not in cs
    assert cs.count("{") == cs.count("}")


def test_grounded_controllers_keep_ground_check(tmp_path, dummy_config):
    p = ControllerPipeline(dummy_config)
    s = p.default_fix(ControllerSchema(name="Walker", variant="third_person",
                                       abilities=["jump", "sprint"]), "third_person")
    p.write_files(s, "third_person", tmp_path)
    cs = (tmp_path / "Scripts" / "Walker.cs").read_text(encoding="utf-8")
    assert "IsGrounded" in cs and "CheckGrounded3D" in cs


# ── 2.5D: distinct from both 2D and 3D ──────────────────────────────────────

def test_2_5d_controller_uses_character_controller_not_rigidbody2d(tmp_path, dummy_config):
    """2.5D is a real 3D CharacterController (like third_person), not Rigidbody2D
    (like 2d_platformer) — the world/models are real 3D even though input is
    locked to a lateral plane."""
    p = ControllerPipeline(dummy_config)
    s = p.default_fix(ControllerSchema(name="Side25D", variant="platformer_2_5d",
                                       abilities=["jump", "sprint"]), "platformer_2_5d")
    p.write_files(s, "platformer_2_5d", tmp_path)
    cs = (tmp_path / "Scripts" / "Side25D.cs").read_text(encoding="utf-8")
    assert "CharacterController _cc" in cs
    assert "Rigidbody2D" not in cs
    assert "CheckGrounded3D" in cs
    # Lateral-only movement: h drives X, Z is explicitly zeroed (locked depth).
    assert "_velocity.x = h * speed;" in cs
    assert "_velocity.z = 0f;" in cs
    assert cs.count("{") == cs.count("}")


def test_2_5d_camera_is_perspective_not_orthographic(tmp_path, dummy_config):
    """follow_2_5d must NOT be orthographic (that's follow_2d/2D) — 2.5D renders
    a real 3D world from a tilted/elevated perspective follow camera."""
    p = CameraRigPipeline(dummy_config)
    s = p.default_fix(CameraRigSchema(name="Cam25D", variant="follow_2_5d"), "follow_2_5d")
    p.write_files(s, "follow_2_5d", tmp_path)
    cs = (tmp_path / "Scripts" / "Cam25D.cs").read_text(encoding="utf-8")
    assert "_cam.orthographic" not in cs
    assert "fovBase" in cs
    assert "LookAt" in cs


@pytest.mark.parametrize("variant", ["humanoid", "vehicle", "creature", "object", "ui"])
def test_animator_declares_backing_fields_and_setters(tmp_path, dummy_config, variant):
    """Regression: default animator params must declare a backing field + public
    Set<Param>() setter (so SetGrounded etc. resolve), with no undeclared _rb/_cc."""
    from src.micro import AnimatorPipeline, AnimatorSchema
    p = AnimatorPipeline(dummy_config)
    s = p.default_fix(AnimatorSchema(name="Anim", variant=variant), variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "Anim.cs").read_text(encoding="utf-8")
    assert "_rb" not in cs and "_cc" not in cs
    for prm in s.params:
        if prm.param_type != "trigger" and not prm.driven_by:
            assert f"_{prm.name.lower()}" in cs and f"Set{prm.name}(" in cs
    # Wiring contract: GameWiring's grounded bridge calls animator.SetGrounded for
    # EVERY variant (CS1061 hit live on the vehicle animator in GravityDrift).
    assert cs.count("public void SetGrounded(") == 1
    assert cs.count("{") == cs.count("}")


@pytest.mark.parametrize("variant,fields", [
    ("enemy_fsm",  ["attackRange", "_distToPlayer", "_seesPlayer", "_heardNoise", "_alertTimer", "_lostTimer"]),
    ("game_fsm",   ["_startPressed", "_pausePressed", "_playerDead", "_restartPressed"]),
    ("object_fsm", ["_triggered", "_activeDone", "_cooldownDone", "_hp"]),
])
def test_statemachine_declares_default_condition_fields(tmp_path, dummy_config, variant, fields):
    """Regression: default FSM transition conditions reference fields that must be
    declared, or the generated FSM is CS0103."""
    from src.micro import StateMachinePipeline, StateMachineSchema
    p = StateMachinePipeline(dummy_config)
    s = p.default_fix(StateMachineSchema(name="Fsm", variant=variant), variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "Fsm.cs").read_text(encoding="utf-8")
    for f in fields:
        assert f in cs, f"{variant} missing condition field {f}"
    assert cs.count("{") == cs.count("}")


@pytest.mark.parametrize("variant", CameraRigPipeline.variants)
def test_camera_renders_every_variant(tmp_path, dummy_config, variant):
    p = CameraRigPipeline(dummy_config)
    s = CameraRigSchema(name="TestCam", variant=variant)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestCam.cs").read_text(encoding="utf-8")
    assert "public class TestCam" in cs


@pytest.mark.parametrize("variant", PhysicsPipeline.variants)
def test_physics_renders_every_variant(tmp_path, dummy_config, variant):
    p = PhysicsPipeline(dummy_config)
    s = PhysicsSchema(name="TestBody", variant=variant, impact_events=True)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestBody.cs").read_text(encoding="utf-8")
    assert "public class TestBody" in cs
    # impact_events → dispatcher must exist
    assert (tmp_path / "Scripts" / "ImpactDispatcher.cs").exists()


@pytest.mark.parametrize("variant", HUDPipeline.variants)
def test_hud_renders_every_variant_ugui(tmp_path, dummy_config, variant):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="TestHud", variant=variant, backend="uGUI")
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestHud.cs").read_text(encoding="utf-8")
    assert "public class TestHud" in cs


def test_hud_racing_variant_emits_race_gauges(tmp_path, dummy_config):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="GameHud", variant="racing_hud", backend="uGUI")
    s = p.default_fix(s, "racing_hud")
    p.write_files(s, "racing_hud", tmp_path)
    cs = (tmp_path / "Scripts" / "GameHud.cs").read_text(encoding="utf-8")
    # The six race gauges + countdown banner API the prompt outline asked for.
    for api in ("SetSpeed", "SetLap", "SetPosition", "SetCoins", "SetBoost",
                "ShowCountdown", "HideCountdown"):
        assert f"public void {api}(" in cs, api
    # Ordinal helper backs SetPosition ("1st/4", "2nd/4", ...).
    assert "private static string Ordinal(" in cs
    # No health-bar leftover — this variant replaces it, not augments it.
    assert "SetHealth(" not in cs


def test_hud_racing_variant_not_clamped(tmp_path, dummy_config):
    # racing_hud must survive the variant validator (unknown variants clamp to
    # health_bar — this is what makes the unity_compose genre branch reachable).
    s = HUDSchema(name="GameHud", variant="racing_hud")
    assert s.variant == "racing_hud"


def test_hud_full_bundle_variant_emits_all_gauges_ugui(tmp_path, dummy_config):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="GameHud", variant="full_hud_bundle", backend="uGUI")
    s = p.default_fix(s, "full_hud_bundle")
    p.write_files(s, "full_hud_bundle", tmp_path)
    cs = (tmp_path / "Scripts" / "GameHud.cs").read_text(encoding="utf-8")
    for api in ("SetHealth", "SetAmmo", "SetScore"):
        assert f"public void {api}(" in cs, api
    for field in ("healthBar", "ammoText", "scoreText", "mapImage", "mapCamera"):
        assert field in cs, field


def test_hud_full_bundle_variant_emits_all_gauges_uitoolkit(tmp_path, dummy_config):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="GameHud", variant="full_hud_bundle", backend="UIToolkit")
    s = p.default_fix(s, "full_hud_bundle")
    p.write_files(s, "full_hud_bundle", tmp_path)
    cs = (tmp_path / "Scripts" / "GameHud.cs").read_text(encoding="utf-8")
    for api in ("SetHealth", "SetAmmo", "SetScore"):
        assert f"public void {api}(" in cs, api
    uxml = (tmp_path / "UI" / "GameHud.uxml").read_text(encoding="utf-8")
    for name in ('name="HealthBar"', 'name="Ammo"', 'name="Score"', 'name="Map"'):
        assert name in uxml, name


@pytest.mark.parametrize("variant", HUDPipeline.variants)
def test_hud_uitoolkit_emits_uxml_and_uss(tmp_path, dummy_config, variant):
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="TestUTK", variant=variant, backend="UIToolkit")
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    assert (tmp_path / "UI" / "TestUTK.uxml").exists()
    assert (tmp_path / "UI" / "TestUTK.uss").exists()


# ── Tier 2 — unity-input ────────────────────────────────────────────────────

def test_input_schema_clamps_variant():
    s = InputSchema(name="X", variant="bogus")
    assert s.variant == "keyboard_mouse"


def test_input_fixer_injects_default_layout_when_empty(dummy_config):
    p = InputPipeline(dummy_config)
    s = InputSchema(name="X", variant="racing_wheel", maps=[])
    s = p.default_fix(s, "racing_wheel")
    assert s.maps, "fixer should inject default racing-wheel layout"
    assert any(a.name == "Steer" for a in s.maps[0].actions)


@pytest.mark.parametrize("variant", InputPipeline.variants)
def test_input_renders_every_variant(tmp_path, dummy_config, variant):
    p = InputPipeline(dummy_config)
    s = InputSchema(name="TestInput", variant=variant, maps=[])
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count >= 3   # .inputactions + InputReader + README (+ Bindings)
    assert (tmp_path / "Input" / "TestInput.inputactions").exists()
    assert (tmp_path / "Scripts" / "TestInputInputReader.cs").exists()
    reader = (tmp_path / "Scripts" / "TestInputInputReader.cs").read_text(encoding="utf-8")
    assert "[CreateAssetMenu" in reader
    assert "public class TestInputInputReader" in reader
    # CS0535 regression: EVERY variant must satisfy the IInputReader contract,
    # not just keyboard_mouse. The class claims `: IInputReader`, so it must
    # declare all four canonical members or Unity won't compile.
    assert ": ScriptableObject, IInputReader" in reader
    for member in ("OnMove", "OnJump", "OnSprint", "OnSprintReleased"):
        assert f" {member};" in reader, f"{variant} reader missing {member}"
    # No duplicate event declarations (canonical vs variant-specific collision).
    assert reader.count("public event Action<Vector2> OnMove;") == 1
    assert reader.count("public event Action          OnJump;") == 1


def test_input_typed_wrapper_optional(tmp_path, dummy_config):
    p = InputPipeline(dummy_config)
    s = InputSchema(name="NoWrap", variant="gamepad", maps=[], use_typed_wrapper=False)
    s = p.default_fix(s, "gamepad")
    p.write_files(s, "gamepad", tmp_path)
    assert not (tmp_path / "Scripts" / "NoWrapBindings.cs").exists()


# ── Tier 2 — unity-audio ────────────────────────────────────────────────────

def test_audio_schema_clamp():
    s = AudioSchema(name="X", variant="weird")
    assert s.variant == "categorical_mixer"


def test_audio_fixer_forces_music_for_music_layers(dummy_config):
    p = AudioPipeline(dummy_config)
    s = AudioSchema(name="X", variant="music_layers", categories=["sfx"])
    s = p.default_fix(s, "music_layers")
    assert "music" in s.categories


@pytest.mark.parametrize("variant", AudioPipeline.variants)
def test_audio_renders_every_variant(tmp_path, dummy_config, variant):
    p = AudioPipeline(dummy_config)
    s = AudioSchema(name="TestAudio", variant=variant)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    assert (tmp_path / "Scripts" / "AudioCue.cs").exists()
    assert (tmp_path / "Scripts" / "TestAudio.cs").exists()
    assert (tmp_path / "Scripts" / "TestAudioAudioManager.cs").exists()
    bank = (tmp_path / "Scripts" / "TestAudio.cs").read_text(encoding="utf-8")
    assert "[CreateAssetMenu" in bank


def test_audio_music_layers_emits_extra_file(tmp_path, dummy_config):
    p = AudioPipeline(dummy_config)
    s = AudioSchema(name="Stems", variant="music_layers")
    s = p.default_fix(s, "music_layers")
    p.write_files(s, "music_layers", tmp_path)
    assert (tmp_path / "Scripts" / "StemsMusicLayers.cs").exists()


def test_audio_crossfade_zone_emits_extra_file(tmp_path, dummy_config):
    p = AudioPipeline(dummy_config)
    s = AudioSchema(name="Zones", variant="crossfade_zones")
    s = p.default_fix(s, "crossfade_zones")
    p.write_files(s, "crossfade_zones", tmp_path)
    assert (tmp_path / "Scripts" / "ZonesCrossfadeZone.cs").exists()


def test_audio_coin_arcade_pack_12_sounds(tmp_path, dummy_config):
    """12-cue sound pack for a top-down coin-collecting arcade game.

    Verifies: all 12 explicit cue IDs survive the fixer's dedup pass,
    game_type resolves to 'generic' (no arcade keyword), the bank C# is
    emitted, and no clip-generation errors bubble up as hard failures.
    """
    from src.micro.audio import AudioCueSpec

    PACK = [
        AudioCueSpec(id="coin_collect",   category="sfx",     descriptor="collect a gold coin ding",          duration_s=0.3),
        AudioCueSpec(id="big_coin",       category="sfx",     descriptor="large treasure coin sparkle chime", duration_s=0.5),
        AudioCueSpec(id="coin_combo",     category="sfx",     descriptor="rapid coin streak combo jingle",    duration_s=0.6),
        AudioCueSpec(id="powerup",        category="sfx",     descriptor="power up upgrade blip",             duration_s=0.4),
        AudioCueSpec(id="player_hit",     category="sfx",     descriptor="player take damage thud",           duration_s=0.3),
        AudioCueSpec(id="player_death",   category="sfx",     descriptor="player death game over sting",      duration_s=1.2),
        AudioCueSpec(id="enemy_die",      category="sfx",     descriptor="enemy defeated pop burst",          duration_s=0.4),
        AudioCueSpec(id="level_complete", category="sfx",     descriptor="level clear fanfare win jingle",    duration_s=1.5),
        AudioCueSpec(id="game_over",      category="sfx",     descriptor="game over dramatic sting",          duration_s=1.0),
        AudioCueSpec(id="ui_click",       category="sfx",     descriptor="menu button click blip",            duration_s=0.15),
        AudioCueSpec(id="footstep",       category="sfx",     descriptor="light footstep tap",                duration_s=0.2),
        AudioCueSpec(id="music_loop",     category="music",   descriptor="upbeat retro arcade loop",          loop=True, duration_s=15.0),
    ]

    p = AudioPipeline({**dummy_config, "audio": {"generate_clips": False}})
    s = AudioSchema(
        name="CoinArcadeBank",
        variant="categorical_mixer",
        game_type="generic",
        categories=["music", "sfx"],
        bank_clip_count_hint=12,
        description="Top-down coin-collecting arcade sound pack",
        cues=PACK,
    )
    s = p.default_fix(s, "categorical_mixer")

    # All 12 explicit IDs must survive the dedup pass
    cue_ids = {c.id for c in s.cues}
    for expected in (
        "coin_collect", "big_coin", "coin_combo", "powerup",
        "player_hit", "player_death", "enemy_die", "level_complete",
        "game_over", "ui_click", "footstep", "music_loop",
    ):
        assert expected in cue_ids, f"cue '{expected}' lost after default_fix"

    assert len(s.cues) >= 12              # fixer may inject extra generic cues
    assert s.game_type == "generic"       # no arcade keyword → generic
    assert "music" in s.categories
    assert "sfx" in s.categories

    # Write the artifacts — must not raise
    p._with_clips = False
    count = p.write_files(s, "categorical_mixer", tmp_path)

    assert count >= 3
    bank_cs = (tmp_path / "Scripts" / "CoinArcadeBank.cs").read_text(encoding="utf-8")
    assert "[CreateAssetMenu" in bank_cs
    assert "public class CoinArcadeBank" in bank_cs
    assert (tmp_path / "Scripts" / "CoinArcadeBankAudioManager.cs").exists()
    assert (tmp_path / "Scripts" / "AudioCue.cs").exists()


# ── Tier 2 — unity-ai ───────────────────────────────────────────────────────

def test_ai_schema_clamp():
    s = AISchema(name="X", variant="weird")
    assert s.variant == "navmesh_patrol"


def test_ai_fixer_forces_navmesh_for_patrol(dummy_config):
    p = AIPipeline(dummy_config)
    s = AISchema(name="X", variant="navmesh_patrol", nav_backend="transform_step")
    s = p.default_fix(s, "navmesh_patrol")
    assert s.nav_backend == "navmesh"


def test_ai_fixer_forces_kinematic_for_flying_swarm(dummy_config):
    p = AIPipeline(dummy_config)
    s = AISchema(name="X", variant="flying_swarm", nav_backend="navmesh")
    s = p.default_fix(s, "flying_swarm")
    assert s.nav_backend == "kinematic"


def test_ai_fixer_injects_default_states_for_fsm(dummy_config):
    p = AIPipeline(dummy_config)
    s = AISchema(name="X", variant="fsm_guard", states=[])
    s = p.default_fix(s, "fsm_guard")
    assert len(s.states) >= 3
    names = {st.name for st in s.states}
    assert {"Idle", "Chase", "Attack"}.issubset(names)


@pytest.mark.parametrize("variant", [
    "navmesh_patrol", "chase_player", "fsm_guard", "behaviour_tree_lite", "cover_shooter",
])
def test_ai_fixer_forces_kinematic_2d_for_2d_project(dummy_config, variant):
    """A composed 2D project has no baked NavMesh — unity_compose.py threads
    game_type into schema.meta the same way it does for trigger/interactable,
    so these variants must fall back to direct transform-seeking instead of
    requiring a NavMeshAgent that will never move on a flat scene."""
    p = AIPipeline(dummy_config)
    s = AISchema(name="X", variant=variant, meta={"game_type": "2D"})
    s = p.default_fix(s, variant)
    assert s.nav_backend == "kinematic_2d"


def test_ai_2d_render_has_no_navmesh_agent(tmp_path, dummy_config):
    p = AIPipeline(dummy_config)
    s = AISchema(name="TestAI2D", variant="navmesh_patrol", meta={"game_type": "2D"})
    s = p.default_fix(s, "navmesh_patrol")
    p.write_files(s, "navmesh_patrol", tmp_path)
    cs = (tmp_path / "Scripts" / "TestAI2D.cs").read_text(encoding="utf-8")
    assert "NavMeshAgent" not in cs
    assert "using UnityEngine.AI;" not in cs
    assert "private void MoveTowards(" in cs
    assert "private void PatrolStep2D()" in cs


@pytest.mark.parametrize("variant", AIPipeline.variants)
def test_ai_renders_every_variant(tmp_path, dummy_config, variant):
    p = AIPipeline(dummy_config)
    s = AISchema(name="TestAI", variant=variant)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestAI.cs").read_text(encoding="utf-8")
    assert "public class TestAI" in cs
    # FSM/BT variants get a state enum file
    if variant in ("fsm_guard", "behaviour_tree_lite"):
        assert (tmp_path / "Scripts" / "TestAIState.cs").exists()
    # NavMesh variants must reference UnityEngine.AI
    if variant in ("navmesh_patrol", "chase_player", "fsm_guard",
                   "behaviour_tree_lite", "cover_shooter"):
        assert "using UnityEngine.AI;" in cs


# ── Tier 2 — unity-spawner ──────────────────────────────────────────────────

def test_spawner_schema_clamp():
    s = SpawnerSchema(name="X", variant="weird")
    assert s.variant == "wave_spawner"


def test_spawner_schema_clamps_echoed_option_menu():
    """Regression: loose models (deepseek-coder) echo the prompt's option menu
    verbatim instead of picking one value. The shared clamp_enum helper must
    coerce these to a valid literal rather than raising ValidationError."""
    s = SpawnerSchema(
        name="X",
        variant="wave_spawner",
        spawn_shape="point | circle | box | spawn_points",
        pooling="pooled | instantiate",
        trigger="timer | on_enter | manual | wave",
    )
    assert s.spawn_shape == "point"
    assert s.pooling == "pooled"
    assert s.trigger == "timer"


def test_spawner_fixer_object_pool_forces_pooling(dummy_config):
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(name="X", variant="object_pool", pooling="instantiate")
    s = p.default_fix(s, "object_pool")
    assert s.pooling == "pooled"


def test_spawner_fixer_wave_forces_wave_trigger(dummy_config):
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(name="X", variant="wave_spawner", trigger="timer", wave_count=0)
    s = p.default_fix(s, "wave_spawner")
    assert s.trigger == "wave"
    assert s.wave_count >= 1


@pytest.mark.parametrize("variant", SpawnerPipeline.variants)
def test_spawner_renders_every_variant(tmp_path, dummy_config, variant):
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(name="TestSpawn", variant=variant)
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestSpawn.cs").read_text(encoding="utf-8")
    assert "public class TestSpawn" in cs
    assert f"Variant: {variant}" in cs
    # Pooled variants (object_pool is forced pooled) emit the companion pool.
    if s.pooling == "pooled":
        assert (tmp_path / "Scripts" / "TestSpawnPool.cs").exists()
        assert count >= 3
    else:
        assert count >= 2


def test_spawner_pooled_emits_pool_file(tmp_path, dummy_config):
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(name="Pooled", variant="continuous_spawner", pooling="pooled")
    s = p.default_fix(s, "continuous_spawner")
    p.write_files(s, "continuous_spawner", tmp_path)
    pool = (tmp_path / "Scripts" / "PooledPool.cs").read_text(encoding="utf-8")
    assert "public class PooledPool" in pool


def test_spawner_invented_override_hook_is_stripped_not_left_illegal(dummy_config):
    # Regression (live Unity, DeadReckoningProtocol run): the model authored
    # `public override void OnSpawned()` on EnemySpawner — a plausible-sounding
    # hook name, but not a real Unity message and not declared virtual by any
    # spawner template, so there is no base method to override (CS0115,
    # EnemySpawner.cs(129)). default_fix must strip the illegal modifier so
    # the method still compiles, rather than shipping a guaranteed CS0115.
    from src.micro.code_fields import CodeMethod
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(
        name="EnemySpawner", variant="wave_spawner",
        extra_methods=[CodeMethod(
            signature="public override void OnSpawned()",
            body="int x = 1;")],
    )
    s = p.default_fix(s, "wave_spawner")
    assert len(s.extra_methods) == 1
    assert "override" not in s.extra_methods[0].signature
    assert "OnSpawned" in s.extra_methods[0].signature


# ── Schema-fill retry (model robustness) ────────────────────────────────────

class _FlakyLLM:
    """Fake LLM: concept always works; schema fill fails `fail_n` times then OK."""
    def __init__(self, fail_n: int, good_json: str):
        self.fail_n = fail_n
        self.good_json = good_json
        self.schema_calls = 0

    def chat(self, system, user, temperature, thinking):
        if thinking:                      # concept step
            return "a concept brief"
        self.schema_calls += 1            # schema step
        if self.schema_calls <= self.fail_n:
            raise ValueError("No JSON object in LLM response")
        return self.good_json

    def extract_json(self, raw):
        import json
        return json.loads(raw)


def test_schema_fill_retries_then_succeeds(tmp_path, dummy_config):
    p = HUDPipeline(dummy_config)
    p.llm = _FlakyLLM(fail_n=1, good_json='{"name": "Hp", "variant": "health_bar"}')
    out = p.run("a health bar", variant="health_bar", out=str(tmp_path))
    assert (Path(out) / "Scripts" / "Hp.cs").exists()
    assert p.llm.schema_calls == 2        # failed once, succeeded on retry


def test_schema_fill_raises_after_exhausting_attempts(tmp_path, dummy_config):
    p = HUDPipeline(dummy_config)
    p.llm = _FlakyLLM(fail_n=99, good_json='{"name": "Hp", "variant": "health_bar"}')
    with pytest.raises(RuntimeError, match="schema fill failed"):
        p.run("a health bar", variant="health_bar", out=str(tmp_path))
    assert p.llm.schema_calls == HUDPipeline.SCHEMA_FILL_ATTEMPTS


# ── Tier 2 — unity-saveload ─────────────────────────────────────────────────

def test_saveload_schema_clamp():
    s = SaveLoadSchema(name="X", variant="weird")
    assert s.variant == "json_file"


def test_saveload_schema_clamps_echoed_option_menu():
    """Regression: loose models echo the option menu verbatim; clamp_enum must
    coerce enum fields rather than raising ValidationError."""
    s = SaveLoadSchema(
        name="X",
        storage="file | playerprefs",
        slot_naming="numbered | named",
    )
    assert s.storage == "file"
    assert s.slot_naming == "numbered"


def test_saveload_fixer_playerprefs_forces_storage(dummy_config):
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(name="X", variant="playerprefs_kv", storage="file")
    s = p.default_fix(s, "playerprefs_kv")
    assert s.storage == "playerprefs"


def test_saveload_fixer_injects_default_data_fields(dummy_config):
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(name="X", variant="json_file", data_fields=[])
    s = p.default_fix(s, "json_file")
    assert s.data_fields, "fixer should inject a default save payload"


def test_saveload_fixer_checkpoint_adds_position(dummy_config):
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(name="X", variant="checkpoint", data_fields=[])
    s = p.default_fix(s, "checkpoint")
    assert any(f.name == "checkpointPosition" for f in s.data_fields)


def test_saveload_auto_declares_data_fields_referenced_by_extra_code(dummy_config):
    # Regression (live Unity, RepoFlowSim run): an extra_method read
    # Data.CommitHistory/Data.BranchStatus while data_fields never declared
    # them (two separate Step 3/3.5 outputs, no cross-check) — CS1061 x2 on
    # SaveSystemData. default_fix must notice the reference and declare it.
    from src.micro.code_fields import CodeMethod
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(
        name="X", variant="json_file", data_fields=[],
        extra_methods=[CodeMethod(
            signature="public bool HasActiveWorkflowData()",
            body="return !string.IsNullOrEmpty(Data?.CommitHistory) "
                 "&& Data?.BranchStatus != null;")],
    )
    s = p.default_fix(s, "json_file")
    names = {f.name: f.type for f in s.data_fields}
    assert names.get("CommitHistory") == "string"
    assert names.get("BranchStatus") == "string"

    # A Data.Method(...) call is not a missing field — must not get stubbed.
    s2 = SaveLoadSchema(
        name="Y", variant="json_file", data_fields=[],
        awake_extra="Debug.Log(Data.ToString());",
    )
    s2 = p.default_fix(s2, "json_file")
    assert "ToString" not in {f.name for f in s2.data_fields}


def test_saveload_strips_invented_data_method_call(dummy_config):
    # Live shape: SaveData.cs.j2 emits Data as a plain [Serializable] POCO —
    # fields only, never a method — but the LLM sometimes treats it like
    # PlayerPrefs and calls Data.GetFloat("volume"): CS1061, since GetFloat
    # is not a real System.Object member and Data has no methods of its own.
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(
        name="X", variant="json_file", data_fields=[],
        awake_extra='float v = Data.GetFloat("volume");',
    )
    s = p.default_fix(s, "json_file")
    assert "GetFloat" not in s.awake_extra.split("// AUDIT:")[0]
    assert "GetFloat" not in {f.name for f in s.data_fields}


def test_saveload_real_object_method_on_data_survives(dummy_config):
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(
        name="X", variant="json_file", data_fields=[],
        awake_extra="Debug.Log(Data.Equals(null));",
    )
    s = p.default_fix(s, "json_file")
    assert "Data.Equals(null)" in s.awake_extra


@pytest.mark.parametrize("variant", SaveLoadPipeline.variants)
def test_saveload_renders_every_variant(tmp_path, dummy_config, variant):
    p = SaveLoadPipeline(dummy_config)
    s = SaveLoadSchema(name="TestSave", variant=variant)
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count == 3
    mgr = (tmp_path / "Scripts" / "TestSave.cs").read_text(encoding="utf-8")
    data = (tmp_path / "Scripts" / "TestSaveData.cs").read_text(encoding="utf-8")
    assert "public class TestSave" in mgr
    assert "public class TestSaveData" in data
    # save_slots exposes slot-parameterized API; others a parameterless Save().
    if variant == "save_slots":
        assert "Save(int slot)" in mgr
    else:
        assert "public void Save()" in mgr


# ── Tier 2 — unity-inventory ────────────────────────────────────────────────

def test_inventory_schema_clamp():
    s = InventorySchema(name="X", variant="weird")
    assert s.variant == "slot_based"


def test_inventory_schema_clamps_echoed_option_menu():
    s = InventorySchema(
        name="X",
        stacking="stackable | unique",
        sort_mode="none | by_name | by_weight",
    )
    assert s.stacking == "stackable"
    assert s.sort_mode == "none"


def test_inventory_fixer_equipment_forces_unique_and_slots(dummy_config):
    p = InventoryPipeline(dummy_config)
    s = InventorySchema(name="X", variant="equipment", stacking="stackable", equipment_slots=[])
    s = p.default_fix(s, "equipment")
    assert s.stacking == "unique"
    assert s.equipment_slots, "fixer should inject default equipment slots"


def test_inventory_fixer_grid_syncs_slot_count(dummy_config):
    p = InventoryPipeline(dummy_config)
    s = InventorySchema(name="X", variant="grid", grid_width=6, grid_height=3)
    s = p.default_fix(s, "grid")
    assert s.slot_count == 18


@pytest.mark.parametrize("variant", InventoryPipeline.variants)
def test_inventory_renders_every_variant(tmp_path, dummy_config, variant):
    p = InventoryPipeline(dummy_config)
    s = InventorySchema(name="TestInv", variant=variant)
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count == 3
    mgr = (tmp_path / "Scripts" / "TestInv.cs").read_text(encoding="utf-8")
    item = (tmp_path / "Scripts" / "TestInvItemDefinition.cs").read_text(encoding="utf-8")
    assert "public class TestInv" in mgr
    assert "public class TestInvItemDefinition" in item
    # equipment exposes Equip(); the container variants expose Add().
    if variant == "equipment":
        assert "public bool Equip(" in mgr
    else:
        assert "public bool Add(" in mgr
    if variant == "hotbar":
        assert "SelectedIndex" in mgr


# ── Tier 2 — unity-dialogue ─────────────────────────────────────────────────

def test_dialogue_schema_clamp():
    s = DialogueSchema(name="X", variant="weird")
    assert s.variant == "linear"


def test_dialogue_schema_clamps_echoed_option_menu():
    s = DialogueSchema(
        name="X",
        advance="submit | any_key | auto",
        text_render="instant | typewriter",
    )
    assert s.advance == "submit"
    assert s.text_render == "instant"


def test_dialogue_fixer_typewriter_and_cutscene_coherence(dummy_config):
    p = DialoguePipeline(dummy_config)
    s = p.default_fix(DialogueSchema(name="X", variant="typewriter"), "typewriter")
    assert s.text_render == "typewriter"
    s2 = p.default_fix(DialogueSchema(name="Y", variant="cutscene"), "cutscene")
    assert s2.advance == "auto"


def test_dialogue_fixer_injects_nodes_and_fixes_start(dummy_config):
    p = DialoguePipeline(dummy_config)
    s = p.default_fix(DialogueSchema(name="X", variant="branching", nodes=[], start_node="ghost"), "branching")
    assert s.nodes, "fixer should inject a default conversation"
    assert s.start_node in {n.id for n in s.nodes}, "start_node must point at a real node"


def test_dialogue_escapes_quotes_in_baked_text(tmp_path, dummy_config):
    """Node text with quotes must become a valid C# string literal (tojson)."""
    p = DialoguePipeline(dummy_config)
    s = DialogueSchema(name="Talk", variant="linear",
                       nodes=[DialogueNode(id="l1", speaker="Guard",
                                           text='He said "halt" loudly')])
    s = p.default_fix(s, "linear")
    p.write_files(s, "linear", tmp_path)
    runner = (tmp_path / "Scripts" / "Talk.cs").read_text(encoding="utf-8")
    assert r'\"halt\"' in runner          # quotes escaped, not raw


def test_dialogue_resolve_override_is_preserved(dummy_config):
    # Dialogue.cs.j2 declares `protected virtual string Resolve(string text)`
    # for localization — the one legitimate override target in this
    # generator's template (see DialoguePipeline._KNOWN_VIRTUAL_METHODS). Must
    # survive default_fix unlike an invented hook name — see
    # test_spawner_invented_override_hook_is_stripped_not_left_illegal.
    from src.micro.code_fields import CodeMethod
    p = DialoguePipeline(dummy_config)
    s = DialogueSchema(
        name="TownDialogue", variant="linear",
        extra_methods=[CodeMethod(
            signature="protected override string Resolve(string text)",
            body="return text.ToUpperInvariant();")],
    )
    s = p.default_fix(s, "linear")
    assert len(s.extra_methods) == 1
    assert "override" in s.extra_methods[0].signature


@pytest.mark.parametrize("variant", DialoguePipeline.variants)
def test_dialogue_renders_every_variant(tmp_path, dummy_config, variant):
    p = DialoguePipeline(dummy_config)
    s = DialogueSchema(name="TestDlg", variant=variant)
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count == 3
    runner = (tmp_path / "Scripts" / "TestDlg.cs").read_text(encoding="utf-8")
    node   = (tmp_path / "Scripts" / "TestDlgNode.cs").read_text(encoding="utf-8")
    assert "public class TestDlg" in runner
    assert "public class TestDlgNode" in node
    if variant == "branching":
        assert "public void Choose(" in runner
    elif variant == "barks":
        assert "public void PlayBark(" in runner
    else:
        assert "public void Advance(" in runner


# ── Tier 1 finishers — menu / interactable / transition ─────────────────────

def test_menu_schema_clamp():
    assert MenuSchema(name="X", variant="weird").variant == "main_menu"


def test_menu_schema_clamps_echoed_option_menu():
    s = MenuSchema(name="X", backend="uGUI | UIToolkit", navigation="mouse | keyboard | gamepad")
    assert s.backend == "uGUI"
    assert s.navigation == "mouse"


def test_menu_fixer_pause_variants_pause_game(dummy_config):
    p = MenuPipeline(dummy_config)
    assert p.default_fix(MenuSchema(name="X", variant="pause_menu"), "pause_menu").pauses_game is True
    assert p.default_fix(MenuSchema(name="Y", variant="main_menu"), "main_menu").pauses_game is False


def test_menu_fixer_injects_default_items(dummy_config):
    p = MenuPipeline(dummy_config)
    s = p.default_fix(MenuSchema(name="X", variant="main_menu", items=[]), "main_menu")
    assert s.items, "fixer should inject default menu items"


def test_menu_root_reference_stripped_on_ugui_backend(dummy_config):
    # Menu.cs.j2 declares `_root` only under backend == "UIToolkit" — same
    # mutually-exclusive-branches shape as HUD.cs.j2. A uGUI-backend reference
    # is a guaranteed CS0103; only the "// AUDIT:" trail may still mention it.
    p = MenuPipeline(dummy_config)
    s = p.default_fix(MenuSchema(
        name="X", variant="pause_menu", backend="uGUI",
        update_extra="if (_root != null) _root.style.opacity = 1f;"), "pause_menu")
    assert "_root" not in s.update_extra.split("// AUDIT:")[0]


def test_menu_root_reference_survives_on_uitoolkit_backend(dummy_config):
    p = MenuPipeline(dummy_config)
    s = p.default_fix(MenuSchema(
        name="X", variant="pause_menu", backend="UIToolkit",
        update_extra="if (_root != null) _root.style.opacity = 1f;"), "pause_menu")
    assert "_root.style.opacity = 1f" in s.update_extra
    assert "AUDIT" not in s.update_extra


def test_menu_ugui_field_reference_stripped_on_uitoolkit_backend(dummy_config):
    p = MenuPipeline(dummy_config)
    s = p.default_fix(MenuSchema(
        name="X", variant="pause_menu", backend="UIToolkit",
        update_extra="if (panel != null) panel.alpha = 1f;"), "pause_menu")
    assert "panel.alpha" not in s.update_extra.split("// AUDIT:")[0]


@pytest.mark.parametrize("variant", MenuPipeline.variants)
def test_menu_renders_every_variant_both_backends(tmp_path, dummy_config, variant):
    p = MenuPipeline(dummy_config)
    for backend in ("uGUI", "UIToolkit"):
        d = tmp_path / f"{variant}_{backend}"
        s = p.default_fix(MenuSchema(name="TestMenu", variant=variant, backend=backend), variant)
        n = p.write_files(s, variant, d)
        cs = (d / "Scripts" / "TestMenu.cs").read_text(encoding="utf-8")
        assert "public class TestMenu" in cs
        if backend == "UIToolkit":
            assert (d / "UI" / "TestMenu.uxml").exists()
            assert n >= 4
        else:
            assert n >= 2


def test_interactable_schema_clamp():
    assert InteractableSchema(name="X", variant="weird").variant == "pickup"


def test_interactable_schema_clamps_echoed_option_menu():
    s = InteractableSchema(name="X", detection="trigger | raycast", interact_key="E | F | Space")
    assert s.detection == "trigger"
    assert s.interact_key == "E"


def test_interactable_fixer_pickup_is_one_shot(dummy_config):
    p = InteractablePipeline(dummy_config)
    assert p.default_fix(InteractableSchema(name="X", variant="pickup", one_shot=False), "pickup").one_shot is True


@pytest.mark.parametrize("variant", InteractablePipeline.variants)
def test_interactable_renders_every_variant(tmp_path, dummy_config, variant):
    p = InteractablePipeline(dummy_config)
    s = p.default_fix(InteractableSchema(name="TestIt", variant=variant), variant)
    n = p.write_files(s, variant, tmp_path)
    assert n == 2
    cs = (tmp_path / "Scripts" / "TestIt.cs").read_text(encoding="utf-8")
    assert "public class TestIt" in cs
    assert "public void Interact(" in cs
    if variant in ("lever", "button_switch", "door"):
        assert "OnToggled" in cs


# ── 2D-aware trigger detection (Collider2D, not the 3D Physics API) ────────────
# A Collider2D never raises OnTriggerEnter(Collider) — only *2D overloads fire on
# it — so a 2D project must get RequireComponent(Collider2D) + OnTriggerEnter2D.

def test_interactable_2d_uses_collider2d_api(tmp_path, dummy_config):
    p = InteractablePipeline(dummy_config)
    s = p.default_fix(
        InteractableSchema(name="TestIt", variant="pickup", detection="trigger",
                           meta={"game_type": "2D"}),
        "pickup")
    p.write_files(s, "pickup", tmp_path)
    cs = (tmp_path / "Scripts" / "TestIt.cs").read_text(encoding="utf-8")
    assert "RequireComponent(typeof(Collider2D))" in cs
    assert "OnTriggerEnter2D(Collider2D other)" in cs
    assert "RequireComponent(typeof(Collider))" not in cs
    assert "OnTriggerEnter(Collider other)" not in cs


def test_interactable_3d_still_uses_collider_api(tmp_path, dummy_config):
    p = InteractablePipeline(dummy_config)
    s = p.default_fix(InteractableSchema(name="TestIt", variant="pickup", detection="trigger"), "pickup")
    p.write_files(s, "pickup", tmp_path)
    cs = (tmp_path / "Scripts" / "TestIt.cs").read_text(encoding="utf-8")
    assert "RequireComponent(typeof(Collider))" in cs
    assert "OnTriggerEnter(Collider other)" in cs
    assert "OnTriggerEnter2D" not in cs


def test_trigger_zone_enter_2d_uses_collider2d_api(tmp_path, dummy_config):
    p = TriggerPipeline(dummy_config)
    s = p.default_fix(
        TriggerSchema(name="TestZone", variant="zone_enter", meta={"game_type": "2D"}),
        "zone_enter")
    p.write_files(s, "zone_enter", tmp_path)
    cs = (tmp_path / "Scripts" / "TestZone.cs").read_text(encoding="utf-8")
    assert "OnTriggerEnter2D(Collider2D other)" in cs
    assert "OnTriggerEnter(Collider other)" not in cs


def test_trigger_zone_enter_2_5d_uses_3d_collider_api(tmp_path, dummy_config):
    """2.5D is a real 3D world now (see GOLDEN_2_5D_CORE_SIGNATURE) — it must
    use the 3D Collider/OnTriggerEnter API, not the 2D one bundled with "2D"."""
    p = TriggerPipeline(dummy_config)
    s = p.default_fix(
        TriggerSchema(name="TestZone25D", variant="zone_enter", meta={"game_type": "2.5D"}),
        "zone_enter")
    p.write_files(s, "zone_enter", tmp_path)
    cs = (tmp_path / "Scripts" / "TestZone25D.cs").read_text(encoding="utf-8")
    assert "OnTriggerEnter(Collider other)" in cs
    assert "OnTriggerEnter2D(Collider2D other)" not in cs


def test_trigger_zone_enter_3d_still_uses_collider_api(tmp_path, dummy_config):
    p = TriggerPipeline(dummy_config)
    s = p.default_fix(TriggerSchema(name="TestZone", variant="zone_enter"), "zone_enter")
    p.write_files(s, "zone_enter", tmp_path)
    cs = (tmp_path / "Scripts" / "TestZone.cs").read_text(encoding="utf-8")
    assert "OnTriggerEnter(Collider other)" in cs
    assert "OnTriggerEnter2D" not in cs


def test_transition_schema_clamp():
    assert TransitionSchema(name="X", variant="weird").variant == "fade"


def test_transition_schema_clamps_echoed_option_menu():
    s = TransitionSchema(name="X", load_mode="single | additive", trigger="manual | on_trigger | on_timer")
    assert s.load_mode == "single"
    assert s.trigger == "manual"


def test_transition_fixer_door_forces_trigger(dummy_config):
    p = TransitionPipeline(dummy_config)
    s = p.default_fix(TransitionSchema(name="X", variant="trigger_door", trigger="manual"), "trigger_door")
    assert s.trigger == "on_trigger"


@pytest.mark.parametrize("variant", TransitionPipeline.variants)
def test_transition_renders_every_variant(tmp_path, dummy_config, variant):
    p = TransitionPipeline(dummy_config)
    s = p.default_fix(TransitionSchema(name="TestTr", variant=variant), variant)
    n = p.write_files(s, variant, tmp_path)
    assert n == 2
    cs = (tmp_path / "Scripts" / "TestTr.cs").read_text(encoding="utf-8")
    assert "public class TestTr" in cs
    assert "public void Go(" in cs
    assert "SceneManager" in cs


# ── P4 — unity-health ───────────────────────────────────────────────────────

def test_health_schema_clamp():
    assert HealthSchema(name="X", variant="weird").variant == "simple"


def test_health_schema_clamps_echoed_option_menu():
    s = HealthSchema(name="X", death_action="destroy | disable | respawn | event_only")
    assert s.death_action == "destroy"


def test_health_fixer_clamps_armor_and_start(dummy_config):
    p = HealthPipeline(dummy_config)
    s = p.default_fix(HealthSchema(name="X", variant="armor", armor=5.0, start_health=999, max_health=100), "armor")
    assert 0.0 <= s.armor <= 0.95
    assert s.start_health == 100


@pytest.mark.parametrize("variant", HealthPipeline.variants)
def test_health_renders_every_variant(tmp_path, dummy_config, variant):
    p = HealthPipeline(dummy_config)
    s = p.default_fix(HealthSchema(name="TestHp", variant=variant), variant)
    n = p.write_files(s, variant, tmp_path)
    assert n == 2
    cs = (tmp_path / "Scripts" / "TestHp.cs").read_text(encoding="utf-8")
    assert "public class TestHp" in cs
    assert "public void TakeDamage(" in cs
    assert "OnHealthChanged" in cs


# ── Vehicle controller (real WheelCollider rig) ────────────────────────────────

class TestVehicleController:
    def test_vehicle_params_clamped(self, dummy_config):
        p = ControllerPipeline(dummy_config)
        s = ControllerSchema(name="Car", variant="vehicle_car",
                             drift_slip_multiplier=99, suspension_spring=1,
                             max_steer_angle=500, wheel_radius=0)
        s = p.default_fix(s, "vehicle_car")
        assert s.drift_slip_multiplier == 1.0
        assert s.suspension_spring == 5000.0
        assert s.max_steer_angle == 50.0
        assert s.wheel_radius == 0.1

    def test_vehicle_car_renders_wheelcollider_rig(self, tmp_path, dummy_config):
        p = ControllerPipeline(dummy_config)
        s = p.default_fix(ControllerSchema(name="Car", variant="vehicle_car",
                                           input_backend="input_system"), "vehicle_car")
        p.write_files(s, "vehicle_car", tmp_path)
        cs = (tmp_path / "Scripts" / "Car.cs").read_text(encoding="utf-8")
        for marker in ("WheelCollider", "BuildCar", "driftSlipMultiplier",
                       "handbrakeTorque", "SpeedKmh", "GetWorldPose",
                       "Headlight", "BrakeLight", "DriftSmoke"):
            assert marker in cs, f"missing {marker}"
        assert cs.count("{") == cs.count("}")
        assert "Minimal vehicle stub" not in cs

    def test_vehicle_car_legacy_backend_renders(self, tmp_path, dummy_config):
        p = ControllerPipeline(dummy_config)
        s = p.default_fix(ControllerSchema(name="Car", variant="vehicle_car",
                                           input_backend="legacy"), "vehicle_car")
        p.write_files(s, "vehicle_car", tmp_path)
        cs = (tmp_path / "Scripts" / "Car.cs").read_text(encoding="utf-8")
        assert "Input.GetKey(KeyCode.Space)" in cs
        assert "Keyboard.current" not in cs
        assert cs.count("{") == cs.count("}")

    def test_fly_ability_renders_flight_override(self, tmp_path, dummy_config):
        p = ControllerPipeline(dummy_config)
        s = p.default_fix(ControllerSchema(name="Fl", variant="third_person",
                                           abilities=["jump", "fly", "sprint"],
                                           input_backend="input_system"), "third_person")
        assert "fly" in s.abilities and "sprint" not in s.abilities  # shift→fly wins
        p.write_files(s, "third_person", tmp_path)
        cs = (tmp_path / "Scripts" / "Fl.cs").read_text(encoding="utf-8")
        assert "flySpeed" in cs and "flyHeld" in cs


# ── Waypoint-follow AI (racing line / route runner) ────────────────────────────

class TestWaypointFollowAI:
    def test_waypoint_params_clamped(self, dummy_config):
        p = AIPipeline(dummy_config)
        s = AISchema(name="Racer", variant="waypoint_follow",
                     slowdown_angle=500, rubber_band_strength=9,
                     waypoint_parent_name="  ")
        s = p.default_fix(s, "waypoint_follow")
        assert s.nav_backend == "transform_step"
        assert s.slowdown_angle == 90.0
        assert s.rubber_band_strength == 1.0
        assert s.waypoint_parent_name == "Waypoints"

    def test_waypoint_follow_renders_route_runner(self, tmp_path, dummy_config):
        p = AIPipeline(dummy_config)
        s = p.default_fix(AISchema(name="RivalRacer", variant="waypoint_follow"),
                          "waypoint_follow")
        p.write_files(s, "waypoint_follow", tmp_path)
        cs = (tmp_path / "Scripts" / "RivalRacer.cs").read_text(encoding="utf-8")
        for marker in ("CollectWaypoints", "slowdownAngle", "rubberBandStrength",
                       "waypointParentName", "_wpIndex"):
            assert marker in cs, f"missing {marker}"
        assert "NavMeshAgent" not in cs          # transform_step needs no NavMesh
        assert cs.count("{") == cs.count("}")


# ── Using hardening (live failures from the SunsetDriftKingdom beta run) ───────

class TestUsingHardening:
    def test_type_names_dropped_from_usings(self):
        from src.micro.code_fields import lint_usings, _KNOWN_NAMESPACES
        out = lint_usings(["UnityEngine.AudioSource", "UnityEngine.ParticleSystem",
                           "UnityEngine.InputSystem", "System.Collections.Generic"],
                          already_in_template={"UnityEngine"},
                          allowed=_KNOWN_NAMESPACES)
        assert out == ["UnityEngine.InputSystem", "System.Collections.Generic"]

    def test_missing_generic_using_auto_added(self):
        from src.unity_schema import UnityScript
        from src.micro.code_fields import lint_extensible
        s = UnityScript(name="HL", type="custom", description="x",
                        extra_fields=[{"name": "_lights", "type": "List<Light>",
                                       "default": "new List<Light>()"}],
                        update_extra="if (Keyboard.current != null) _lights.Clear();")
        lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"})
        assert "System.Collections.Generic" in s.extra_usings
        assert "UnityEngine.InputSystem" in s.extra_usings

    def test_bare_inputsystem_reference_gets_using(self):
        # Regression (live Unity, AlpineClimber2D run): a custom script that
        # calls InputSystem.GetDevice<Keyboard>() directly (not via
        # Keyboard.current) is CS0103 "the name 'InputSystem' does not exist
        # in the current context" without `using UnityEngine.InputSystem;`.
        from src.unity_schema import UnityScript
        from src.micro.code_fields import lint_extensible
        s = UnityScript(name="SlopeDetector", type="custom", description="x",
                        update_extra="var kb = InputSystem.GetDevice<Keyboard>();")
        lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"})
        assert "UnityEngine.InputSystem" in s.extra_usings

    def test_spriteatlas_reference_gets_using_and_survives_allowlist(self):
        # Regression (live Unity, AlpineClimber2D run): Resources.Load<SpriteAtlas>
        # without `using UnityEngine.U2D;` is CS0311 (SpriteAtlas isn't found,
        # so it doesn't resolve as a UnityEngine.Object). UnityEngine.U2D also
        # has to be in _KNOWN_NAMESPACES or lint_usings' allowlist filter drops
        # it right back out before the auto-add trigger even runs.
        from src.unity_schema import UnityScript
        from src.micro.code_fields import lint_extensible
        s = UnityScript(name="SpriteAtlasLoader", type="custom", description="x",
                        awake_extra="var atlas = Resources.Load<SpriteAtlas>(\"Atlas\");")
        lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"})
        assert "UnityEngine.U2D" in s.extra_usings

    def test_type_names_dropped_by_every_micro_generator(self, dummy_config):
        # Regression (live Unity, RepoFlowSim run): lint_usings' allowlist
        # filter (tested above) only helps a caller that actually passes
        # `allowed=_KNOWN_NAMESPACES` — trigger/vfx/statemachine/shader/
        # lighting/animator each called lint_usings without it, so
        # `using UnityEngine.AudioSource;` / `using UnityEngine.ParticleSystem;`
        # (a model naming the TYPE it wants, not a namespace) rode straight
        # through into SceneTrigger.cs and ImpactVFX.cs — CS0138, 3 compile
        # errors, invisible to the syntax gate because it's a binding-level
        # error. Only controller.py wired the allowlist through; this locks
        # in that every generator touching LLM-authored extra_usings does.
        from src.micro.vfx import VfxPipeline, VfxSchema
        from src.micro.lighting import LightingPipeline, LightingSchema
        from src.micro import AnimatorPipeline, AnimatorSchema, StateMachinePipeline, StateMachineSchema

        bad = ["UnityEngine.AudioSource", "UnityEngine.ParticleSystem"]
        cases = [
            (TriggerPipeline(dummy_config),
             TriggerSchema(name="Zone", extra_usings=list(bad)), "zone_enter"),
            (VfxPipeline(dummy_config),
             VfxSchema(name="Impact", extra_usings=list(bad)), "particle_system"),
            (StateMachinePipeline(dummy_config),
             StateMachineSchema(name="Fsm", extra_usings=list(bad)), "player_fsm"),
            (ShaderPipeline(dummy_config),
             ShaderSchema(name="Shade", extra_usings=list(bad)), "color_cycle"),
            (LightingPipeline(dummy_config),
             LightingSchema(name="Lights", extra_usings=list(bad)), "three_point"),
            (AnimatorPipeline(dummy_config),
             AnimatorSchema(name="Anim", extra_usings=list(bad)), "humanoid"),
        ]
        for pipeline, schema, variant in cases:
            fixed = pipeline.default_fix(schema, variant)
            for u in bad:
                assert u not in fixed.extra_usings, (
                    f"{type(pipeline).__name__} let a type-as-namespace "
                    f"using ({u!r}) through — would raise CS0138")


class TestBehaviorReconciliation:
    def test_unknown_enemy_behavior_rewires_to_enemy_script(self):
        from src.unity_schema import UnitySchema, UnityScript, EntitySpec
        from src.unity_generator import UnityProjectGenerator
        gen = UnityProjectGenerator("templates/unity")
        schema = UnitySchema(
            project_name="BehFix", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="EnemyAI", type="enemy", description="e")],
            entities=[
                EntitySpec(name="Rivals", kind="enemy", count=2, behavior="AIWaypointDriver"),
                EntitySpec(name="Props", kind="prop", count=2, behavior="MadeUpThing"),
            ],
        )
        files = gen.generate(schema, {})
        cs = files["BehFix/Assets/Scripts/EntitySpawner.cs"]
        assert '"EnemyAI"' in cs            # enemy group rewired to the real class
        assert "AIWaypointDriver" not in cs
        assert "MadeUpThing" not in cs      # non-enemy unknown behavior cleared


def test_subsystems_for_racing_genre_uses_waypoint_ai():
    # waypoint_follow needs a "Waypoints" container, which only a path-placed
    # racing scaffold ever emits — gated on genre=="racing" (the actual
    # racing-game signal from unity_archetypes.py), not locomotion=="drive"
    # alone. locomotion=="drive" only means the PLAYER uses a car to get
    # around; it says nothing about whether ENEMIES should race a line.
    # RustbeltDepthTestbed (drive locomotion, genre="universal", sentry-drone
    # enemies with no waypoints at all) was live-caught getting
    # "waypoint_follow" under the old locomotion-only gate — its drones had
    # no Waypoints container to follow and never moved.
    from src.unity_compose import subsystems_for
    subs = subsystems_for("3D", ["ai"], locomotion="drive", genre="racing")
    ai = next(s for s in subs if s.kind == "ai")
    assert ai.variant == "waypoint_follow"

    # Same locomotion, non-racing genre -> patrol/chase AI, not waypoint-follow.
    non_racing_drive_ai = next(
        s for s in subsystems_for("3D", ["ai"], locomotion="drive", genre="universal")
        if s.kind == "ai")
    assert non_racing_drive_ai.variant == "navmesh_patrol"

    walk_ai = next(s for s in subsystems_for("3D", ["ai"], locomotion="walk")
                   if s.kind == "ai")
    assert walk_ai.variant == "navmesh_patrol"


# ── Shader ───────────────────────────────────────────────────────────────────

def test_shader_schema_clamps_unknown_variant():
    s = ShaderSchema(name="X", variant="bogus")
    assert s.variant == "color_cycle"


def test_shader_fixer_fills_default_property_per_variant(dummy_config):
    p = ShaderPipeline(dummy_config)
    for variant, expected in {
        "color_cycle": "_Color", "dissolve": "_DissolveAmount",
        "hit_flash": "_Color", "uv_scroll": "_MainTex",
        "outline_pulse": "_OutlineIntensity",
    }.items():
        s = p.default_fix(ShaderSchema(name="X", variant=variant), variant)
        assert s.property_name == expected


def test_shader_fixer_respects_explicit_property_name(dummy_config):
    p = ShaderPipeline(dummy_config)
    s = p.default_fix(
        ShaderSchema(name="X", variant="dissolve", property_name="_MyDissolve"),
        "dissolve")
    assert s.property_name == "_MyDissolve"


def test_shader_fixer_seeds_color_cycle_stops(dummy_config):
    p = ShaderPipeline(dummy_config)
    s = p.default_fix(ShaderSchema(name="X", variant="color_cycle"), "color_cycle")
    assert len(s.stops) >= 2


def test_shader_fixer_swaps_inverted_pulse_range(dummy_config):
    p = ShaderPipeline(dummy_config)
    s = p.default_fix(
        ShaderSchema(name="X", variant="outline_pulse", min_value=1.0, max_value=0.0),
        "outline_pulse")
    assert s.min_value <= s.max_value


@pytest.mark.parametrize("variant", ShaderPipeline.variants)
def test_shader_renders_every_variant(tmp_path, dummy_config, variant):
    p = ShaderPipeline(dummy_config)
    s = ShaderSchema(name="TestShader", variant=variant)
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / "TestShader.cs").read_text(encoding="utf-8")
    assert "public class TestShader" in cs
    assert f"Variant: {variant}" in cs
    assert "MaterialPropertyBlock" in cs
    assert count == 2


def test_shader_uv_scroll_uses_st_property_suffix(tmp_path, dummy_config):
    p = ShaderPipeline(dummy_config)
    s = p.default_fix(ShaderSchema(name="Scroller", variant="uv_scroll"), "uv_scroll")
    p.write_files(s, "uv_scroll", tmp_path)
    cs = (tmp_path / "Scripts" / "Scroller.cs").read_text(encoding="utf-8")
    assert 'Shader.PropertyToID(propertyName + "_ST")' in cs


def test_shader_atom_behavior_map_entry_present():
    from src.atoms.behavior_map import microgen_atoms
    atoms = microgen_atoms("shader")
    assert atoms
    assert atoms[0].atom == "SetMaterial"


# ── CS0592 — an attribute list with no field under it ────────────────────────
# `[Header]`, `[Tooltip]`, `[SerializeField]` and friends are legal only on a
# property, indexer or field. A template that prints a header unconditionally
# above fields that are ALL conditional emits a bare attribute list whenever
# none of those conditions holds, and the attribute then binds to whatever
# declaration comes next. Live on a RepoQuestEnterprise build,
# EnemySpawner.cs(30,6): `[Header("Where to spawn")]` on the `point` shape
# (which declares no positioning field) landed on the `OnDespawned` EVENT.
#
# Roslyn cannot catch this for us — `src/csharp_syntax_gate.py` is a PARSE-only
# gate by design, and a misplaced attribute parses perfectly. It is an
# attribute-target rule, decided during binding, so it has to be checked here.

_ATTR_RUN_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
# The head of a declaration: everything before its initializer, body or arrow.
_DECL_HEAD_RE = re.compile(r"^(.*?)(?:=>|[={;])")


def _is_attribute_target(line: str) -> bool:
    """True when `line` declares something an attribute may legally sit on.

    Fields, properties and indexers qualify; methods, events, types and a bare
    brace do not. The discriminator is a parameter list in the declaration
    HEAD — `public int AliveCount => _alive.Count;` has none and is a property,
    `private void OnEnable() => StartCoroutine(...)` has one and is a method.
    """
    s = line.strip()
    if not s or s[0] in "{}":
        return False
    head = (_DECL_HEAD_RE.match(s) or re.match(r"^(.*)$", s)).group(1)
    if "(" in head:
        return False                                  # method / constructor
    tokens = head.replace("\t", " ").split()
    return not ({"event", "class", "struct", "interface", "enum",
                 "delegate", "namespace"} & set(tokens))


def dangling_attribute_lists(cs: str) -> list[tuple[int, str]]:
    """`[(line_number, attribute_text), ...]` for every attribute list in `cs`
    whose target is not a field, property or indexer — i.e. every CS0592."""
    lines = cs.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        m = _ATTR_RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        first, attrs = i, m.group(0).strip()
        rest = lines[i][m.end():].strip()
        # Walk to the declaration this attribute run applies to, stepping over
        # blank lines, comments, preprocessor lines and any further attribute
        # lines (which extend the same run rather than starting a new one).
        while not rest or rest.startswith(("//", "/*", "*", "#")):
            i += 1
            if i >= len(lines):
                rest = ""
                break
            nxt = _ATTR_RUN_RE.match(lines[i])
            rest = lines[i][nxt.end():].strip() if nxt else lines[i].strip()
        if not _is_attribute_target(rest):
            out.append((first + 1, attrs))
        i += 1
    return out


def test_dangling_attribute_detector_reads_csharp_targets():
    """The detector itself — a check nobody can trust unless it is checked."""
    assert not dangling_attribute_lists(
        '[Header("A")]\n[SerializeField] private int x = 1;')
    assert not dangling_attribute_lists(
        '[Header("A")]\n\n    // note\n    private float y;')
    assert not dangling_attribute_lists(
        "[Header(\"A\")]\npublic int Count => _list.Count;")     # property
    assert dangling_attribute_lists(
        '[Header("A")]\npublic event Action<int> OnDone;')       # the live bug
    assert dangling_attribute_lists(
        '[Header("A")]\nprivate void Update() { }')
    assert dangling_attribute_lists('[Header("A")]\n}')


@pytest.mark.parametrize("variant", SpawnerPipeline.variants)
@pytest.mark.parametrize("shape", ["point", "circle", "box", "spawn_points"])
@pytest.mark.parametrize("pooling", ["instantiate", "pooled"])
def test_spawner_never_emits_a_headerless_field_block(
        tmp_path, dummy_config, variant, shape, pooling):
    p = SpawnerPipeline(dummy_config)
    s = SpawnerSchema(name="EnemySpawner", variant=variant,
                      spawn_shape=shape, pooling=pooling)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    for f in sorted((tmp_path / "Scripts").glob("*.cs")):
        bad = dangling_attribute_lists(f.read_text(encoding="utf-8"))
        assert not bad, f"{f.name} {variant}/{shape}/{pooling}: CS0592 at {bad}"


@pytest.mark.parametrize("variant", HUDPipeline.variants)
@pytest.mark.parametrize("backend", ["uGUI", "UIToolkit"])
def test_hud_never_emits_a_headerless_field_block(
        tmp_path, dummy_config, variant, backend):
    """The uGUI `[Header("uGUI refs")]` is guarded by the BACKEND while every
    field under it is keyed on the VARIANT, so it stays legal only while the
    variant chain is total. This is what fails if an eleventh variant is added
    without a uGUI branch."""
    p = HUDPipeline(dummy_config)
    s = HUDSchema(name="TestHUD", variant=variant, backend=backend)
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    for f in sorted((tmp_path / "Scripts").glob("*.cs")):
        bad = dangling_attribute_lists(f.read_text(encoding="utf-8"))
        assert not bad, f"{f.name} {variant}/{backend}: CS0592 at {bad}"


# ── CS0103: `_grid` declared, `grid` written ─────────────────────────────────

class TestUnderscoreAliasRebinding:
    """Live regression (Gridfall/InputHandler): the author declared
    `[SerializeField] private GridLogic _grid`, guarded with
    `if (_grid == null) return;`, then called `grid.SetScore(...)` — three
    CS0103s in one file. Rebinding to the declared field is the only repair
    that keeps the behaviour: neutralizing deletes it, and declaring a second
    `grid` field ships a collaborator nothing ever wires.
    """

    def _fix(self, body, unbound, declared):
        from src.micro.code_fields import lint_underscore_alias_references
        return lint_underscore_alias_references(
            body, unbound=unbound, declared=declared)

    def test_rebinds_to_the_declared_field(self):
        out, rebound = self._fix(
            "grid.SetScore(grid.GetScore() + 1);", {"grid"}, {"_grid"})
        assert out == "_grid.SetScore(_grid.GetScore() + 1);"
        assert rebound == {"grid"}

    def test_leaves_a_longer_identifier_alone(self):
        out, rebound = self._fix("int n = gridSize;", {"grid"}, {"_grid"})
        assert out == "int n = gridSize;"
        assert not rebound

    def test_leaves_strings_and_comments_alone(self):
        src = 'var s = "grid";  // grid\n'
        out, rebound = self._fix(src, {"grid"}, {"_grid"})
        assert out == src
        assert not rebound

    def test_leaves_a_bound_local_alone(self):
        # Not in `unbound`, so it compiles today and must not be touched.
        src = "var grid = New(); grid.Go();"
        assert self._fix(src, set(), {"_grid"})[0] == src

    def test_does_not_double_underscore_a_member_access(self):
        src = "other._grid.Ping();"
        assert self._fix(src, {"grid"}, {"_grid"})[0] == src


# ── Authored bodies that need an import, or contradict their own field type ──

class TestAuthoredBodyRepairs:
    """Both hit live in one Gridfall build of GridLogic."""

    def test_array_use_requires_system(self):
        from src.micro.code_fields import required_usings_for
        # lint_usings only ever FILTERED what the model asked for, so a body
        # calling Array.Fill shipped without `using System;` -> CS0103.
        assert required_usings_for("Array.Fill(grid[0], -1);") == ["System"]

    def test_generic_collection_requires_its_namespace(self):
        from src.micro.code_fields import required_usings_for
        assert required_usings_for("var p = new List<int>();") == [
            "System.Collections.Generic"]

    def test_random_is_left_alone_because_it_is_ambiguous(self):
        from src.micro.code_fields import required_usings_for
        # UnityEngine.Random and System.Random are both real — importing
        # System here would trade CS0103 for CS0104.
        assert required_usings_for("Random.Range(0, 3);") == []

    def test_jagged_rank_follows_the_authors_own_declaration(self):
        from src.micro.code_fields import jagged_rank_corrections
        body = "int[][] grid = _grid; grid[y][x] = -1;"
        assert jagged_rank_corrections([("_grid", "int[][][]")], body) == {
            "_grid": "int[][]"}

    def test_jagged_rank_abstains_without_an_explicit_declaration(self):
        from src.micro.code_fields import jagged_rank_corrections
        # Indexing depth alone is a guess; a wrong guess ships a wrong program.
        assert jagged_rank_corrections([("_grid", "int[][][]")],
                                       "_grid[y][x] = 1;") == {}

    def test_jagged_rank_abstains_on_conflicting_declarations(self):
        from src.micro.code_fields import jagged_rank_corrections
        assert jagged_rank_corrections(
            [("_grid", "int[][][]")], "int[][] a = _grid; int[] b = _grid;") == {}


class TestBracketLiteralDefaults:
    """`private Vector2 _targetRect = [0.5, 0.5];` — Python/JS list syntax in a
    C# initializer, CS1525 "Invalid expression term '['". Live on Gridfall's
    CameraController. The model meant the value; only the syntax is wrong, so
    rebuild it rather than drop it — the same contract sanitize_default already
    uses when unquoting `"0.5"`.
    """

    def _s(self, ty, default):
        from src.micro.code_fields import sanitize_default
        return sanitize_default(ty, default)

    def test_vector2_literal_is_rebuilt_with_float_suffixes(self):
        # Without the `f`, fixing the brackets would just trade CS1525 for
        # CS1503 (double -> float).
        assert self._s("Vector2", "[0.5, 0.5]") == "new Vector2(0.5f, 0.5f)"

    def test_array_literal_is_rebuilt(self):
        assert self._s("int[]", "[1, 2, 3]") == "new int[] { 1, 2, 3 }"

    def test_wrong_arity_is_dropped_not_guessed(self):
        assert self._s("Vector2", "[1, 2, 3]") is None

    def test_a_valid_initializer_is_untouched(self):
        assert self._s("Vector2", "new Vector2(1f, 2f)") == "new Vector2(1f, 2f)"


class TestMisqualifiedTypes:
    """`UnityEngine.Touchscreen` — CS0234. Touch types read like core engine
    types but live in UnityEngine.InputSystem. Live on BlockWell's TouchInput,
    which had already imported the right namespace and over-qualified anyway.
    """

    def _f(self, ty):
        from src.micro.code_fields import correct_qualified_type
        return correct_qualified_type(ty)

    def test_touchscreen_is_requalified(self):
        assert self._f("UnityEngine.Touchscreen") == (
            "Touchscreen", "UnityEngine.InputSystem")

    def test_already_correct_is_untouched(self):
        assert self._f("UnityEngine.InputSystem.Touchscreen")[1] == ""

    def test_a_real_unityengine_type_is_untouched(self):
        assert self._f("UnityEngine.Transform") == ("UnityEngine.Transform", "")

    def test_an_unknown_qualified_type_is_left_alone(self):
        assert self._f("MyNs.Thing") == ("MyNs.Thing", "")


class TestRunawayBodyCapLeavesParseableCode:
    """A line-count cap knows nothing about brace depth.

    Live regression (Gridfall/GridLogic): the model degenerated into repeating
    one `for` block, the 60-line cap landed inside the last copy, and the file
    ended mid-block — CS1022, and the syntax gate then rejected the ENTIRE
    build. One runaway method cost every other script in it.
    """

    def _cap(self, text, what="public void ApplyGravity()"):
        from src.micro.code_fields import _cap_lines
        return _cap_lines(text, what=what)

    def _depth(self, text):
        from src.micro.code_fields import _unclosed_braces
        return _unclosed_braces(text)

    def test_a_cut_inside_a_block_is_closed(self):
        body = "\n".join(["for (int r = 0; r < n; r++) {\n    cells[i] = 2;\n}"] * 30)
        assert self._depth("\n".join(body.splitlines()[:59])) > 0   # naive slice breaks
        assert self._depth(self._cap(body)) == 0                     # cap does not

    def test_a_short_body_is_untouched(self):
        assert self._cap("int a = 0;") == "int a = 0;"

    def test_the_truncation_notice_survives(self):
        body = "\n".join(["x();"] * 200)
        assert "truncated at" in self._cap(body)

    def test_braces_in_strings_and_comments_are_not_counted(self):
        assert self._depth('var s = "{";') == 0
        assert self._depth("// {") == 0
        assert self._depth("/* { */") == 0
        assert self._depth("var c = '{';") == 0

    def test_escaped_quote_does_not_swallow_the_rest(self):
        assert self._depth('var s = "he said \\"hi\\""; {') == 1
