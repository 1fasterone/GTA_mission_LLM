"""
Tests for the generalized insertion-point / code-fragment system.

Covers: the CodeExtensibleSchema mixin across every Unity micro generator,
the shared lint pass, and the GTA mission pipeline's extra_init/extra_tick/
extra_methods slots. The point of the whole system is that LLM-authored code
lands in the schema (so Step 6 stays a pure function) — these tests assert the
fragments actually reach the rendered output and that bad fragments are cleaned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.code_fields import (
    CodeField, CodeMethod, CodeExtensibleSchema,
    lint_code_fragment, lint_usings, lint_fields, lint_method, lint_extensible,
    lint_hash_references, lint_member_references, lint_vector3_static_misuse,
    find_invalid_member_refs, lint_enum_references, find_invalid_enum_refs,
    lint_bare_task_static_misuse, _safe_to_comment_out,
    lint_function_call_ref_misuse, lint_world_get_out_param_misuse,
    lint_vector3_raw_call_arg, find_vector3_raw_call_args,
    lint_undeclared_catch_exception_var, lint_vector3_entity_prop_misuse,
    find_vector3_entity_prop_misuse,
)
from src.micro.camera   import CameraRigPipeline, CameraRigSchema
from src.micro.physics  import PhysicsPipeline,   PhysicsSchema
from src.micro.hud      import HUDPipeline,       HUDSchema
from src.micro.ai       import AIPipeline,        AISchema
from src.micro.audio    import AudioPipeline,     AudioSchema

from src.mission_schema import MissionSchema, MissionStep
from src.code_generator import CodeGenerator
from src.pipeline       import _programmatic_fix


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dummy_config():
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


# ── Lint primitives ──────────────────────────────────────────────────────────

def test_lint_rejects_python_fragment():
    assert lint_code_fragment("def foo(): pass", what="x") == ""


def test_lint_strips_md_fences():
    out = lint_code_fragment("```csharp\nint x = 1;\n```", what="x")
    assert "```" not in out and "int x = 1;" in out


def test_lint_balances_braces():
    out = lint_code_fragment("if (x) {", what="x")
    assert out.count("{") == out.count("}")


def test_lint_repairs_value_type_property_member_assign():
    # CS1612: mutating a member of a value-type property won't compile. The lint
    # pass must rewrite it to read-modify-write (hit live: GravityDrift.cs).
    out = lint_code_fragment("Physics.gravity.x += shiftX;", what="x")
    assert "Physics.gravity.x +=" not in out
    assert "var _v = Physics.gravity;" in out and "Physics.gravity = _v;" in out
    out2 = lint_code_fragment("transform.position.y = 3f;", what="x")
    assert "var _v = transform.position;" in out2 and "transform.position = _v;" in out2
    # A comparison must be left untouched (no false positive on ==).
    assert lint_code_fragment("bool b = Physics.gravity.x == 0f;", what="x") \
        == "bool b = Physics.gravity.x == 0f;"


def test_lint_hash_references_comments_out_invalid_native():
    """The exact real-world regression: an LLM invents SET_MISSION_FAILED /
    SET_MISSION_PASSED (neither exists in GTA.Native.Hash) -> CS0117 at
    compile time, confirmed live by the SHVDN3 audit tool."""
    body = (
        "if (_finishOrder == 0) {\n"
        "  Function.Call(Hash.SET_MISSION_FAILED, true);\n"
        "} else {\n"
        "  Function.Call(Hash.SET_MISSION_PASSED, true);\n"
        "}"
    )
    out = lint_hash_references(body, allowed=frozenset({"SET_ENTITY_AS_MISSION_ENTITY"}))
    # The original line text is preserved inside the "// AUDIT:" comment for
    # traceability, so check no ACTIVE (non-comment) line still calls the
    # invalid native, rather than a raw substring check.
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Hash.SET_MISSION_FAILED" in ln for ln in active_lines)
    assert not any("Hash.SET_MISSION_PASSED" in ln for ln in active_lines)
    assert "// AUDIT: removed invalid Hash member(s) SET_MISSION_FAILED" in out
    assert "// AUDIT: removed invalid Hash member(s) SET_MISSION_PASSED" in out
    assert out.count("{") == out.count("}")


def test_lint_hash_references_keeps_valid_native():
    body = 'Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);'
    out = lint_hash_references(body, allowed=frozenset({"PLAY_SOUND_FRONTEND"}))
    assert out == body


def test_lint_hash_references_noop_without_allowlist():
    """allowed=None (e.g. lookups/gta_hash_allowlist.json missing) must not touch the text."""
    body = "Function.Call(Hash.TOTALLY_MADE_UP, true);"
    assert lint_hash_references(body, allowed=None) == body


def test_lint_hash_references_skips_brace_mixed_line():
    """A line that opens/closes a block on the SAME line as the bad reference is left
    alone rather than risk unbalancing the rest of the fragment — e.g. a one-liner
    `if (x) Function.Call(Hash.BAD, ...);` has no braces so it's fine to comment, but
    a line mixing an unmatched brace with the reference must not be touched."""
    body = "if (x) { Function.Call(Hash.BAD_NATIVE, true); "  # unmatched: only opens
    out = lint_hash_references(body, allowed=frozenset())
    assert out == body   # untouched — compiler will surface CS0117 instead


def test_lint_hash_references_disables_control_flow_header_instead_of_commenting():
    """SAFETY REGRESSION TEST: a bare `if (cond)` header whose `{` opens on the NEXT
    line must NEVER be commented out — that would silently turn the guarded block
    into an always-executing bare block (compiles, but changes behavior). Instead
    the condition is swapped for `false`, which always disables the block safely."""
    body = (
        "if (Function.Call<bool>(Hash.INVENTED_CHECK, x))\n"
        "{\n"
        "    DoSomething();\n"
        "}"
    )
    out = lint_hash_references(body, allowed=frozenset())
    assert "if (false)" in out
    assert "Hash.INVENTED_CHECK" not in out.split("\n")[0].split("/*")[0]  # not in the live condition
    assert out.count("{") == out.count("}")
    # The block itself (and its brace) must be untouched — only the condition changed.
    assert "{\n    DoSomething();\n}" in out or "{" in out.splitlines()[1]


# ── Type-aware member reference checker (extends the Hash-native lint to
# invented C# members/methods on known SHVDN3 types) ────────────────────────

_FAKE_SURFACE = {
    "GTA.Ped": ["Position", "Heading", "IsAlive", "Task", "Delete", "CurrentVehicle"],
    "GTA.Vehicle": ["Position", "Speed", "Driver", "Delete", "Heading"],
    "GTA.World": ["CreatePed", "CreateVehicle", "CreateBlip", "GetAllPeds", "CreateProp"],
    "GTA.Game": ["Player", "IsKeyPressed", "GameTime", "LastFrameTime", "MaxWantedLevel"],
}


def test_find_invalid_member_refs_catches_chain_pattern():
    bad = find_invalid_member_refs(
        "Game.Player.Character.GetNearestVehicle(\"x\", 10);", surface=_FAKE_SURFACE)
    assert any(t == "GTA.Ped" and m == "GetNearestVehicle" for _pos, t, m in bad)


def test_find_invalid_member_refs_ignores_valid_member():
    bad = find_invalid_member_refs("Game.Player.Character.Position;", surface=_FAKE_SURFACE)
    assert bad == []


def test_find_invalid_member_refs_catches_static_world():
    bad = find_invalid_member_refs("World.SpawnVehicle(x);", surface=_FAKE_SURFACE)
    assert any(t == "GTA.World" and m == "SpawnVehicle" for _pos, t, m in bad)


def test_find_invalid_member_refs_var_declaration_inference():
    bad = find_invalid_member_refs(
        "Ped p = World.CreatePed(model, pos);\np.ClearTasks();", surface=_FAKE_SURFACE)
    assert any(t == "GTA.Ped" and m == "ClearTasks" for _pos, t, m in bad)


def test_find_invalid_member_refs_skips_ambiguous_var_name():
    """The same identifier bound to two different types in one fragment is
    genuinely ambiguous — skip rather than risk a false accusation."""
    bad = find_invalid_member_refs(
        "Ped p = World.CreatePed(m1, pos1);\nVehicle p = World.CreateVehicle(m2, pos2);\np.NotAMember();",
        surface=_FAKE_SURFACE,
    )
    assert bad == []


def test_find_invalid_member_refs_noop_on_empty_surface():
    assert find_invalid_member_refs("Game.Player.Character.Fake();", surface={}) == []
    assert find_invalid_member_refs("Game.Player.Character.Fake();", surface=None) == []


def test_lint_member_references_comments_out_invalid_member():
    body = "Game.Player.Character.SetPositionAndRotation(pos, rot);"
    out = lint_member_references(body, surface=_FAKE_SURFACE)
    assert "// AUDIT: removed invalid member reference(s) Ped.SetPositionAndRotation" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("SetPositionAndRotation" in ln for ln in active_lines)


def test_lint_member_references_keeps_valid_member():
    body = "Game.Player.Character.Delete();"
    assert lint_member_references(body, surface=_FAKE_SURFACE) == body


def test_lint_member_references_noop_without_surface():
    body = "Game.Player.Character.Fake();"
    assert lint_member_references(body, surface=None) == body
    assert lint_member_references(body, surface={}) == body


def test_lint_member_references_disables_control_flow_header():
    """Mirror of the real SkyHighPrisonInterception regression: an invented member
    inside a bare `if (cond)` header (brace on the next line) must be disabled via
    `if (false)`, never commented out (which would silently drop the condition)."""
    body = (
        'if (Game.Player.Character.IsInVehicle() '
        '&& Game.Player.Character.GetNearestVehicle("x", 15) == _targetVehicle)\n'
        "{\n"
        "    DoSomething();\n"
        "}"
    )
    out = lint_member_references(body, surface=_FAKE_SURFACE)
    assert "if (false)" in out
    assert out.count("{") == out.count("}")
    assert "DoSomething();" in out   # block body untouched


# ── Enum member reference checker (extends Hash/member lint to invented enum
# values, e.g. Weather.Rain — the real member is Weather.Raining) ────────────

_FAKE_ENUMS = {
    "Weather": ["Clear", "Raining", "ThunderStorm", "Foggy", "Overcast"],
    "BlipColor": ["White", "Red", "Green", "Blue", "Yellow"],
}


def test_find_invalid_enum_refs_catches_bad_member():
    bad = find_invalid_enum_refs("World.Weather = Weather.Rain;", enums=_FAKE_ENUMS)
    assert any(e == "Weather" and m == "Rain" for _pos, e, m in bad)


def test_find_invalid_enum_refs_ignores_valid_member():
    bad = find_invalid_enum_refs("World.Weather = Weather.Raining;", enums=_FAKE_ENUMS)
    assert bad == []


def test_find_invalid_enum_refs_no_type_inference_needed():
    """Unlike member checking, an enum name is unambiguous wherever it appears —
    no local declaration or chain pattern required to trust the check."""
    bad = find_invalid_enum_refs("var w = Weather.Rain;", enums=_FAKE_ENUMS)
    assert any(e == "Weather" and m == "Rain" for _pos, e, m in bad)


def test_find_invalid_enum_refs_noop_on_empty_or_missing():
    assert find_invalid_enum_refs("Weather.Rain;", enums={}) == []
    assert find_invalid_enum_refs("Weather.Rain;", enums=None) == []


def test_lint_enum_references_comments_out_invalid_value():
    body = "World.Weather = Weather.Rain;"
    out = lint_enum_references(body, enums=_FAKE_ENUMS)
    assert "// AUDIT: removed invalid enum reference(s) Weather.Rain" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Weather.Rain" in ln for ln in active_lines)


def test_lint_enum_references_keeps_valid_value():
    body = "World.Weather = Weather.Raining;"
    assert lint_enum_references(body, enums=_FAKE_ENUMS) == body


def test_lint_enum_references_noop_without_allowlist():
    body = "World.Weather = Weather.Rain;"
    assert lint_enum_references(body, enums=None) == body
    assert lint_enum_references(body, enums={}) == body


def test_lint_enum_references_disables_control_flow_header():
    body = (
        "if (World.Weather == Weather.Rain)\n"
        "{\n"
        "    DoSomething();\n"
        "}"
    )
    out = lint_enum_references(body, enums=_FAKE_ENUMS)
    assert "if (false)" in out
    assert out.count("{") == out.count("}")
    assert "DoSomething();" in out


def test_lint_enum_references_does_not_collide_with_hash_or_member_checks():
    """Mixed content: only the bad enum reference gets flagged, valid Hash and
    member references are untouched by this checker."""
    body = (
        'Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);\n'
        "Game.Player.Character.Delete();\n"
        "World.Weather = Weather.Rain;"
    )
    bad = find_invalid_enum_refs(body, enums=_FAKE_ENUMS)
    assert len(bad) == 1
    assert bad[0][1:] == ("Weather", "Rain")


# ── _safe_to_comment_out: trailing inline comment must not block neutralizing ──

def test_safe_to_comment_out_true_for_statement_with_trailing_comment():
    """Regression: a real corpus line (`Task.GoTo(...); // Initial nudge`) was
    wrongly treated as unsafe to neutralize because `_safe_to_comment_out` only
    checked the raw line, which ends in comment text, not `;`."""
    assert _safe_to_comment_out("Function.Call(Hash.BAD, true); // explanatory note") is True


def test_safe_to_comment_out_ignores_url_style_double_slash():
    """A `//` inside a string literal (no preceding space, e.g. a URL) must NOT
    be mistaken for a comment start."""
    assert _safe_to_comment_out('Function.Call(Hash.BAD, "http://example.com");') is True


def test_safe_to_comment_out_false_for_header_with_trailing_comment():
    assert _safe_to_comment_out("if (Hash.BAD) // comment") is False


# ── Bare Task.X() static misuse (Task is instance-only via ped.Task) ─────────

def test_lint_bare_task_static_misuse_comments_out_bad_call():
    """Real corpus line, pulled verbatim: LLMs frequently write bare Task.X()
    as if TaskInvoker were a static class — it only exists as ped.Task."""
    body = "Task.GoTo(ped, fleeTarget);"
    out = lint_bare_task_static_misuse(body)
    assert "// AUDIT: removed bare Task.X() static misuse" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Task.GoTo" in ln for ln in active_lines)


def test_lint_bare_task_static_misuse_handles_trailing_inline_comment():
    """The exact real corpus line that exposed the _safe_to_comment_out bug."""
    body = ("Task.GoTo(player.Position + new Vector3((float)(_rng.NextDouble() * 2), "
             "(float)(_rng.NextDouble() * 2), 0f)); // Initial nudge")
    out = lint_bare_task_static_misuse(body)
    assert out.startswith("// AUDIT:")


def test_lint_bare_task_static_misuse_keeps_valid_instance_call():
    body = "ped.Task.GoTo(pos);"
    assert lint_bare_task_static_misuse(body) == body


def test_lint_bare_task_static_misuse_keeps_valid_chain_call():
    body = "Game.Player.Character.Task.FightAgainst(target);"
    assert lint_bare_task_static_misuse(body) == body


def test_lint_bare_task_static_misuse_disables_control_flow_header():
    body = (
        "if (someCondition)\n"
        "{\n"
        "    Task.Wait(1500);\n"
        "}"
    )
    out = lint_bare_task_static_misuse(body)
    assert "if (someCondition)" in out  # header itself untouched — the misuse is inside the block
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Task.Wait" in ln for ln in active_lines)
    assert out.count("{") == out.count("}")


def test_lint_bare_task_static_misuse_noop_on_empty_text():
    assert lint_bare_task_static_misuse("") == ""


# ── Vector3.Distance/.Dot instance-call misuse (CS1501) ──────────────────────

def test_lint_vector3_static_misuse_comments_out_bad_call():
    body = "float d = a.Distance(b);"
    out = lint_vector3_static_misuse(body)
    assert "// AUDIT: removed Vector3.Distance/.Dot instance-call misuse" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any(".Distance(" in ln for ln in active_lines)


def test_lint_vector3_static_misuse_keeps_correct_static_call():
    body = "float d = Vector3.Distance(a, b);"
    assert lint_vector3_static_misuse(body) == body


def test_lint_vector3_static_misuse_keeps_distanceto_instance_method():
    """Entity.DistanceTo(pos) is a REAL instance method (different name) — must
    never be caught by the Distance/Dot-specific pattern."""
    body = "float d = Game.Player.Character.Position.DistanceTo(pos);"
    assert lint_vector3_static_misuse(body) == body


def test_lint_vector3_static_misuse_noop_on_empty_text():
    assert lint_vector3_static_misuse("") == ""


# ── Function.Call(ref ...) misuse (CS1502/CS1503 — no ref->InputArgument) ────

def test_lint_function_call_ref_misuse_comments_out_bad_call():
    body = "Function.Call(Hash.GET_GROUND_Z_FOR_3D_COORD, x, y, z, ref height);"
    out = lint_function_call_ref_misuse(body)
    assert "// AUDIT: removed Function.Call(ref ...) misuse" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Function.Call" in ln for ln in active_lines)


def test_lint_function_call_ref_misuse_keeps_valid_call():
    body = "Function.Call(Hash.GET_GROUND_Z_FOR_3D_COORD, x, y, z, oa, false);"
    assert lint_function_call_ref_misuse(body) == body


def test_lint_function_call_ref_misuse_noop_on_empty_text():
    assert lint_function_call_ref_misuse("") == ""


# ── World.GetX(out ...) misuse (CS1061/CS0117 — these return their result directly) ─

def test_lint_world_get_out_param_misuse_comments_out_bad_call():
    body = "World.GetNearbyPeds(pos, 10f, out Ped[] nearby);"
    out = lint_world_get_out_param_misuse(body)
    assert "// AUDIT: removed hallucinated `out` parameter" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("World.GetNearbyPeds" in ln for ln in active_lines)


def test_lint_world_get_out_param_misuse_keeps_valid_call():
    body = "Ped[] nearby = World.GetNearbyPeds(pos, 10f);"
    assert lint_world_get_out_param_misuse(body) == body


def test_lint_world_get_out_param_misuse_keeps_legitimate_out_method():
    """World.GetGroundHeight legitimately uses `out` — must never be flagged."""
    body = "World.GetGroundHeight(pos, out float height);"
    assert lint_world_get_out_param_misuse(body) == body


def test_lint_world_get_out_param_misuse_noop_on_empty_text():
    assert lint_world_get_out_param_misuse("") == ""


# ── Raw Vector3 passed to Function.Call (CS1503 — no Vector3->InputArgument) ──

def test_find_vector3_raw_call_args_catches_declared_variable():
    src = "Vector3 spawnPos = pos;\nFunction.Call(Hash.CREATE_VEHICLE, hash, spawnPos);"
    assert find_vector3_raw_call_args(src) == [1]


def test_find_vector3_raw_call_args_catches_position_property():
    src = "Function.Call(Hash.CREATE_VEHICLE, hash, ped.Position);"
    assert find_vector3_raw_call_args(src) == [0]


def test_find_vector3_raw_call_args_ignores_decomposed_floats():
    src = "Function.Call(Hash.CREATE_VEHICLE, hash, pos.X, pos.Y, pos.Z);"
    assert find_vector3_raw_call_args(src) == []


def test_lint_vector3_raw_call_arg_comments_out_bad_call():
    body = "Vector3 spawnPos = pos;\nFunction.Call(Hash.CREATE_VEHICLE, hash, spawnPos);"
    out = lint_vector3_raw_call_arg(body)
    assert "// AUDIT: removed Function.Call passing a raw Vector3 argument" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("Function.Call" in ln for ln in active_lines)


def test_lint_vector3_raw_call_arg_keeps_decomposed_call():
    body = "Function.Call(Hash.CREATE_VEHICLE, hash, pos.X, pos.Y, pos.Z);"
    assert lint_vector3_raw_call_arg(body) == body


def test_lint_vector3_raw_call_arg_noop_on_empty_text():
    assert lint_vector3_raw_call_arg("") == ""


# ── Entity-only property (.Position, .Rotation, ...) accessed on a Vector3 ───
# (CS1061 — live regression: SpawnOrigin.Position, SpawnOrigin already being
# a Vector3 declared by the mission template itself)

def test_find_vector3_entity_prop_misuse_catches_reserved_spawn_origin():
    src = "var pos = SpawnOrigin.Position + new Vector3(5f, 0f, 0f);"
    bad = find_vector3_entity_prop_misuse(src)
    assert bad == [(src.index("Position"), "SpawnOrigin", "Position")]


def test_find_vector3_entity_prop_misuse_catches_locally_declared_var():
    src = "Vector3 anchor = pos;\nvar p2 = anchor.Rotation;"
    bad = find_vector3_entity_prop_misuse(src)
    assert [(var, member) for _, var, member in bad] == [("anchor", "Rotation")]


def test_find_vector3_entity_prop_misuse_ignores_unrelated_identifier():
    """An identifier not confidently known to be Vector3 must never be
    flagged — this stays conservative like the other member checks."""
    src = "var pos = ped.Position + new Vector3(5f, 0f, 0f);"
    assert find_vector3_entity_prop_misuse(src) == []


def test_lint_vector3_entity_prop_misuse_comments_out_bad_call():
    """Live regression, pulled verbatim from a failed compile: SpawnOrigin is
    a Vector3 (declared in mission.cs.j2), not an Entity — it has no
    .Position member (CS1061)."""
    body = (
        'var engineer = World.CreatePed(new Model("s_m_indian_01"), '
        "SpawnOrigin.Position + new Vector3(5f, 0f, 0f)); _peds.Add(engineer);"
    )
    out = lint_vector3_entity_prop_misuse(body)
    assert "// AUDIT: removed Entity-only property access on a Vector3" in out
    active_lines = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    assert not any("SpawnOrigin" in ln for ln in active_lines)


def test_lint_vector3_entity_prop_misuse_keeps_correct_usage():
    body = "var pos = SpawnOrigin + new Vector3(5f, 0f, 0f);"
    assert lint_vector3_entity_prop_misuse(body) == body


def test_lint_vector3_entity_prop_misuse_keeps_real_entity_position():
    """Game.Player.Character is a Ped (Entity), not a Vector3 — .Position is
    a real, valid property there and must never be flagged."""
    body = "var pos = Game.Player.Character.Position;"
    assert lint_vector3_entity_prop_misuse(body) == body


def test_lint_vector3_entity_prop_misuse_noop_on_empty_text():
    assert lint_vector3_entity_prop_misuse("") == ""


# ── Undeclared catch (Exception) referencing bare `ex` (CS0103) ──────────────

def test_lint_undeclared_catch_exception_var_declares_missing_variable():
    body = (
        "try\n"
        "{\n"
        "    DoSomething();\n"
        "}\n"
        "catch (Exception)\n"
        "{\n"
        "    Notification.PostTicker(ex.Message, false);\n"
        "}"
    )
    out = lint_undeclared_catch_exception_var(body)
    assert "catch (Exception ex)" in out
    assert "ex.Message" in out


def test_lint_undeclared_catch_exception_var_leaves_unused_catch_alone():
    body = "try { DoSomething(); } catch (Exception) { Notification.PostTicker(\"failed\", false); }"
    assert lint_undeclared_catch_exception_var(body) == body


def test_lint_undeclared_catch_exception_var_leaves_already_declared_alone():
    body = "try { DoSomething(); } catch (Exception ex) { Notification.PostTicker(ex.Message, false); }"
    assert lint_undeclared_catch_exception_var(body) == body


def test_lint_undeclared_catch_exception_var_noop_on_empty_text():
    assert lint_undeclared_catch_exception_var("") == ""


def test_lint_declares_inline_out_params():
    # CS0103: the pre-C#7 raycast idiom `out hit` with no declaration. The lint
    # pass must declare it inline as `out var hit` (hit live: GrappleGunSystem.cs
    # in the TerminalHorizon project).
    out = lint_code_fragment(
        "if (Physics.Raycast(ray, out hit, 5f)) { var a = hit.transform; }", what="x")
    assert "out var hit" in out
    # Already-declared out param is left alone (no double declaration).
    assert lint_code_fragment("RaycastHit hit; Physics.Raycast(ray, out hit, 5f);",
                              what="x") == "RaycastHit hit; Physics.Raycast(ray, out hit, 5f);"
    # A field target (`_`-prefixed) must NOT be rewritten — out-to-field is valid.
    assert lint_code_fragment("Physics.Raycast(ray, out _hitField, 5f);", what="x") \
        == "Physics.Raycast(ray, out _hitField, 5f);"
    # Two raycasts sharing a name: first declares, second reuses it.
    two = lint_code_fragment(
        "Physics.Raycast(a, out hit, 5f); Physics.Raycast(b, out hit, 5f);", what="x")
    assert two == "Physics.Raycast(a, out var hit, 5f); Physics.Raycast(b, out hit, 5f);"


def test_lint_usings_dedupes_and_drops_template():
    out = lint_usings(["UnityEngine", "UnityEngine.UI", "using UnityEngine.UI;"],
                      already_in_template={"UnityEngine"})
    assert out == ["UnityEngine.UI"]


def test_lint_fields_drops_reserved():
    fields = [CodeField(name="moveSpeed", type="float"), CodeField(name="custom", type="int")]
    out = lint_fields(fields, reserved={"movespeed"})
    assert [f.name for f in out] == ["custom"]


def test_codefield_coerces_nonstring_default():
    """Regression: models sometimes emit a numeric/bool default (e.g. 0 instead
    of "0"). CodeField must coerce to str rather than raising ValidationError."""
    assert CodeField(name="score", type="int", default=0).default == "0"
    assert CodeField(name="rate", type="float", default=1.5).default == "1.5"
    assert CodeField(name="flag", type="bool", default=True).default == "true"
    assert CodeField(name="flag2", type="bool", default=False).default == "false"
    assert CodeField(name="empty", type="int").default is None


def test_lint_method_drops_python_body():
    m = CodeMethod(signature="void F()", body="self.x = 1")
    assert lint_method(m, taken_names=set()) is None


def test_lint_extensible_cleans_in_place():
    class S(CodeExtensibleSchema):
        pass
    s = S(extra_usings=["UnityEngine"], awake_extra="print('hi')")
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"})
    assert s.extra_usings == []          # template-dup dropped
    assert s.awake_extra == ""           # python rejected


# ── Mixin inheritance across all Unity micro generators ──────────────────────

@pytest.mark.parametrize("schema_cls", [
    CameraRigSchema, PhysicsSchema, HUDSchema, AISchema, AudioSchema,
])
def test_unity_schemas_inherit_extensible(schema_cls):
    assert issubclass(schema_cls, CodeExtensibleSchema)
    inst = schema_cls()
    for field in ("extra_usings", "extra_fields", "awake_extra",
                  "update_extra", "fixed_update_extra", "extra_methods"):
        assert hasattr(inst, field)


# ── End-to-end fragment injection for each Unity generator ───────────────────

_UNITY_CASES = [
    (CameraRigPipeline, CameraRigSchema, "follow",            "TCam.cs"),
    (PhysicsPipeline,   PhysicsSchema,   "rigidbody_3d",      "TBody.cs"),
    (HUDPipeline,       HUDSchema,       "health_bar",        "THud.cs"),
    (AIPipeline,        AISchema,        "chase_player",      "TAi.cs"),
]


@pytest.mark.parametrize("Pipe,Schema,variant,mainfile", _UNITY_CASES)
def test_unity_fragment_injection(tmp_path, dummy_config, Pipe, Schema, variant, mainfile):
    name = mainfile.replace(".cs", "")
    p = Pipe(dummy_config)
    s = Schema(
        name=name, variant=variant,
        extra_usings=["UnityEngine.Events"],
        extra_fields=[CodeField(name="customVal", type="float", default="1f", header="Custom")],
        awake_extra="customVal = 2f;",
        update_extra="// frame logic\nif (customVal > 0f) customVal -= Time.deltaTime;",
        fixed_update_extra="customVal += 0.01f;",
        extra_methods=[CodeMethod(signature="public void Ping()", body='Debug.Log("ping");')],
    )
    s = p.default_fix(s, variant)
    p.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / mainfile).read_text(encoding="utf-8")
    assert "using UnityEngine.Events;" in cs
    assert "customVal" in cs
    assert "public void Ping()" in cs
    assert "private void FixedUpdate()" in cs


def test_audio_fragment_injection(tmp_path, dummy_config):
    p = AudioPipeline(dummy_config)
    s = AudioSchema(
        name="TAudio", variant="categorical_mixer",
        extra_usings=["UnityEngine.Events"],
        extra_fields=[CodeField(name="masterDuck", type="float", default="0.5f")],
        awake_extra="masterDuck = 1f;",
        extra_methods=[CodeMethod(signature="public void Duck()", body="masterDuck = 0.3f;")],
    )
    s = p.default_fix(s, "categorical_mixer")
    p.write_files(s, "categorical_mixer", tmp_path)
    cs = (tmp_path / "Scripts" / "TAudioAudioManager.cs").read_text(encoding="utf-8")
    assert "masterDuck" in cs and "public void Duck()" in cs


def test_unity_fragment_lint_drops_bad_input(tmp_path, dummy_config):
    p = CameraRigPipeline(dummy_config)
    s = CameraRigSchema(
        name="TClean", variant="follow",
        extra_fields=[CodeField(name="smoothTime", type="float")],   # reserved → dropped
        awake_extra="self.x = 1",                                     # python → dropped
    )
    s = p.default_fix(s, "follow")
    assert s.extra_fields == []
    assert s.awake_extra == ""


# ── GTA mission insertion points ─────────────────────────────────────────────

def _mission(**extra):
    return MissionSchema(
        mission_name="Test Mission", location_name="airport",
        steps=[MissionStep(type="go_to", label="Drive")],
        **extra,
    )


def test_gta_mission_injects_fragments():
    s = _mission(
        extra_usings=["GTA.UI"],
        extra_fields=[CodeField(name="_oxygen", type="float", default="100f", tooltip="drains")],
        extra_init="_oxygen = 100f;",
        extra_tick="if (_state == State.Active) _oxygen -= Game.LastFrameTime;",
        extra_methods=[CodeMethod(signature="private void Refill(float a)", body="_oxygen += a;")],
    )
    s = _programmatic_fix(s)
    code = CodeGenerator("templates/mission.cs.j2").generate(s)
    assert "private float _oxygen = 100f;" in code
    assert "_oxygen = 100f;" in code                     # extra_init
    assert "_oxygen -= Game.LastFrameTime;" in code      # extra_tick
    assert "private void Refill(float a)" in code         # extra_methods


def test_gta_mission_empty_extras_no_injection():
    s = _programmatic_fix(_mission())
    code = CodeGenerator("templates/mission.cs.j2").generate(s)
    assert "prompt-specific" not in code
    assert "Mission-specific state" not in code


def test_gta_lint_drops_reserved_and_python():
    s = _mission(
        extra_fields=[CodeField(name="_state", type="int"),     # reserved → dropped
                      CodeField(name="_custom", type="int")],   # kept
        extra_init="import os",                                 # python → dropped
    )
    s = _programmatic_fix(s)
    names = [f.name for f in s.extra_fields]
    assert names == ["_custom"]
    assert s.extra_init == ""


def test_gta_lint_drops_template_using():
    s = _mission(extra_usings=["GTA", "MyMod.Helpers"])
    s = _programmatic_fix(s)
    assert s.extra_usings == ["MyMod.Helpers"]   # "GTA" already in template


def test_gta_lint_neutralizes_invented_hash_native():
    """End-to-end: the actual RunwayRumbleTheCrowdSChoice.3.cs regression caught by
    the SHVDN3 audit tool (rule 21-Y) — an LLM-authored extra_method that invents
    Hash.SET_MISSION_FAILED / Hash.SET_MISSION_PASSED must not reach compiled output."""
    s = _mission(
        extra_methods=[CodeMethod(
            signature="private void _EvaluateFinishOrder()",
            body=(
                "if (_finishOrder == 0) {\n"
                "  Function.Call(Hash.SET_MISSION_FAILED, true);\n"
                "} else {\n"
                "  Function.Call(Hash.SET_MISSION_PASSED, true);\n"
                "}"
            ),
        )],
    )
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("Hash.SET_MISSION_FAILED" in ln for ln in active_lines)
    assert not any("Hash.SET_MISSION_PASSED" in ln for ln in active_lines)
    assert "_EvaluateFinishOrder" in cs   # the method itself still lands
    assert cs.count("{") == cs.count("}")
    assert cs.count("(") == cs.count(")")


def test_gta_lint_keeps_real_native_in_extras():
    s = _mission(extra_tick='Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);')
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "Function.Call(Hash.PLAY_SOUND_FRONTEND" in cs


def test_gta_lint_neutralizes_invented_members_and_vector3_misuse():
    """End-to-end: the actual SkyHighPrisonInterception.3.cs regression caught by
    the SHVDN3 audit tool (rules 21-Member + 22-F) — an LLM-authored extra_tick
    that invents Ped.GetNearestVehicle / Ped.SetPositionAndRotation, and misuses
    Vector3.Distance as an instance method, must not reach compiled output. Uses
    the REAL lookups/gta_api_surface.json (via _programmatic_fix), not a mock."""
    s = _mission(
        extra_tick=(
            'if (Game.Player.Character.IsInVehicle() '
            '&& Game.Player.Character.GetNearestVehicle("armored_prison_transport", 15) == _targetVehicle)\n'
            "{\n"
            "    float dist = Game.Player.Character.Position.Distance(_landingZone);\n"
            "    if (dist < 3f)\n"
            "    {\n"
            "        Game.Player.Character.SetPositionAndRotation(_landingZone, new GTA.Math.Quaternion(0, 0, 1));\n"
            "    }\n"
            "}"
        ),
    )
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("SetPositionAndRotation" in ln for ln in active_lines)
    assert not any("Position.Distance(" in ln for ln in active_lines)
    # The GetNearestVehicle header can't be commented (brace on next line) —
    # it must be disabled via `if (false)` instead, never left calling the
    # invented member live.
    assert not any("GetNearestVehicle" in ln and "if (false)" not in ln for ln in active_lines)
    assert "if (false)" in cs
    assert cs.count("{") == cs.count("}")
    assert cs.count("(") == cs.count(")")


def test_gta_lint_keeps_real_member_and_valid_vector3_call():
    s = _mission(extra_tick=(
        "Game.Player.Character.Delete();\n"
        "float d = Vector3.Distance(a, b);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "Game.Player.Character.Delete();" in cs
    assert "Vector3.Distance(a, b);" in cs


def test_gta_lint_neutralizes_invented_enum_value():
    """End-to-end using the REAL lookups/gta_enum_allowlist.json (via
    _programmatic_fix), not a mock. Corpus-mined regression: 133 occurrences of
    exactly this shape (e.g. Weather.Rain, the real member is Weather.Raining)
    across a 1191-file independent dataset — the 4th most common real failure."""
    s = _mission(extra_tick="World.Weather = Weather.Rain;")
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("Weather.Rain" in ln for ln in active_lines)
    assert "// AUDIT: removed invalid enum reference(s) Weather.Rain" in cs
    assert cs.count("{") == cs.count("}")


def test_gta_lint_keeps_real_enum_value():
    s = _mission(extra_tick="World.Weather = Weather.Raining;")
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "World.Weather = Weather.Raining;" in cs


def test_gta_lint_neutralizes_bare_task_static_misuse():
    """Corpus-mined regression: bare Task.X() (43 occurrences across a 1191-file
    independent dataset) — Task only exists as ped.Task, never as a static
    class. Real corpus line, pulled verbatim (with its trailing inline
    comment, which exposed a _safe_to_comment_out bug during development)."""
    s = _mission(extra_tick=(
        "Task.GoTo(player.Position + new Vector3((float)(_rng.NextDouble() * 2), "
        "(float)(_rng.NextDouble() * 2), 0f)); // Initial nudge"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("Task.GoTo" in ln for ln in active_lines)
    assert "// AUDIT: removed bare Task.X() static misuse" in cs
    assert cs.count("{") == cs.count("}")


def test_gta_lint_keeps_valid_task_instance_call():
    s = _mission(extra_tick="Game.Player.Character.Task.FightAgainst(target);")
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "Game.Player.Character.Task.FightAgainst(target);" in cs


def test_gta_lint_neutralizes_function_call_ref_misuse():
    """Corpus-mined regression: `ref x` passed into Function.Call (26 occurrences
    across a 1191-file independent dataset) — InputArgument has no ref conversion."""
    s = _mission(extra_tick=(
        "Function.Call(Hash.GET_GROUND_Z_FOR_3D_COORD, x, y, z, ref height);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("Function.Call(Hash.GET_GROUND_Z_FOR_3D_COORD" in ln for ln in active_lines)
    assert "// AUDIT: removed Function.Call(ref ...) misuse" in cs


def test_gta_lint_neutralizes_world_get_out_param_misuse():
    """Corpus-mined regression: hallucinated `out` param on World.GetNearbyPeds
    (22 occurrences across a 1191-file independent dataset) — it returns the
    array directly, no out overload exists."""
    s = _mission(extra_tick=(
        "World.GetNearbyPeds(Game.Player.Character.Position, 10f, out Ped[] nearby);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("World.GetNearbyPeds" in ln for ln in active_lines)
    assert "// AUDIT: removed hallucinated `out` parameter" in cs


def test_gta_lint_neutralizes_vector3_raw_call_arg():
    """Corpus-mined regression: a whole Vector3 passed to Function.Call (15
    occurrences across a 1191-file independent dataset) — InputArgument has no
    Vector3 conversion."""
    s = _mission(extra_tick=(
        "Vector3 spawnPos = Game.Player.Character.Position;\n"
        "Function.Call(Hash.CREATE_VEHICLE, (uint)VehicleHash.Sultan, spawnPos);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("Function.Call(Hash.CREATE_VEHICLE" in ln for ln in active_lines)
    assert "// AUDIT: removed Function.Call passing a raw Vector3 argument" in cs


def test_gta_lint_neutralizes_vector3_entity_prop_misuse():
    """Live regression pulled from a real ScriptHookVDotNet.log compile
    failure (AbyssalDescent.recompiled.3.cs, 6x CS1061 'Vector3 does not
    contain a definition for Position'): the LLM-authored extra_init wrote
    `SpawnOrigin.Position + new Vector3(...)`, but SpawnOrigin is already a
    Vector3 (declared by the mission template itself) — Vector3 has no
    .Position member."""
    s = _mission(extra_init=(
        'var engineer = World.CreatePed(new Model("s_m_indian_01"), '
        "SpawnOrigin.Position + new Vector3(5f, 0f, 0f)); _peds.Add(engineer);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    active_lines = [ln for ln in cs.splitlines() if not ln.strip().startswith("//")]
    assert not any("SpawnOrigin.Position" in ln for ln in active_lines)
    assert "// AUDIT: removed Entity-only property access on a Vector3" in cs


def test_gta_lint_keeps_correct_spawn_origin_usage():
    s = _mission(extra_init=(
        'var engineer = World.CreatePed(new Model("s_m_indian_01"), '
        "SpawnOrigin + new Vector3(5f, 0f, 0f)); _peds.Add(engineer);"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert 'SpawnOrigin + new Vector3(5f, 0f, 0f)); _peds.Add(engineer);' in cs


def test_gta_lint_declares_missing_catch_exception_var():
    """Corpus-mined regression: `catch (Exception)` referencing a bare `ex`
    inside the block (42 occurrences across a 1191-file independent dataset) —
    `ex` is out of scope without a declared variable name (CS0103)."""
    s = _mission(extra_tick=(
        "try\n"
        "{\n"
        "    DoRiskyThing();\n"
        "}\n"
        "catch (Exception)\n"
        "{\n"
        "    Notification.PostTicker(ex.Message, false);\n"
        "}"
    ))
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "catch (Exception ex)" in cs
    assert "ex.Message" in cs


# ── B1 regression: the prompt's few-shot shape renders into the mission ────────

def test_gta_fewshot_wave_spawner_lands_in_script():
    """Mirror of the SCHEMA_FILL_USER few-shot ('cartel sends a chase car every
    30s'): proves the extras channel carries a full concept — fields + init +
    tick + method — into the .3.cs. This is the cookie-cutter fix's contract."""
    s = _mission(
        extra_fields=[CodeField(name="_nextWaveAt", type="DateTime",
                                default="DateTime.MinValue")],
        extra_init="_nextWaveAt = DateTime.UtcNow.AddSeconds(30);",
        extra_tick=("if (_state == State.Active && DateTime.UtcNow >= _nextWaveAt) "
                    "{ SpawnChaseWave(); _nextWaveAt = DateTime.UtcNow.AddSeconds(30); }"),
        extra_methods=[CodeMethod(
            signature="private void SpawnChaseWave()",
            body=('var car = World.CreateVehicle(new Model("baller"), '
                  'Game.Player.Character.Position + new Vector3(40f, 40f, 0f));\n'
                  'if (car != null) _vehicles.Add(car);'),
        )],
    )
    s = _programmatic_fix(s)
    cs = CodeGenerator().generate(s)
    assert "private DateTime _nextWaveAt = DateTime.MinValue;" in cs
    assert "_nextWaveAt = DateTime.UtcNow.AddSeconds(30);" in cs
    assert "SpawnChaseWave();" in cs
    assert "private void SpawnChaseWave()" in cs
    assert "_vehicles.Add(car);" in cs
    assert cs.count("{") == cs.count("}")


# ── CodeMethod alias tolerance (LLM shape drift) ──────────────────────────────

def test_codemethod_accepts_name_alias_and_synthesises_signature():
    """LLMs frequently emit {name, body} instead of {signature, body}. A bare
    method name must become a valid C# signature, not abort schema-fill."""
    m = CodeMethod.model_validate({"name": "OnAirEmpty", "body": "Die();"})
    assert m.signature == "private void OnAirEmpty()"
    assert m.body == "Die();"


def test_codemethod_accepts_code_alias_and_keeps_full_signature():
    m = CodeMethod.model_validate({"signature": "public int Foo(int x)", "code": "return x;"})
    assert m.signature == "public int Foo(int x)"   # full signature untouched
    assert m.body == "return x;"


def test_codemethod_valid_input_passes_through_unchanged():
    m = CodeMethod(signature="void F()", body="x();")
    assert m.signature == "void F()" and m.body == "x();"


# ── Fix #1: custom scripts that use `rb` get a Rigidbody auto-declared ─────────

def _custom_script_cs(name: str, update_extra: str) -> str:
    from src.unity_schema import UnitySchema
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema.model_validate({
        "project_name": name, "game_type": "3D", "platform": "pc",
        "render_pipeline": "URP", "scenes": ["Main"],
        "scripts": [{"name": "Subj", "type": "custom", "attach_to": "systems",
                     "description": "x", "update_extra": update_extra}],
    })
    return UnityProjectGenerator().generate(schema, {})[f"{name}/Assets/Scripts/Subj.cs"]


def test_custom_script_autodeclares_rigidbody_when_rb_used():
    """Regression for the BuoyancySystem CS0103: `rb.AddForce(...)` with no rb
    field must not break compilation — the generator declares the Rigidbody."""
    cs = _custom_script_cs("RbProof", "rb.AddForce(Vector3.up * 9.81f, ForceMode.Acceleration);")
    assert "private Rigidbody rb;" in cs
    assert "[RequireComponent(typeof(Rigidbody))]" in cs
    assert "rb = GetComponent<Rigidbody>();" in cs
    assert cs.count("{") == cs.count("}")


def test_custom_script_no_rigidbody_when_rb_unused():
    cs = _custom_script_cs("NoRbProof", "transform.Rotate(Vector3.up * Time.deltaTime);")
    assert "Rigidbody" not in cs
