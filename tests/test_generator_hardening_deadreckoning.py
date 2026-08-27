"""Regression coverage for six confirmed-live generator bugs found in a real
`unity-create` run (project "DeadReckoningProtocol") that shipped 15 C# compile
errors. Each test reproduces the exact authored-content shape that produced one
error (reconstructed from the emitted .cs, since the true Step 3.5 draft is not
persisted anywhere — see docs/model-pipeline-architecture.md on `microgens`)
and asserts the current pipeline no longer lets it through.

Two of the original 15 errors are NOT covered here on purpose:
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
