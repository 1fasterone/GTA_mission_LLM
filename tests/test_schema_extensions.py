"""Schema Extensions — the LLM's sandboxed field-invention namespace.

See docs/SCHEMA_EXTENSIONS.md and CLAUDE.md "Schema Extensions". Two layers
covered here:

  1. `apply_extensions_safe_block` in isolation — the safe-block contract
     itself (no pruning, no clamping, no renaming; a collision is a loud
     abstain recorded in schema.meta, never a silent fix). This is
     deliberately the OPPOSITE contract from `lint_fields`/`lint_extensible`,
     so every assertion here has a matching "and lint_fields WOULD have
     changed this" contrast where it matters.
  2. End-to-end through two real pipelines: `controller` (hand-rolled
     extra_*/extensions_*, not CodeExtensibleSchema) and `health`
     (CodeExtensibleSchema-based, the ~18-generator cohort) — proving the
     fixer wiring and the Jinja rendering both actually fire, not just that
     the Python function works standalone.
"""
from __future__ import annotations

from pathlib import Path

from src.csharp_syntax_gate import check_files
from src.micro.code_fields import CodeField, CodeMethod, apply_extensions_safe_block
from src.micro.controller import ControllerPipeline, ControllerSchema
from src.micro.health import HealthPipeline, HealthSchema

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    """Mirrors the dummy_config pattern in test_controller_vehicle_bike.py —
    config.yaml is gitignored and missing in a fresh CI checkout."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


# ── apply_extensions_safe_block, in isolation ───────────────────────────────

class _Bare:
    """Minimal stand-in satisfying _ExtensionsLike without any other schema
    machinery — isolates the function from any one pipeline's reserved set."""
    model_fields: dict = {}

    def __init__(self):
        self.extensions_fields: list[CodeField] = []
        self.extensions_methods: list[CodeMethod] = []
        self.extensions_metadata: dict = {}
        self.extra_fields: list[CodeField] = []
        self.extra_methods: list[CodeMethod] = []
        self.meta: dict = {}


def test_non_colliding_field_and_method_pass_through_unchanged():
    s = _Bare()
    s.extensions_fields = [CodeField(name="questFlag", type="bool", default="false")]
    s.extensions_methods = [CodeMethod(signature="private void OnQuestDone()", body="questFlag = true;")]
    apply_extensions_safe_block(s, reserved={"movespeed"})
    assert [f.name for f in s.extensions_fields] == ["questFlag"]
    assert len(s.extensions_methods) == 1
    assert "extensions_conflicts" not in s.meta


def test_collision_with_reserved_name_is_excluded_and_recorded_loudly():
    s = _Bare()
    s.extensions_fields = [CodeField(name="moveSpeed", type="float", default="1f")]
    apply_extensions_safe_block(s, reserved={"movespeed"})
    assert s.extensions_fields == []
    assert any("moveSpeed" in c for c in s.meta["extensions_conflicts"])


def test_collision_with_sibling_extra_field_is_excluded():
    s = _Bare()
    s.extra_fields = [CodeField(name="oxygen", type="float", default="0f")]
    s.extensions_fields = [CodeField(name="oxygen", type="float", default="1f")]
    apply_extensions_safe_block(s, reserved=set())
    assert s.extensions_fields == []
    assert "extensions_conflicts" in s.meta


def test_duplicate_within_extensions_fields_keeps_first_excludes_second():
    s = _Bare()
    s.extensions_fields = [
        CodeField(name="grip", type="float", default="1f"),
        CodeField(name="Grip", type="float", default="2f"),   # case-insensitive dupe
    ]
    apply_extensions_safe_block(s, reserved=set())
    assert [f.name for f in s.extensions_fields] == ["grip"]
    assert len(s.meta["extensions_conflicts"]) == 1


def test_method_colliding_with_kept_field_name_is_excluded():
    s = _Bare()
    s.extensions_fields = [CodeField(name="cooldown", type="float", default="0f")]
    s.extensions_methods = [CodeMethod(signature="private void cooldown()", body="return;")]
    apply_extensions_safe_block(s, reserved=set())
    assert s.extensions_fields != []
    assert s.extensions_methods == []
    assert any("cooldown" in c for c in s.meta["extensions_conflicts"])


def test_no_clamping_unlike_lint_fields():
    """A type-mismatched default (float field, int-looking literal that would
    read fine, but see the contrast below) survives untouched — the safe
    block does not call sanitize_default the way lint_fields does."""
    s = _Bare()
    # "2i" is not a valid C# suffix (CS1002) — lint_fields' sanitize_default
    # would repair/strip this; apply_extensions_safe_block must not.
    s.extensions_fields = [CodeField(name="odd", type="int", default="2i")]
    apply_extensions_safe_block(s, reserved=set())
    assert s.extensions_fields[0].default == "2i"   # untouched, not repaired


# ── End-to-end: controller (hand-rolled extra_*/extensions_*) ──────────────

def test_controller_extensions_field_survives_default_fix_and_renders():
    pipe = ControllerPipeline(_cfg())
    schema = ControllerSchema(
        name="TestCtrl", variant="third_person",
        extensions_fields=[CodeField(name="staminaMeter", type="float",
                                     default="100f", header="Experimental")],
        extensions_methods=[CodeMethod(signature="private void DrainStamina()",
                                       body="staminaMeter -= 1f;")],
    )
    fixed = pipe.default_fix(schema, "third_person")
    assert [f.name for f in fixed.extensions_fields] == ["staminaMeter"]
    assert len(fixed.extensions_methods) == 1

    env = pipe.jinja()
    out = env.get_template("Controller.cs.j2").render(s=fixed, variant="third_person")
    assert "staminaMeter" in out
    assert "DrainStamina" in out
    assert "EXPERIMENTAL EXTENSIONS" in out
    assert out.count("{") == out.count("}")


def test_controller_extensions_field_colliding_with_reserved_name_is_dropped():
    pipe = ControllerPipeline(_cfg())
    schema = ControllerSchema(
        name="TestCtrl", variant="third_person",
        extensions_fields=[CodeField(name="moveSpeed", type="float", default="99f")],
    )
    fixed = pipe.default_fix(schema, "third_person")
    assert fixed.extensions_fields == []
    assert "extensions_conflicts" in fixed.meta

    env = pipe.jinja()
    out = env.get_template("Controller.cs.j2").render(s=fixed, variant="third_person")
    # Only ever one moveSpeed declaration — the dropped extension never
    # doubled the baseline field (would be CS0102 if it had).
    assert out.count("float moveSpeed") <= 1


def test_controller_with_extensions_passes_the_real_csharp_syntax_gate(tmp_path):
    pipe = ControllerPipeline(_cfg())
    schema = ControllerSchema(
        name="TestCtrl", variant="third_person",
        extensions_fields=[CodeField(name="questFlag", type="bool", default="false")],
        extensions_methods=[CodeMethod(signature="private bool IsQuestDone()",
                                       body="return questFlag;")],
    )
    fixed = pipe.default_fix(schema, "third_person")
    env = pipe.jinja()
    out = env.get_template("Controller.cs.j2").render(s=fixed, variant="third_person")
    path = tmp_path / "TestCtrl.cs"
    path.write_text(out, encoding="utf-8")
    result = check_files([str(path)])
    if result.skipped_reason:
        return  # no .NET SDK on this machine
    assert not result.diagnostics, result.diagnostics


# ── End-to-end: a CodeExtensibleSchema-based generator (health) ────────────

def test_health_extensions_field_survives_and_renders():
    pipe = HealthPipeline(_cfg())
    schema = HealthSchema(
        name="TestHealth", variant="simple",
        extensions_fields=[CodeField(name="poisonStacks", type="int", default="0")],
    )
    fixed = pipe.default_fix(schema, "simple")
    assert [f.name for f in fixed.extensions_fields] == ["poisonStacks"]

    env = pipe.jinja()
    out = env.get_template("Health.cs.j2").render(s=fixed, variant="simple")
    assert "poisonStacks" in out
    assert out.count("{") == out.count("}")


def test_health_extensions_field_colliding_with_reserved_name_is_dropped():
    pipe = HealthPipeline(_cfg())
    schema = HealthSchema(
        name="TestHealth", variant="simple",
        extensions_fields=[CodeField(name="maxHealth", type="float", default="1f")],
    )
    fixed = pipe.default_fix(schema, "simple")
    assert fixed.extensions_fields == []
    assert "extensions_conflicts" in fixed.meta
