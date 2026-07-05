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
