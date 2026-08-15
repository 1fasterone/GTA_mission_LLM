"""
Step-3 schema-fill prompt tests — the multimodal IR ask (src/unity_prompts.py).

UnitySchema is a full multimodal IR and PR #69 wired every one of those fields
into the deterministic pipeline for CONSUMPTION, but the Step-3 prompt never
mentioned them, so they were empty on every generated project. These tests lock
in that the prompt asks for them, that the shapes it documents match the Pydantic
models AND the deterministic readers downstream, and that the token cost stays
bounded on a local model's context window.
"""

import re

import pytest

from src.atoms import get_atom
from src.unity_compose import subsystems_for
from src.unity_prompts import (
    GAMECONTEXT_SCHEMA_GUIDANCE,
    UNITY_SCHEMA_FILL_SYSTEM,
    UNITY_SCHEMA_FILL_USER,
    WIRING_MICRO_KINDS,
    MultimodalBlock,
    atom_vocabulary,
    render_multimodal_block,
)
from src.unity_schema import (
    AIBehaviorTree,
    AnimationSpec,
    AudioSpec,
    BehaviorTree,
    BTNode,
    CharacterSpec,
    MaterialSpec,
    MissionSpec,
    MicrogensSpec,
    ModelSpec,
    ProjectCapabilities,
    ShaderSpec,
    UnitySchema,
    VfxSpec,
    WiringMap,
)

# A brief that name-checks every optional section's signal vocabulary, so
# `max_optional` is the only thing limiting what ships.
EVERYTHING_CONCEPT = (
    "A 3D action game. Voiced NPC quest givers hold branching dialogue with the "
    "player. Enemies patrol and chase on sight, with a boss. Animation: a "
    "humanoid rig with an idle and a walk cycle. Explosions throw sparks, smoke "
    "and debris particles. Custom toon shader materials with metallic textures "
    "dress the procedural meshes. A cinematic intro cutscene fades in before the "
    "opening sequence."
)

CORE_SECTIONS = ("capabilities", "characters", "missions", "audio_specs")
OPTIONAL_SECTIONS = ("dialog", "behavior_trees", "animations", "vfx",
                     "surfaces", "sequence")


def block(concept=EVERYTHING_CONCEPT, prompt="", **kw) -> MultimodalBlock:
    return render_multimodal_block(concept, prompt, **kw)


def full_prompt(mm: MultimodalBlock, concept: str = "a brief") -> str:
    return UNITY_SCHEMA_FILL_USER.format(concept=concept, schema_hints="",
                                         multimodal_block=mm.text)


# ── The regression this whole change exists for ───────────────────────────────

class TestMultimodalFieldsAreAskedFor:
    """Every always-empty field is now named in the prompt the LLM actually gets."""

    # The fields verified empty across three separately generated projects.
    ALWAYS_EMPTY = ("characters", "missions", "audio_specs", "materials",
                    "models", "animations", "vfx", "shaders", "behavior_trees",
                    "microgens", "atoms", "capabilities")

    @pytest.mark.parametrize("field", ALWAYS_EMPTY)
    def test_field_named_in_rendered_prompt(self, field):
        rendered = full_prompt(block(max_optional=len(OPTIONAL_SECTIONS)))
        assert f'"{field}"' in rendered, f"{field} is never asked for"

    def test_wiring_is_asked_for_in_compose_mode(self):
        # wiring's source/target vocabulary is the micro kinds, which only exist
        # under --compose — so it rides the compose-only guidance, not the base.
        assert '"wiring"' in GAMECONTEXT_SCHEMA_GUIDANCE
        assert '"wiring"' not in full_prompt(block())

    def test_world_spec_is_deliberately_not_asked_for(self):
        # unity_layout / unity_racetrack author world_spec deterministically in
        # Step 5 — asking the LLM for it would only cost tokens.
        assert '"world_spec"' not in full_prompt(block(max_optional=99))

    def test_system_prompt_frames_the_schema_as_the_full_design(self):
        assert "characters" in UNITY_SCHEMA_FILL_SYSTEM


# ── Gating: core always, optional on content ──────────────────────────────────

class TestSectionGating:
    def test_core_sections_always_ship(self):
        for concept in ("", "a menu", EVERYTHING_CONCEPT):
            mm = render_multimodal_block(concept)
            assert mm.sections[:len(CORE_SECTIONS)] == CORE_SECTIONS

    def test_empty_concept_gets_core_only(self):
        assert render_multimodal_block("").sections == CORE_SECTIONS

    def test_optional_section_fires_on_its_own_signal(self):
        mm = render_multimodal_block("Explosions throw sparks and smoke particles.")
        assert "vfx" in mm.sections
        assert '"vfx": max 4' in mm.text

    def test_optional_section_stays_out_without_signal(self):
        mm = render_multimodal_block("A quiet 2D puzzle game about sliding tiles.")
        assert mm.sections == CORE_SECTIONS
        assert '"behavior_trees"' not in mm.text

    def test_user_prompt_also_supplies_signals(self):
        mm = render_multimodal_block("A quiet game.", "enemies that patrol and chase")
        assert "behavior_trees" in mm.sections

    def test_strongest_signal_wins_when_capped(self):
        # vfx is named far more often than dialogue here, so at a cap of 1 it is
        # the one that ships even though "dialog" is declared first.
        concept = ("A quest giver talks to you. Explosions, sparks, smoke, debris, "
                   "dust and flames erupt from every impact.")
        mm = render_multimodal_block(concept, max_optional=1)
        assert mm.sections == CORE_SECTIONS + ("vfx",)
        assert mm.scores["vfx"] > mm.scores["dialog"]

    @pytest.mark.parametrize("cap", [0, 1, 2, 3, 6])
    def test_max_optional_caps_optional_sections(self, cap):
        mm = block(max_optional=cap)
        optional = [s for s in mm.sections if s not in CORE_SECTIONS]
        assert len(optional) <= cap

    def test_everything_concept_can_reach_every_section(self):
        mm = block(max_optional=len(OPTIONAL_SECTIONS))
        assert set(mm.sections) == set(CORE_SECTIONS) | set(OPTIONAL_SECTIONS)

    def test_sections_render_in_declaration_order(self):
        mm = block(max_optional=len(OPTIONAL_SECTIONS))
        optional = [s for s in mm.sections if s not in CORE_SECTIONS]
        assert optional == list(OPTIONAL_SECTIONS)

    def test_negative_cap_is_treated_as_zero(self):
        assert block(max_optional=-3).sections == CORE_SECTIONS

    def test_deterministic(self):
        a, b = block(), block()
        assert a.text == b.text and a.sections == b.sections


class TestSignalMatching:
    """Word-boundary matched — the class of false positive that silently
    mis-fired the audio keyword table before it was boundary-matched."""

    @pytest.mark.parametrize("text", [
        "the player sits on a chair",          # 'ai' inside 'chair'
        "a rigid body simulation",             # 'rig' inside 'rigid'
        "the guardian angel statue",           # 'guard' inside 'guardian'
        "a glowing idler",                     # 'glow' in 'glowing', 'idle' in 'idler'
    ])
    def test_substring_does_not_fire_a_section(self, text):
        assert render_multimodal_block(text).sections == CORE_SECTIONS

    def test_whole_word_does_fire(self):
        assert "behavior_trees" in render_multimodal_block("a guard on patrol").sections


# ── Shapes match the Pydantic models ──────────────────────────────────────────

class TestDocumentedShapesValidate:
    """Each documented example, transcribed from the prompt body, validates
    against the model it claims to describe."""

    def test_capabilities_sub_objects(self):
        caps = ProjectCapabilities.model_validate({
            "controller": {"movement": True, "jump": True, "sprint": False,
                           "crouch": False, "speed": 6.0, "sprint_multiplier": 1.7,
                           "jump_force": 8.0, "look_sensitivity": 2.0},
            "physics":    {"gravity": True, "collision": "dynamic", "mass": 80.0, "drag": 0.0},
            "hud":        {"show_health": True, "show_ammo": False,
                           "show_objective": True, "show_minimap": False},
            "input":      {"bindings": [{"action": "Fire", "key": "Mouse0"}]},
            "ai":         {"pathfinding": True, "detection_range": 15.0, "behavior": "patrol"},
            "animation":  {"rig_type": "humanoid", "clips": {"Idle": "Idle", "Run": "Run"}},
            "dialog":     {"system": "simple", "bark_on_sight": False},
            "vfx":        {"particles": True, "post_processing": False},
            "model":      {"lod": "auto", "animations": {"Idle": "Idle"}},
        })
        # No sub-model silently normalised away a documented value.
        assert caps.physics.collision == "dynamic"
        assert caps.ai.behavior == "patrol"
        assert caps.animation.rig_type == "humanoid"
        assert caps.dialog.system == "simple"
        assert caps.model.lod == "auto"
        assert caps.input.bindings[0].action == "Fire"

    def test_character_with_dialog_graph_and_brain(self):
        char = CharacterSpec.model_validate({
            "id": "pit_chief", "name": "Pit Chief", "role": "npc",
            "model_ref": "CrewMembers", "animation_set": ["Idle", "Run"],
            "dialog_graph": {"start_node": "n1", "nodes": [
                {"id": "n1", "speaker": "Pit Chief", "text": "Tyres are cold.",
                 "emotion": "neutral", "audio_ref": "chief_line_1",
                 "choices": [{"text": "Roger", "next_node": "n2", "condition": ""}]}]},
            "npc_brain": {"traits": ["gruff"], "goals": ["keep the car alive"], "memory": []},
        })
        assert char.role == "npc"
        assert char.dialog_graph.nodes[0].choices[0].next_node == "n2"
        assert char.npc_brain.goals == ["keep the car alive"]

    def test_mission_with_objectives(self):
        mission = MissionSpec.model_validate({
            "id": "opening_lap", "title": "Opening Lap", "description": "Win it.",
            "objectives": [{"id": "obj_1", "description": "Cross the line first",
                            "type": "reach", "target_ref": "FinishLine",
                            "count": 1, "optional": False}],
            "rewards": {"score": 100}, "story_beat_ref": "",
        })
        assert mission.objectives[0].type == "reach"

    def test_audio_spec(self):
        spec = AudioSpec.model_validate({
            "id": "engine_note", "type": "sfx", "description": "a growling V8",
            "generation_method": "stable_audio", "loop": True,
            "parameters": {"gen_prompt": "growling V8 idle", "spatial": False},
        })
        assert spec.generation_method == "stable_audio"

    def test_animation_vfx_material_shader_model_specs(self):
        assert AnimationSpec.model_validate(
            {"id": "run", "name": "Run", "clip": "Run", "loop": True,
             "events": ["Footstep"]}).name == "Run"
        assert VfxSpec.model_validate(
            {"id": "crash_burst", "name": "CrashBurst", "type": "particle",
             "parameters": {"loop": False, "duration": 2.0, "play_on_start": True,
                            "auto_destroy": True, "scale": 1.0}}).type == "particle"
        assert MaterialSpec.model_validate(
            {"id": "chrome", "name": "Chrome", "shader_model": "Standard",
             "generation_method": "procedural",
             "parameters": {"color": "#B0B0B0", "smoothness": 0.3}}).generation_method == "procedural"
        assert ShaderSpec.model_validate(
            {"id": "toon", "name": "Toon", "language": "shadergraph",
             "parameters": {"note": "hard ramp"}}).language == "shadergraph"
        assert ModelSpec.model_validate(
            {"id": "rival_car", "name": "RivalCar", "category": "vehicle",
             "generation_method": "blender_microgen", "parameters": {},
             "materials": ["chrome"]}).category == "vehicle"

    def test_behavior_tree(self):
        entry = AIBehaviorTree.model_validate({
            "id": "guard_brain", "name": "GuardBrain", "tree": {"root": "n1", "nodes": [
                {"id": "n1", "type": "selector", "children": ["n2", "n4"]},
                {"id": "n2", "type": "sequence", "children": ["n3"]},
                {"id": "n3", "type": "condition", "action": "PlayerInRange",
                 "params": {"range": 12}},
                {"id": "n4", "type": "action", "action": "Patrol",
                 "params": {"speed": 2.0}}]}})
        ids = {n.id for n in entry.tree.nodes}
        assert entry.tree.root in ids
        for node in entry.tree.nodes:
            assert set(node.children) <= ids

    def test_microgen_and_atoms(self):
        mg = MicrogensSpec.model_validate({
            "kind": "intro_sequence", "input": {}, "output_files": [],
            "atoms": [{"atom": "Wait", "args": {"seconds": 1.0}, "label": "beat"}]})
        assert mg.atoms[0].atom == "Wait"
        schema = UnitySchema.model_validate({
            "project_name": "P", "game_type": "3D", "platform": "pc",
            "render_pipeline": "URP",
            "atoms": [{"atom": "ScreenFade", "args": {"alpha": 0.0, "duration": 1.5},
                       "label": "fade in"},
                      {"atom": "CameraShake", "args": {"intensity": 0.4, "duration": 1.0},
                       "label": "impact"}]})
        assert [a.atom for a in schema.atoms] == ["ScreenFade", "CameraShake"]

    def test_wiring_map(self):
        wiring = WiringMap.model_validate(
            {"dependencies": [{"source": "trigger", "target": "objectives",
                               "type": "triggers"}]})
        assert wiring.dependencies[0].type == "triggers"


class TestDocumentedFieldNames:
    """The section body has to name the model's real fields — a shape that drifts
    from the Pydantic model teaches the LLM to emit keys that get dropped."""

    CASES = [
        (CharacterSpec,  ("id", "name", "role", "model_ref", "animation_set")),
        (MissionSpec,    ("id", "title", "description", "objectives", "rewards",
                          "story_beat_ref")),
        (AudioSpec,      ("id", "type", "description", "generation_method",
                          "loop", "parameters")),
        (AnimationSpec,  ("id", "name", "clip", "loop", "events")),
        (VfxSpec,        ("id", "name", "type", "parameters")),
        (MaterialSpec,   ("id", "name", "shader_model", "generation_method",
                          "parameters")),
        (ShaderSpec,     ("id", "name", "language", "parameters")),
        (ModelSpec,      ("id", "name", "category", "generation_method",
                          "parameters", "materials")),
        (MicrogensSpec,  ("kind", "input", "output_files", "atoms")),
    ]

    @pytest.mark.parametrize("model,fields", CASES, ids=[c[0].__name__ for c in CASES])
    def test_documented_fields_exist_on_the_model(self, model, fields):
        text = block(max_optional=len(OPTIONAL_SECTIONS)).text
        for field in fields:
            assert field in model.model_fields, f"{model.__name__}.{field} does not exist"
            assert f'"{field}"' in text, f"{model.__name__}.{field} is never documented"


class TestDocumentedParametersMatchReaders:
    """`parameters` is a free-form dict, so the ONLY thing tying the prompt to the
    deterministic consumer is the key names. Each list below is what its reader
    actually looks up — anything else the LLM writes there is silently ignored."""

    @pytest.mark.parametrize("keys,reader", [
        # unity_generator._material_hex_and_smoothness
        (("color", "smoothness"), "materials"),
        # unity_compose._audio_cues_from_specs
        (("gen_prompt", "spatial"), "audio_specs"),
        # unity_compose._vfx_options_from_specs
        (("loop", "duration", "play_on_start", "auto_destroy", "scale"), "vfx"),
    ])
    def test_parameter_keys_are_documented(self, keys, reader):
        text = block(max_optional=len(OPTIONAL_SECTIONS)).text
        for key in keys:
            assert f'"{key}"' in text, f"{reader}.parameters.{key} is never documented"


# ── Atom vocabulary is generated from the real registry ───────────────────────

class TestAtomVocabulary:
    def test_every_listed_atom_resolves(self):
        for name in re.findall(r"(\w+)\(", atom_vocabulary()):
            assert get_atom(name) is not None, f"{name} is not a real atom"

    def test_listed_args_are_the_atom_s_real_params(self):
        for name, args in re.findall(r"(\w+)\(([^)]*)\)", atom_vocabulary()):
            documented = [a.strip() for a in args.split(",") if a.strip()]
            real = [p.name for p in get_atom(name).params]
            assert documented == real, f"{name} args drifted from the registry"

    def test_subsystem_categories_are_excluded(self):
        # controller/hud/physics/input/model atoms are driven by "capabilities",
        # not by a beat sequence — keeping them out is what makes the section cheap.
        names = set(re.findall(r"(\w+)\(", atom_vocabulary()))
        assert "Jump" not in names and "MoveTo" in names

    def test_vocabulary_reaches_the_prompt_and_the_slot_is_consumed(self):
        text = block(max_optional=len(OPTIONAL_SECTIONS)).text
        assert "ScreenFade(" in text
        assert "<<ATOM_VOCABULARY>>" not in text


# ── Compose-only wiring guidance ──────────────────────────────────────────────

class TestWiringGuidance:
    def test_documented_kinds_are_all_real_micro_kinds(self):
        """A dep naming a kind subsystems_for never emits is silently dropped."""
        produced = set()

        class _E:
            def __init__(self, kind): self.kind = kind

        class _S:
            def __init__(self, type_): self.type, self.name = type_, type_

        feats = ["physics", "ui_toolkit", "input_system", "cinemachine", "navmesh",
                 "audio", "inventory", "dialogue", "npc_dialog", "vfx",
                 "scene_transition", "animation_rigging"]
        missions = [MissionSpec(id="m1", title="T", objectives=[
            {"id": "o1", "description": "d", "type": "reach"}])]
        trees = [AIBehaviorTree(id="b1", name="B",
                                tree=BehaviorTree(root="n1", nodes=[BTNode(id="n1")]))]
        for game_type in ("3D", "2D", "top_down"):
            for locomotion in ("walk", "drive"):
                produced |= {s.kind for s in subsystems_for(
                    game_type, feats, locomotion=locomotion,
                    user_prompt="voiced npc quest giver enemies boss",
                    scenes=["A", "B", "C"],
                    entities=[_E("enemy"), _E("npc"), _E("pickup")],
                    scripts=[_S("dialogue_manager")],
                    missions=missions, behavior_trees=trees)}

        assert set(WIRING_MICRO_KINDS) == produced

    def test_pre_wired_pairs_are_called_out(self):
        # These already get a named seam; re-declaring them only duplicates it.
        for pair in ("health↔hud", "input↔controller", "trigger↔saveload"):
            assert pair in GAMECONTEXT_SCHEMA_GUIDANCE

    def test_wiring_type_vocabulary_documented(self):
        assert "uses | triggers | references | contains" in GAMECONTEXT_SCHEMA_GUIDANCE


# ── Token budget ──────────────────────────────────────────────────────────────

class TestTokenBudget:
    """The multimodal ask has to stay bounded — that is the whole reason the
    optional sections are gated instead of all 13 fields shipping every time.

    Calibrated to the window this project is actually RUN at, not a guessed one.
    `lms load qwen3.5-9b --context-length 101678 --parallel 1 --estimate-only`
    reports **6.10 GiB on the 8 GB card — the same estimate as 32k**, so the KV
    cache is cheap for this model and ~100k of context is effectively free.

    The ceiling here previously assumed an 8k window and capped the prompt at
    5500 tokens. That is ~12× tighter than the hardware allows, and it was tight
    enough to reject a useful line of prompt AND to justify dropping half the
    multimodal ask (see render_multimodal_block's max_optional).

    Budget arithmetic at 101,678: a schema-fill answer is the whole project JSON,
    around 5k tokens (CodeBranchSim's was 19 KB). A 36k prompt still leaves ~60k
    for the answer. The guard exists to stop unbounded growth, not to ration a
    context that fits.
    """

    @staticmethod
    def _tokens(text: str) -> int:
        return len(text) // 4   # rough chars-per-token, good enough for a guard

    def test_the_block_stays_under_2500_tokens(self):
        """The default IS the uncapped block now, so one bound covers both: what
        the full optional set costs when a brief signals for all of it."""
        assert block().text == block(max_optional=len(OPTIONAL_SECTIONS)).text
        assert self._tokens(block().text) < 2500

    def test_the_whole_optional_set_costs_about_a_kilotoken(self):
        """The size the old cap was buying back. ~940 tokens against a 101,678
        window is 0.9% — and it was costing half the multimodal ask."""
        full = self._tokens(block().text)
        capped = self._tokens(block(max_optional=3).text)
        assert 0 < full - capped < 1500

    def test_a_configured_cap_still_shrinks_the_block(self):
        """config.yaml can re-impose one; the mechanism is intact."""
        assert self._tokens(block(max_optional=1).text) < self._tokens(block().text)

    def test_full_prompt_leaves_room_in_a_101k_window(self):
        rendered = full_prompt(block(), concept="x" * 2400)  # ~600-token concept
        assert self._tokens(rendered) < 36000

    def test_empty_block_leaves_the_base_prompt_intact(self):
        rendered = UNITY_SCHEMA_FILL_USER.format(concept="c", schema_hints="",
                                                 multimodal_block="")
        assert "MULTIMODAL" not in rendered
        assert rendered.rstrip().endswith("Output ONLY the JSON. No other text.")


# ── The prompt still formats cleanly ──────────────────────────────────────────

class TestPromptRendering:
    def test_no_unsubstituted_placeholders(self):
        rendered = full_prompt(block(max_optional=len(OPTIONAL_SECTIONS)))
        assert not re.search(r"\{[a-z_]+\}", rendered)

    def test_block_is_inserted_before_the_output_instruction(self):
        rendered = full_prompt(block())
        assert rendered.index("MULTIMODAL DESIGN LAYER") < rendered.index("Output ONLY the JSON")

    def test_json_braces_survive_formatting(self):
        # The base template escapes its literal braces as {{ }}; the block is a
        # substituted VALUE, so its single braces must come through untouched.
        rendered = full_prompt(block())
        assert '"show_health":true' in rendered
        assert "{{" not in rendered
