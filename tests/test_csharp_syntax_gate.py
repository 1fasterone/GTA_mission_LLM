"""Tests for the generated-C# syntax gate (src/csharp_syntax_gate.py) and the
string-literal escaping that feeds it (src/micro/code_fields.py).

The live defects both came from a DropZone run:

  NpcDialogPlayer.cs(23,75) / NpcDialogTrigger.cs(18,75)  CS1003
      an unescaped quote inside [Tooltip("…")] ended the literal early
  SaveSystem.cs(77,5)                                     CS0106
      a closing brace inside a `//` comment never closed Update()

The escaping tests run everywhere. The parser tests need a .NET SDK and are
skipped without one — CI has no dotnet (see the toolchain policy in the module
docstring: a missing SDK is not a defect in the generated project).
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from src.csharp_syntax_gate import (CSharpSyntaxError, SyntaxDiagnostic,
                                    check_files, enforce)
from src.micro.code_fields import (CodeField, escape_cs_string,
                                   initializer_instance_refs, lint_extensible,
                                   lint_fields, sanitize_default)

# The exact tooltip from the run, as the model returned it.
_LIVE_TOOLTIP = 'Current active quest identifier (e.g., "RocBiomeFreefall", ...'

_needs_dotnet = pytest.mark.skipif(
    shutil.which("dotnet") is None, reason="no .NET SDK on PATH")


# ── escaping (Step 2a) ────────────────────────────────────────────────────────

def test_quote_in_tooltip_is_escaped():
    f = CodeField(name="_questId", type="string", tooltip=_LIVE_TOOLTIP)
    assert '\\"RocBiomeFreefall\\"' in f.tooltip
    # No bare quote survives — that is what terminated the literal at column 75.
    assert '"' not in f.tooltip.replace('\\"', "")


def test_backslash_escaped_before_quote():
    """Order matters: escaping quotes first would then double the backslashes
    the quote-escape itself introduced."""
    assert escape_cs_string(r'C:\temp "x"') == r'C:\\temp \"x\"'


def test_newlines_and_tabs_become_escapes():
    # A raw newline inside [Tooltip("…")] is as fatal as a raw quote.
    assert escape_cs_string("Quest\nState\tB") == "Quest\\nState\\tB"
    assert escape_cs_string("a\r\nb") == "a\\nb"


def test_header_escaped_too():
    assert CodeField(name="_x", header='Set "up"').header == 'Set \\"up\\"'


def test_default_is_not_escaped():
    """`default` is NOT inside a literal — it IS the literal. saveload's
    default_fix authors `default='\"\"'` for a string field, which must render
    as `= \"\"`; escaping it would emit `= \\\"\\\"` and break the declaration."""
    assert CodeField(name="playerName", type="string", default='""').default == '""'


# ── field initializers (Step 2b) ──────────────────────────────────────────────
# `default` is the only injected value that is not inside a literal — it IS the
# initializer — so escaping does not apply to it and nothing else validated it.
# Live: ChronosDecay's SaveSystem.cs(16,175) CS1002 + 2x CS1519 on `= 0i;`.


def test_invented_int_suffix_is_stripped():
    """The live defect. The model generalised the prompt's float example
    (`"default": "0f"`) to the integer case; `i` is not a C# suffix at all."""
    assert sanitize_default("int", "0i") == "0"
    assert sanitize_default("int", "5int") == "5"


def test_float_literal_gets_the_suffix_it_needs():
    # `float x = 0.5;` is CS0664 — an unsuffixed real literal is a double.
    assert sanitize_default("float", "0.5") == "0.5f"
    assert sanitize_default("float", "80") == "80f"
    assert sanitize_default("float", "0.5d") == "0.5f"
    # `2.` is not a C# real literal at all (`2.f` lexes as member access).
    assert sanitize_default("float", "2.") == "2f"


def test_suffix_casing_is_the_authors():
    assert sanitize_default("long", "5L") == "5L"
    assert sanitize_default("float", "1.5F") == "1.5F"


def test_hex_and_binary_literals_survive():
    """`0xFF`'s digits are letters — a decimal-shaped repair would read them as
    an invented suffix and silently return 0."""
    assert sanitize_default("int", "0xFF") == "0xFF"
    assert sanitize_default("int", "0b1010") == "0b1010"
    assert sanitize_default("uint", "0xFFu") == "0xFFu"


def test_fractional_value_on_an_integral_field_is_dropped():
    """CS0266, and neither repair is honest: truncating invents a value,
    widening rewrites the author's declared type. The field still compiles."""
    assert sanitize_default("int", "2.5") is None


def test_python_literals_are_translated():
    assert sanitize_default("bool", "True") == "true"
    assert sanitize_default("string", "None") == "null"


def test_bool_defaults():
    assert sanitize_default("bool", "1") == "true"
    assert sanitize_default("bool", "0") == "false"
    assert sanitize_default("bool", "yes") is None            # no honest repair


def test_bare_string_default_is_quoted_not_dropped():
    assert sanitize_default("string", "idle") == '"idle"'
    assert sanitize_default("string", 'he said "hi"') == '"he said \\"hi\\""'
    # Already a literal / sentinel — left exactly as authored.
    assert sanitize_default("string", '""') == '""'
    assert sanitize_default("string", "null") == "null"


def test_expressions_pass_through_untouched():
    for expr in ("new Vector3(0f, 1f, 0f)", "Color.white", "Mathf.PI",
                 "new List<int>()"):
        assert sanitize_default("Vector3", expr) == expr


def test_structurally_unsafe_initializers_are_dropped():
    """The declaration is one line, so a stray `;`, an unclosed bracket or a
    newline does not mistype this field — it corrupts every member after it."""
    assert sanitize_default("int", "0; public int Injected = 1") is None
    assert sanitize_default("float", "(1f") is None
    assert sanitize_default("string", '"unterminated') is None
    assert sanitize_default("int", "0\npublic int y") is None
    # A `;` inside a string literal is not a hazard.
    assert sanitize_default("string", '"a;b"') == '"a;b"'


def test_collection_default_requires_new_or_null():
    """Live on the RepoQuest run, BranchStrategyController.cs(11): the model
    meant `new List<string> { "GitHubFlow", "GitFlow" }` and wrote the bare
    comma list instead — quotes balanced, no brackets, so the old structural
    guard alone waved it through as a legal-looking expression."""
    assert sanitize_default("List<string>", 'GitHubFlow", "GitFlow') is None
    assert sanitize_default("List<string>", "null") == "null"
    assert sanitize_default(
        "List<string>", 'new List<string> { "GitHubFlow", "GitFlow" }'
    ) == 'new List<string> { "GitHubFlow", "GitFlow" }'
    assert sanitize_default("Dictionary<string, int>", "new()") == "new()"
    assert sanitize_default("int[]", "1, 2, 3") is None


def test_in_repo_authored_defaults_are_untouched():
    """saveload's default_fix authors these three; the repair must be a no-op
    for values that were already correct."""
    for type_, default in (("int", "1"), ("int", "0"), ("string", '""')):
        assert sanitize_default(type_, default) == default


def test_lint_fields_applies_the_repair():
    """lint_fields is the single choke point — all thirteen render sites of
    `= {{ f.default }}` go through it."""
    out = lint_fields([CodeField(name="_breaches", type="int", default="0i")],
                      reserved=set())
    assert out[0].default == "0"


def test_repaired_field_renders_a_parseable_declaration():
    """Through the real macro, the way the generator does it."""
    env = Environment(loader=FileSystemLoader("templates/unity/micro"))
    tpl = env.from_string(
        '{% import "_inject.j2" as inject %}{{ inject.fields(s) }}')

    class S:
        extra_fields = lint_fields(
            [CodeField(name="_breaches", type="int", default="0i")],
            reserved=set())

    assert "= 0;" in tpl.render(s=S())
    assert "0i" not in tpl.render(s=S())


# ── instance-referencing initializers (CS0236) ────────────────────────────────
# A field initializer runs before `this` exists, so an instance reference is a
# hard error however well-formed the expression is — every structural check in
# sanitize_default passes it. Live on a Monaco monobike run:
#   MonobikeController.cs(9,45)  = GetComponent<Rigidbody>()
#   MonobikeAI.cs(9,53)          = GetComponent<NavMeshAgent>()

_LIVE_RB_INIT    = "GetComponent<Rigidbody>()"
_LIVE_AGENT_INIT = "GetComponent<NavMeshAgent>()"


def test_getcomponent_initializer_moves_into_awake():
    """The repair is a MOVE, not a drop: dropping alone compiles but leaves a
    null the scene never assigns, and the guarded body silently does nothing."""
    hoisted: list[str] = []
    out = lint_fields([CodeField(name="rb", type="Rigidbody", default=_LIVE_RB_INIT)],
                      reserved=set(), hoist_into=hoisted)
    assert out[0].default is None
    assert hoisted == ["rb = GetComponent<Rigidbody>();"]


def test_getcomponent_initializer_is_dropped_without_an_awake_channel():
    """Eleven micro templates render extra_fields with no Awake() injection
    point; there the compile error still has to go, so the initializer does."""
    out = lint_fields(
        [CodeField(name="aiAgent", type="NavMeshAgent", default=_LIVE_AGENT_INIT)],
        reserved=set())
    assert out[0].default is None


def test_receiver_qualified_expressions_are_legal_and_untouched():
    """`Camera.main.GetComponent<T>()` never touches `this`, so CS0236 does not
    apply — flagging it would move a perfectly good initializer."""
    for expr in ("Camera.main.GetComponent<AudioListener>()",
                 "Camera.main.transform",
                 "Time.deltaTime",
                 "nameof(Rigidbody)"):
        assert initializer_instance_refs(expr, siblings=set()) == []
        assert sanitize_default("float", expr) == expr


def test_sibling_field_reference_is_the_same_error():
    """One extra_field's initializer reading another is CS0236 too — the other
    field is instance state, and initializer order is not guaranteed."""
    hoisted: list[str] = []
    out = lint_fields(
        [CodeField(name="baseSpeed",  type="float", default="10f"),
         CodeField(name="boostSpeed", type="float", default="baseSpeed * 2f")],
        reserved=set(), hoist_into=hoisted)
    assert out[0].default == "10f"                 # untouched
    assert out[1].default is None
    assert hoisted == ["boostSpeed = baseSpeed * 2f;"]


def test_instance_names_inside_literals_are_not_references():
    """Judged at CODE positions only — the same `_code_mask` every other pass
    uses, so a string that merely spells `transform` is not held against it."""
    assert initializer_instance_refs('"transform"', siblings=set()) == []
    assert initializer_instance_refs('"the gameObject name"', siblings={"name"}) == []


def test_hoisted_field_renders_a_bare_declaration():
    """Through the real macro: the declaration keeps the field, loses the
    initializer, and no GetComponent survives at class level."""
    env = Environment(loader=FileSystemLoader("templates/unity/micro"))
    tpl = env.from_string(
        '{% import "_inject.j2" as inject %}{{ inject.fields(s) }}')

    class S:
        extra_fields = lint_fields(
            [CodeField(name="rb", type="Rigidbody", default=_LIVE_RB_INIT)],
            reserved=set(), hoist_into=[])

    rendered = tpl.render(s=S())
    assert "private Rigidbody rb;" in rendered
    assert "GetComponent" not in rendered


def _extensible(**kw):
    """Minimal object satisfying `_ExtensibleLike` (lint_extensible mutates it
    in place; the real schemas are Pydantic models with the same attributes)."""
    return SimpleNamespace(
        extra_usings=kw.get("extra_usings", []),
        extra_fields=kw.get("extra_fields", []),
        awake_extra=kw.get("awake_extra", ""),
        update_extra=kw.get("update_extra", ""),
        fixed_update_extra=kw.get("fixed_update_extra", ""),
        extra_methods=kw.get("extra_methods", []),
    )


def test_lint_extensible_hoists_ahead_of_authored_awake_statements():
    """Order matters: the author's own Awake() body may read the field the
    hoisted assignment initialises."""
    s = _extensible(
        extra_fields=[CodeField(name="rb", type="Rigidbody", default=_LIVE_RB_INIT)],
        awake_extra="rb.mass = 250f;")
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    assert s.extra_fields[0].default is None
    assert s.awake_extra == "rb = GetComponent<Rigidbody>();\nrb.mass = 250f;"


def test_lint_extensible_hoist_is_opt_in():
    """Default off: a caller whose template does not render awake_extra would
    otherwise lose the assignment silently."""
    s = _extensible(
        extra_fields=[CodeField(name="rb", type="Rigidbody", default=_LIVE_RB_INIT)])
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"})
    assert s.extra_fields[0].default is None
    assert s.awake_extra == ""


def test_hoisted_initializer_still_triggers_its_missing_using():
    """The auto-heal blob reads awake_extra, so moving the expression out of
    the declaration must not cost the field its namespace (CS0246)."""
    s = _extensible(
        extra_fields=[CodeField(name="agent", type="NavMeshAgent",
                                default=_LIVE_AGENT_INIT)])
    lint_extensible(s, reserved_fields=set(), template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    assert "UnityEngine.AI" in s.extra_usings


# ── the declaration text lives in exactly one place ──────────────────────────


def test_no_template_hand_copies_the_declaration_text():
    """Both injection defects fixed so far lived in the field-declaration line,
    and each had to be chased across every template carrying a copy of it. The
    Unity micro templates now build it from `_inject.j2`'s attrs/decl/field
    macros; nothing may re-inline `[SerializeField] private {{ f.type }}`.

    Whitespace AROUND the call is each generator's own business (indentation
    and attribute placement genuinely differ) — only the declaration text is
    centralised.
    """
    root = Path("templates/unity/micro")
    offenders = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.j2")
        if p.name != "_inject.j2"
        and "[SerializeField] private {{ f.type }}" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "these templates re-inline the field declaration instead of calling "
        f"inject.field(f) / inject.decl(f): {offenders}")


def test_inject_macros_emit_no_stray_whitespace():
    """The micro Jinja env sets neither trim_blocks nor lstrip_blocks, so a
    multi-line macro body would leak newlines into all thirteen call sites."""
    env = Environment(loader=FileSystemLoader("templates/unity/micro"))
    inject = env.get_template("_inject.j2").module
    f = CodeField(name="_x", type="int", default="3", header="H", tooltip="T")
    assert str(inject.attrs(f)) == '[Header("H")] [Tooltip("T")] '
    assert str(inject.decl(f)) == "[SerializeField] private int _x = 3;"
    assert str(inject.field(f)) == str(inject.attrs(f)) + str(inject.decl(f))
    # No attributes → the attrs half contributes nothing at all.
    assert str(inject.attrs(CodeField(name="_y", type="float"))) == ""


def test_escaped_tooltip_renders_a_parseable_attribute(tmp_path):
    """The whole point, through the real macro: _inject.j2 interpolates the
    tooltip raw, so the escaping has to survive to the rendered line."""
    env = Environment(loader=FileSystemLoader("templates/unity/micro"))
    tpl = env.from_string(
        '{% import "_inject.j2" as inject %}\n'
        "using UnityEngine;\n"
        "public class T : MonoBehaviour\n{\n{{ inject.fields(s) }}\n}"
    )

    class S:
        extra_fields = [CodeField(name="_questId", type="string",
                                  tooltip=_LIVE_TOOLTIP)]
        extra_usings: list = []
        extra_methods: list = []

    out = tpl.render(s=S())
    assert '[Tooltip("Current active quest identifier (e.g., \\"RocBiomeFreefall\\", ...")]' in out


# ── diagnostic parsing (no toolchain needed) ─────────────────────────────────

def test_diagnostic_formats_like_the_compiler():
    d = SyntaxDiagnostic(file="A.cs", line=23, column=75, code="CS1003",
                         message="Syntax error, ',' expected")
    assert str(d) == "A.cs(23,75): CS1003: Syntax error, ',' expected"


def test_gate_can_be_disabled(tmp_path):
    f = tmp_path / "Broken.cs"
    f.write_text("public class X { ", encoding="utf-8")
    result = check_files([f], enabled=False)
    assert result.skipped and result.ok


def test_non_cs_files_are_ignored(tmp_path):
    (tmp_path / "notes.md").write_text("# not code", encoding="utf-8")
    assert check_files([tmp_path / "notes.md"]).checked == 0


# ── the parser itself (Step 3) ───────────────────────────────────────────────

def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@_needs_dotnet
def test_unescaped_quote_in_tooltip_is_caught(tmp_path):
    """The CS1003 half of the live evidence, verbatim."""
    p = _write(tmp_path, "NpcDialogTrigger.cs",
               "using UnityEngine;\n"
               "public class NpcDialogTrigger : MonoBehaviour\n{\n"
               '    [Tooltip("Current active quest identifier (e.g., "RocBiomeFreefall", ...")]'
               " [SerializeField] private string _questId;\n"
               "    private void Update() { }\n}\n")
    result = check_files([p])
    assert not result.ok
    assert any(d.code == "CS1003" and d.line == 4 for d in result.diagnostics)


@_needs_dotnet
def test_brace_inside_a_comment_is_caught(tmp_path):
    """The CS0106 half. Brace CHARACTERS are balanced here — this is the case
    that proves a counter cannot work and a real parser is required."""
    src = ("using UnityEngine;\n"
           "public class SaveSystem : MonoBehaviour\n{\n"
           "    private bool _isDirty;\n"
           "    private int _currentLocationIndex;\n"
           "    private void Update()\n    {\n"
           "        if (_isDirty) { int loc = _currentLocationIndex;"
           " // Validate state before save }\n"
           "    }\n\n    public void Save() { }\n}\n")
    assert src.count("{") == src.count("}")      # a counter sees nothing wrong
    result = check_files([_write(tmp_path, "SaveSystem.cs", src)])
    assert not result.ok


@_needs_dotnet
def test_clean_unity_script_passes(tmp_path):
    """No false positives: an ordinary MonoBehaviour must parse clean even
    though UnityEngine is not referenced — the gate parses, it does not bind."""
    p = _write(tmp_path, "Clean.cs",
               "using UnityEngine;\n"
               "public class Clean : MonoBehaviour\n{\n"
               '    [Tooltip("Speed in m/s.")] [SerializeField] private float _speed = 1f;\n'
               "    private void Update() { transform.Translate(Vector3.forward * _speed); }\n"
               "}\n")
    result = check_files([p])
    assert result.ok and result.checked == 1


@_needs_dotnet
def test_escaped_tooltip_parses_clean(tmp_path):
    """Step 2a and Step 3 together: the same live tooltip, escaped, parses."""
    f = CodeField(name="_questId", type="string", tooltip=_LIVE_TOOLTIP)
    p = _write(tmp_path, "Fixed.cs",
               "using UnityEngine;\n"
               "public class Fixed : MonoBehaviour\n{\n"
               f'    [Tooltip("{f.tooltip}")] [SerializeField] private string _questId;\n'
               "    private void Update() { }\n}\n")
    assert check_files([p]).ok


@_needs_dotnet
def test_enforce_raises_with_location_and_message(tmp_path):
    """Fail loudly — same doctrine as [assets.strict]: file, line, column and
    the compiler's own text, never a silent repair."""
    p = _write(tmp_path, "Bad.cs", "public class X { void M() { \n")
    with pytest.raises(CSharpSyntaxError) as exc:
        enforce([p])
    assert exc.value.diagnostics
    d = exc.value.diagnostics[0]
    assert d.code.startswith("CS") and d.line >= 1 and d.column >= 1
    assert "Bad.cs" in str(exc.value)


@_needs_dotnet
def test_language_version_violations_are_NOT_caught(tmp_path):
    """Documented limitation, locked in so nobody assumes otherwise.

    The gate parses as C# 9 (Unity 6000.0), but Roslyn PARSES newer constructs
    happily and defers "feature is not available in C# 9" to binding, which
    this gate deliberately does not run. So a C# 12 collection expression
    (`= [12000f, 4500f, 300f]`, seen live) and a C# 10 file-scoped namespace
    both pass here and surface in the Unity console instead.
    """
    collection_expr = _write(
        tmp_path, "Collect.cs",
        "public class C { private float[] _a = [12000f, 4500f, 300f]; }\n")
    file_scoped_ns = _write(
        tmp_path, "Ns.cs", "namespace N;\npublic class D { }\n")
    assert check_files([collection_expr]).ok
    assert check_files([file_scoped_ns]).ok
