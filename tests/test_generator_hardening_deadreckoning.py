"""Regression coverage for confirmed-live generator bugs found in real
`unity-create`/`unity-validate --compose` runs of project "DeadReckoningProtocol"
(first batch: 15 C# compile errors; second batch, item 7 below: a real Unity
batchmode compile via `unity_cli.py e2e-verify` after the first batch's fixes
landed). Each test reproduces the exact authored-content shape that produced
one error (reconstructed from the emitted .cs, since the true Step 3.5 draft is
not persisted anywhere — see docs/model-pipeline-architecture.md on
`microgens`) and asserts the current pipeline no longer lets it through.

Two of the first batch's 15 errors are NOT covered here on purpose:
  - ExtractionProtocolObjectives.cs(163) `ReportProgress(1, 2)` — reproducing
    this exact shape against the current codebase shows it is ALREADY caught
    and neutralized (see `find_field_type_misuse`'s existing argument-type
    check); the live failure predates that check or ran against a different
    build. Nothing new to fix or test.
  - LightingDirector.cs(285) `KeyLight.distance` and VehicleChassis.cs(15)
    `Vector3.Lerp(vec, floatVar, ...)` — both are member/argument misuse
    against UnityEngine BCL types (Light, Vector3), not against a
    template-declared or schema-declared field. This system only checks
    identifiers/fields it has ground truth for (its own template or the
    schema's own extra_fields); validating arbitrary UnityEngine API surface
    would need a full API knowledge base, a different and much larger
    undertaking than the fixes below.

The second batch's real compile turned up six errors total, four of which
are now generator-hardened:
  - An `override void OnSpawned()` with no base method (CS0115) is covered by
    `test_spawner_invented_override_hook_is_stripped_not_left_illegal` in
    tests/test_micro_generators.py, alongside the sibling test that locks in
    Dialogue.cs.j2's one genuine override target (`Resolve`) so the fix
    cannot regress it.
  - `MonoBehaviour.TriggerEvent()` (CS1061) is item 7 below.
  - `Animator.CrossFade(_vehicleModeHash, modeName)` (CS1503, a wrong-typed
    second argument — originally filed under "BCL-API misuse, out of scope"
    alongside KeyLight.distance/Vector3.Lerp above, then narrowed: unlike
    those two, this is ONE specific, fully-enumerable method family, not
    arbitrary API surface, so a hand-verified check for just it — never a
    general Animator knowledge base — was in scope after all) is item 8.
  - `_objectives.Count`/`_objectives[i]` (property/indexer access, not a
    `.Method()` call — `find_field_type_misuse` originally only checked
    calls) and `CurrentStep += 1` (a read-only property whose body is a
    ternary, not the simple `Name => _field;` shape
    `_repair_readonly_property_assign` can rewrite — narrowed the same way
    Animator was: a NAME-only "is this read-only" check doesn't need the
    body's shape, only a value-preserving REWRITE does) are item 9.
The remaining two were fixed live in the project only (not generator-hardened
this pass — each is a genuinely uncertain root cause not worth patching
blind; see their own citations for why):
  - SaveSystemData.cs `string Length` — an author-declared extra_field with a
    plausible-sounding name and a type that happened to be wrong for how the
    author then used it (`Data.Length > 0`); no ground truth this system owns
    says a field named "Length" should be numeric.
  - StealthSystem.cs(17,27) `speed` referenced twice inside an already-
    neutralized `if (false)` block — the unbound-identifier pass caught the
    condition's own bad reference (`CurrentMode`) but not these two.

Fixing item 9 also surfaced a THIRD, unrelated bug, spun off as its own
follow-up task rather than fixed here: `template_scope_members`/
`template_field_types` (code_fields.py) do not see GameRules.cs.j2's
`_currentStepIndex` field at all (confirmed independent of every change in
this file — `'_currentStepIndex' in template_scope_members(...)` is False
while the sibling field `_objectives` a few lines later IS found), so a
perfectly legitimate `_currentStepIndex++;` reference in authored code gets
wrongly neutralized as "unbindable". Out of scope for item 9's own tests.
"""
from __future__ import annotations

from src.micro.audio import AudioPipeline, AudioSchema
from src.micro.camera import CameraRigPipeline, CameraRigSchema
from src.micro.code_fields import (
    CodeExtensibleSchema, CodeField, CodeMethod, lint_extensible,
)
from src.micro.controller import ControllerMode, ControllerPipeline, ControllerSchema
from src.micro.npc_dialog import NpcDialogPipeline, NpcDialogSchema


def _has_live_call(methods: list, needle: str) -> bool:
    """True if `needle` appears on any non-commented line of any method body —
    an AUDIT comment legitimately echoes the original text it neutralized, so
    a bare substring check would false-positive on the comment itself."""
    for m in methods:
        for line in (m.body or "").splitlines():
            if needle in line and not line.strip().startswith("//"):
                return True
    return False


def _cfg() -> dict:
    """config.yaml is gitignored and missing in a fresh CI checkout — mirrors
    the dummy_config pattern used throughout tests/test_controller_*.py."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {"unity_output_dir": "output/unity",
                           "unity_template_dir": "templates/unity"},
    }


# ── 1. Controller variant-alternative template scope leak (CS0103) ─────────
# PointerController.cs.j2's `ApplyHighlight` was falsely "in scope" for every
# OTHER controller variant, because the template-member scanner read every
# .cs.j2 in the folder regardless of which one the variant actually renders.

def test_pointer_only_method_not_in_scope_for_multi_mode_controller():
    pipe = ControllerPipeline(_cfg())
    schema = ControllerSchema(
        name="PlayerController", variant="multi_mode",
        modes=[ControllerMode(kind="on_foot"), ControllerMode(kind="flying")],
        extra_fields=[CodeField(name="_stealthAudioSource", type="AudioSource", default="null")],
        extra_methods=[CodeMethod(
            signature="private void OnStealthTakedown()",
            body="ApplyHighlight(_stealthAudioSource.gameObject);")],
    )
    fixed = pipe.default_fix(schema, "multi_mode")
    msgs = pipe.harden(fixed)
    assert any("ApplyHighlight" in m for m in msgs)
    # The method's only statement was the neutralized call, so it is dropped
    # entirely rather than shipped as an empty shell — either way, no LIVE
    # (non-commented) ApplyHighlight(...) call survives anywhere.
    assert not _has_live_call(fixed.extra_methods, "ApplyHighlight(")


# ── 2. Assignment to a get-only template property (CS0200) ─────────────────

def test_assignment_to_readonly_controller_property_rewritten_to_backing_field():
    pipe = ControllerPipeline(_cfg())
    schema = ControllerSchema(
        name="PlayerController", variant="multi_mode",
        modes=[ControllerMode(kind="on_foot"), ControllerMode(kind="flying")],
        extra_methods=[CodeMethod(
            signature="private void TryLandOnPad()",
            body="GearDown = true;\nFlapsDown = true;")],
    )
    fixed = pipe.default_fix(schema, "multi_mode")
    pipe.harden(fixed)   # _repair_readonly_property_assign runs inside harden(), not default_fix()
    body = fixed.extra_methods[0].body
    assert "_gearDown = true;" in body
    assert "_flapsDown = true;" in body
    assert "GearDown = true" not in body
    assert "FlapsDown = true" not in body


# ── 3. Vector tuple-literal default (CS0029) ────────────────────────────────

def test_vector3_tuple_literal_default_rewritten_to_constructor():
    pipe = CameraRigPipeline(_cfg())
    schema = CameraRigSchema(
        name="PlayerCamera", variant="follow",
        extra_fields=[CodeField(name="_offset", type="Vector3", default="(0f, 2f, 0f)")],
    )
    fixed = pipe.default_fix(schema, "follow")
    assert fixed.extra_fields[0].default == "new Vector3(0f, 2f, 0f)"


def test_vector2_tuple_literal_wrong_arity_is_dropped_not_guessed():
    pipe = CameraRigPipeline(_cfg())
    schema = CameraRigSchema(
        name="PlayerCamera", variant="follow",
        extra_fields=[CodeField(name="_pad", type="Vector2", default="(1f, 2f, 3f)")],
    )
    fixed = pipe.default_fix(schema, "follow")
    assert fixed.extra_fields[0].default is None


# ── 4. String field used as an AudioClip argument (CS1503) ─────────────────

def test_string_field_passed_to_playoneshot_retyped_to_audioclip():
    pipe = AudioPipeline(_cfg())
    schema = AudioSchema(
        name="AudioDirector", variant="3d_spatial",
        extra_fields=[CodeField(name="_exhaustClip", type="string", default='"Exhaust_Heavy"')],
        update_extra="if (_engineSource != null) _engineSource.PlayOneShot(_exhaustClip, 1f);",
    )
    fixed = pipe.default_fix(schema, "3d_spatial")
    field = fixed.extra_fields[0]
    assert field.type == "AudioClip"
    assert field.default == "null"


def test_string_field_not_passed_to_clip_call_stays_a_string():
    pipe = AudioPipeline(_cfg())
    schema = AudioSchema(
        name="AudioDirector", variant="3d_spatial",
        extra_fields=[CodeField(name="_label", type="string", default='"Engine"')],
    )
    fixed = pipe.default_fix(schema, "3d_spatial")
    assert fixed.extra_fields[0].type == "string"


# ── 5. Sibling-class field leaking into scope via a comment (CS0103) ───────
# DialogTrigger.cs.j2's OWN explanatory comment (that it does not render
# extras) contains the words "extra_fields"/"extra_methods"/"extra_usings",
# which a plain substring search over raw text mistook for a genuine
# injection site — folding DialogTrigger's `_player` field into scope for
# DialogPlayer's authored code, a completely different class.

def test_dialogtrigger_player_field_not_in_scope_for_dialogplayer():
    pipe = NpcDialogPipeline(_cfg())
    schema = NpcDialogSchema(
        name="Engineer", variant="quest_giver",
        extra_methods=[CodeMethod(
            signature="private void OnEngineerApproach(float radius)",
            body='if (_player != null && _player.CompareTag("Player")) { }')],
    )
    fixed = pipe.default_fix(schema, "quest_giver")
    msgs = pipe.harden(fixed)
    assert any("_player" in m for m in msgs)
    body = fixed.extra_methods[0].body if fixed.extra_methods else ""
    assert "_player.CompareTag" not in body


# ── 6. Member call on an `object`-typed field (CS1061) ──────────────────────
# A custom script's cross-reference to another custom script has no
# sanctioned typed pattern (GameContext.Get<T>() only covers fixed
# micro-generator types), so the LLM types the field `object` — which then
# has zero real members. Any `.Method()`/`.Property` on it beyond
# ToString/Equals/GetHashCode/GetType is unconditionally wrong.

class _DummyExtensible(CodeExtensibleSchema):
    pass


def test_method_call_on_object_typed_field_is_neutralized():
    s = _DummyExtensible(
        extra_fields=[CodeField(name="_vtolController", type="object", default="null")],
        extra_methods=[CodeMethod(
            signature="private void OnTakedownDetected()",
            body="if (_vtolController != null) {\n    _vtolController.RetractGear();\n}")],
    )
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    assert not _has_live_call(s.extra_methods, "_vtolController.RetractGear(")
    assert "AUDIT" in s.extra_methods[0].body


def test_object_typed_field_null_check_survives():
    """The neutralization must be surgical — `_vtolController != null` is a
    perfectly valid, always-legal use of an object-typed field and must not
    be swept away along with the invalid member call."""
    s = _DummyExtensible(
        extra_fields=[CodeField(name="_vtolController", type="object", default="null")],
        extra_methods=[CodeMethod(
            signature="private void OnTakedownDetected()",
            body="if (_vtolController != null) {\n    _vtolController.RetractGear();\n}")],
    )
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    assert "_vtolController != null" in s.extra_methods[0].body


# ── 7. Member call on a `MonoBehaviour`-typed field (CS1061) ────────────────
# Same shape as #6, one type up: when the unresolvable cross-script reference
# is instead typed `MonoBehaviour` (rather than `object`), _UNITY_FIELD_TYPE_
# SURFACE had no entry for it, so every check fell through silently — unlike
# `object`, which the object-branch above has always covered. Confirmed live,
# DeadReckoningProtocol EngineerEscort.cs(17): `[SerializeField] private
# MonoBehaviour _engineerDialogueManager;` then `_engineerDialogueManager.
# TriggerEvent("StartEscortDialogue");` — TriggerEvent is not a member of
# MonoBehaviour or anything it inherits from (CS1061).

def test_method_call_on_monobehaviour_typed_field_is_neutralized():
    s = _DummyExtensible(
        extra_fields=[CodeField(name="_engineerDialogueManager", type="MonoBehaviour",
                                default="null")],
        extra_methods=[CodeMethod(
            signature="public void StartEscort()",
            body='if (_engineerDialogueManager != null) {\n'
                 '    _engineerDialogueManager.TriggerEvent("StartEscortDialogue");\n}')],
    )
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    assert not _has_live_call(s.extra_methods, "_engineerDialogueManager.TriggerEvent(")
    assert "AUDIT" in s.extra_methods[0].body


# ── 8. Animator.CrossFade with a string second argument (CS1503) ───────────

def test_animator_crossfade_with_string_second_arg_is_neutralized():
    # `_anim` is the ANIMATOR TEMPLATE'S OWN field (Animator.cs.j2's `private
    # Animator _anim;`) — AnimatorPipeline.default_fix predates lint_extensible
    # and never read its own template's field types until now, so a misuse of
    # `_anim` went uncaught the same way #7's MonoBehaviour case did for a
    # SCHEMA-owned field. Confirmed live, DeadReckoningProtocol
    # PlayerAnimator.cs(109): `_anim.CrossFade(_vehicleModeHash, modeName)` —
    # `modeName` is this METHOD'S OWN string parameter, passed where
    # CrossFade's second argument (a transition duration) belongs.
    from src.micro.animator import AnimatorPipeline, AnimatorSchema
    pipe = AnimatorPipeline(_cfg())
    schema = AnimatorSchema(
        name="PlayerAnimator", variant="vehicle",
        extra_fields=[CodeField(name="_vehicleModeHash", type="int", default="0")],
        extra_methods=[CodeMethod(
            signature="public void SetVehicleMode(string modeName)",
            body="_anim.CrossFade(_vehicleModeHash, modeName);")],
    )
    fixed = pipe.default_fix(schema, "vehicle")
    assert not _has_live_call(
        fixed.extra_methods, "_anim.CrossFade(_vehicleModeHash, modeName)")
    assert "AUDIT" in fixed.extra_methods[0].body


def test_animator_crossfade_with_numeric_second_arg_survives():
    """The check must not over-fire on CrossFade's real, correct shape."""
    from src.micro.animator import AnimatorPipeline, AnimatorSchema
    pipe = AnimatorPipeline(_cfg())
    schema = AnimatorSchema(
        name="PlayerAnimator", variant="vehicle",
        extra_methods=[CodeMethod(
            signature="public void SetVehicleMode(string modeName)",
            body="_anim.CrossFade(modeName, 0.1f);")],
    )
    fixed = pipe.default_fix(schema, "vehicle")
    assert _has_live_call(fixed.extra_methods, "_anim.CrossFade(modeName, 0.1f)")


def test_monobehaviour_real_members_are_never_flagged():
    """The surface must not be so narrow it flags MonoBehaviour's OWN real
    API — GetComponent/StartCoroutine/CompareTag etc. are always legal."""
    s = _DummyExtensible(
        extra_fields=[CodeField(name="_relay", type="MonoBehaviour", default="null")],
        extra_methods=[CodeMethod(
            signature="private void Relay()",
            body='if (_relay != null) {\n'
                 '    _relay.CompareTag("Player");\n'
                 '    _relay.StartCoroutine(null);\n}')],
    )
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    body = s.extra_methods[0].body
    assert "AUDIT" not in body
    assert '_relay.CompareTag("Player")' in body
    assert "_relay.StartCoroutine(null)" in body


# ── 9. `_objectives.Count`/`_objectives[i]` and `CurrentStep += 1` on
# GameRules' own template fields (CS1061 / CS0200) ──────────────────────────
# `_objectives` is Rules.cs.j2's own `private MonoBehaviour _objectives;` (a
# duck-typed reflection binding to whichever objectives tracker the project
# has — see BindObjectives() — never a collection); `CurrentStep` is a
# ternary-bodied get-only property (`Finished || steps.Count == 0 ? null :
# steps[_step]`). Confirmed live, DeadReckoningProtocol GameRules.cs:
#     265: if (_currentStepIndex < _objectives.Count - 1) {
#     266:     CurrentStep += 1;
#     277: if (... && _currentStepIndex < _objectives.Count) {
#     278:     return _objectives[_currentStepIndex];
# `_objectives.Count`/`_objectives[i]` are bare property/indexer access, not
# a `.Method()` call — item 7's MonoBehaviour surface only ever checked
# calls. `CurrentStep += 1` also went undetected twice over: its ternary body
# has no single backing field `_repair_readonly_property_assign` could
# rewrite into, AND `_ASSIGN_TO_NAME_RE` only ever matched a bare `=`, so
# `+=` was invisible to that pass regardless of body shape.

def test_gamerules_objectives_property_and_index_misuse_is_neutralized():
    from src.micro.rules import RulesPipeline, RulesSchema
    pipe = RulesPipeline(_cfg())
    schema = RulesSchema(
        name="GameRules", variant="guided",
        extra_methods=[
            CodeMethod(
                signature="private bool HasMoreObjectives()",
                body="if (_objectives.Count > 0) return true;\nreturn false;"),
            CodeMethod(
                signature="private string PeekObjective()",
                body='if (_objectives.Count > 0) return _objectives[0];\nreturn "";'),
        ],
    )
    pipe.harden(schema)
    assert len(schema.extra_methods) == 2, "a real surviving return must not be dropped"
    assert not _has_live_call(schema.extra_methods, "_objectives.Count")
    assert not _has_live_call(schema.extra_methods, "_objectives[0]")
    assert "AUDIT" in schema.extra_methods[0].body
    assert "AUDIT" in schema.extra_methods[1].body
    assert 'return false;' in schema.extra_methods[0].body
    assert 'return "";' in schema.extra_methods[1].body


def test_gamerules_currentstep_assignment_is_neutralized():
    from src.micro.rules import RulesPipeline, RulesSchema
    pipe = RulesPipeline(_cfg())
    schema = RulesSchema(
        name="GameRules", variant="guided",
        extra_methods=[CodeMethod(
            signature="private void AdvanceManually()",
            body="if (Finished) { }\nelse CurrentStep += 1;")],
    )
    pipe.harden(schema)
    assert len(schema.extra_methods) == 1
    assert not _has_live_call(schema.extra_methods, "CurrentStep += 1")
    assert "AUDIT" in schema.extra_methods[0].body
    assert "is a read-only property" in schema.extra_methods[0].body


def test_gamerules_stepindex_and_finished_reads_survive():
    """The check must not over-fire on GameRules' own real, correct members —
    `_step`-adjacent reads (`StepIndex`, `Finished`) are always legal, and
    `_objectives == null` (a bare null check, not a method call/property/
    index) must survive untouched, same discipline as item 6's null-check
    test."""
    from src.micro.rules import RulesPipeline, RulesSchema
    pipe = RulesPipeline(_cfg())
    schema = RulesSchema(
        name="GameRules", variant="guided",
        extra_methods=[CodeMethod(
            signature="private string DebugLabel()",
            body='if (_objectives == null) return "none";\n'
                 'return Finished ? "done" : StepIndex.ToString();')],
    )
    pipe.harden(schema)
    body = schema.extra_methods[0].body
    assert "AUDIT" not in body
    assert "_objectives == null" in body
    assert "StepIndex.ToString()" in body
