"""
Tests for the template-scope helpers that survive the R0 binder retirement.

`bind_authored_code`, the Python name-binder that commented authored
statements out against a hand-maintained MonoBehaviour member list, was
retired in R0: whether authored C# binds is now judged by the selected-source
Roslyn contract against the real Unity reference profile
(src/unity_authored_check.py) and by the Editor compile gate. What remains
here are the template readers and lint helpers other passes still call
(`find_unbound_identifiers` backs the statemachine's transition check).

Fixture provenance: these are TEST DATA (hand-transcribed from generated .cs),
not pipeline output. They exercise the deterministic lint layer only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.code_fields import (
    CodeExtensibleSchema, CodeField, CodeMethod, find_unbound_identifiers,
    sanitize_default, template_method_arities, template_members,
    template_method_params,
    _disable_bad_header, _fragment_declares,
)

MICRO = Path("templates/unity/micro")


def _schema(fields=(), methods=(), **extras) -> CodeExtensibleSchema:
    return CodeExtensibleSchema(
        extra_fields=[CodeField(name=n, type=t) for n, t in fields],
        extra_methods=[CodeMethod(signature=s, body=b) for s, b in methods],
        **extras,
    )


def _all_code(schema: CodeExtensibleSchema) -> str:
    return "\n".join(
        [schema.awake_extra, schema.update_extra, schema.fixed_update_extra]
        + [m.body for m in schema.extra_methods]
    )


def _live_lines(text: str) -> list[str]:
    """Statements that survived — AUDIT comments do not count as code."""
    return [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("//")]


# ── the live TurboToys failures, one test per micro ──────────────────────────


def test_controller_comment_prose_does_not_declare_a_variable():
    """The regression that hid the five `rb` errors: `// ...increase speed` on
    the line above `rb.AddForce(...)` reads as a `<type> <name>` pair, so `rb`
    looked declared. Declarations must come from CODE only."""
    frag = "// Consume boost to increase speed\nrb.AddForce(Vector3.up);"
    assert "rb" not in _fragment_declares(frag)
    assert "boost" not in _fragment_declares(frag)
    # ...while a real declaration on a real line still registers.
    assert "lateralG" in _fragment_declares("float lateralG = 1f;")


# ── the pass must not accuse legal code ──────────────────────────────────────


# ── the supporting ground truth ──────────────────────────────────────────────


def test_template_method_arities_reads_optional_parameters_as_a_range():
    arities = template_method_arities(str(MICRO / "interactable"))
    assert arities.get("OnTriggerEnter") == (1, 1)


def test_disable_bad_header_preserves_a_trailing_brace():
    """A header, its `{`, and a comment that swallowed the closer, all on one
    line (RivalPathfinderBT.cs(132,13)). The brace must be re-emitted or the
    block never closes."""
    out = _disable_bad_header("    if (nope < 1f) { // taunt here }", ["nope"])
    assert out is not None
    assert out.count("{") == 1 and out.count("}") == 0
    assert "nope" not in out.split("AUDIT")[0]


def test_disable_bad_header_declines_a_nested_if_on_one_line():
    """EnemySpawner.cs(69) (Dead Reckoning Protocol live run): a NESTED
    `if (...) { ...; if (...) { // comment } }`, same trailing-comment-
    swallows-the-closer shape as the single-if TurboToys case above, one
    level deeper. `cond`'s greedy `.*` walked past the outer header's own
    `)` and swallowed the inner if's header AND brace, so the rewrite kept
    only ONE `{` for what were really two real opens -- both original
    closes survive untouched further down the file, so the file gained one
    extra `}` and every declaration after it misparsed (CS8803/CS0106/
    CS1022). Declining (None) and leaving the line alone is the existing,
    tested fallback for "neither repair is safe" -- a stray CS0103 on this
    one line beats a corrupted file."""
    line = ('    if (Player.something) { float x = 1f; '
            'if (x > 0f) { // Trigger stealth alert state } }')
    assert _disable_bad_header(line, ["Player"]) is None


def test_neutralize_flagged_lines_leaves_a_nested_if_line_untouched():
    """End-to-end: `_neutralize_flagged_lines` must fall through to "leave
    the line alone" for the same nested shape, not silently corrupt brace
    balance across the whole method."""
    from src.micro.code_fields import _neutralize_flagged_lines

    text = (
        "private void Update()\n"
        "{\n"
        "    if (Player.something) { float x = 1f; "
        "if (x > 0f) { // Trigger stealth alert state } }\n"
        "}\n"
    )
    out, flagged = _neutralize_flagged_lines(text, {2: {"Player"}})
    assert out.count("{") == out.count("}")
    assert "Player.something" in out   # left alone, not silently mangled


def test_find_unbound_identifiers_skips_strings_and_comments():
    text = 'Debug.Log("driftIntensity is high"); // zoomFactor unused'
    assert find_unbound_identifiers(text, known={"Debug"}) == {}


def test_named_argument_is_not_treated_as_an_identifier():
    text = "Mathf.Lerp(a: 0f, b: 1f, t: 0.5f);"
    assert find_unbound_identifiers(text, known={"Mathf"}) == {}


# ── CS0029: a quoted literal on a field that is not a string ─────────────────

@pytest.mark.parametrize("declared,default,expected", [
    ("Dictionary<string, object>", '"null"', "null"),   # SaveSystem.cs(15,166)
    ("float", '"0.5"', "0.5f"),
    ("bool", '"true"', "true"),
    ("Vector3", '"new Vector3(1,2,3)"', "new Vector3(1,2,3)"),
    ("int", '"abc"', None),                             # CS0029 -> not CS0103
    ("object", '"anything"', '"anything"'),             # genuinely convertible
    ("string", '"quoted"', '"quoted"'),
])
def test_quoted_default_on_a_non_string_field(declared, default, expected):
    assert sanitize_default(declared, default) == expected


# ── CS1503 / CS0103 from CodeBranchSim ───────────────────────────────────────
#
# Eight compile errors from one run, all past the two gates that exist. Seven
# were argument TYPES on a call whose argument COUNT was legal, which the arity
# check answers "fine" to by construction. The eighth called a template method
# that this variant does not render.
#
# Fixture provenance: TEST DATA, hand-transcribed from the generated .cs at
# C:\u\CodeBranchSim. They exercise the deterministic lint layer only.


def test_an_overloaded_name_is_not_type_checked(tmp_path):
    """Two signatures mean a call could match either, and this pass cannot tell
    which was meant — so it judges neither, rather than picking one and
    neutralising every call that matched the other."""
    (tmp_path / "Over.cs.j2").write_text("""public class Over {
    public void Ping(string a) {}
    public void Ping(int a) {}
    public void Solo(string a) {}
}
""", encoding="utf-8")
    params = template_method_params(str(tmp_path))
    assert "Ping" not in params
    assert params["Solo"] == ("string",)


# ── Template-field type misuse (CS1061 / CS1503) ─────────────────────────────
#
# Three compile errors from one compose build (AutomatonCitadel), all the same
# shape: an authored statement uses a TEMPLATE-declared field as if it had a
# different type than the template actually gives it. The unbound-identifier
# pass above proves the field NAME binds; it never asks what binding to it
# means, so `_music.PickClip(...)` shipped — `_music` really is a field.
#
# Fixture provenance: TEST DATA, hand-transcribed from the generated .cs.


def test_a_local_shadowing_a_template_field_name_is_not_judged():
    """A local re-declared with its own type is not the template's field, so a
    call against it must never be judged by the field's declared type."""
    from src.micro.code_fields import find_field_type_misuse, template_field_types
    field_types = template_field_types(str(MICRO / "audio"))
    assert field_types.get("_music") == "AudioSource"
    body = 'AudioCue _music = someBank.Find("x"); _music.PickClip();'
    assert not find_field_type_misuse(body, field_types=field_types)


# ── The variant decides what the template actually renders ───────────────────

def test_a_member_from_another_variant_is_not_in_scope():
    """SaveSystem.cs(70,89) CS0103. `SetCheckpoint` really is declared in the
    saveload template — inside `{% if variant == "checkpoint" %}`. Reading the
    template as one flat text put it in scope for all three variants, and the
    emitted file for `slots` does not have it."""
    assert "SetCheckpoint" in template_members(str(MICRO / "saveload"), "checkpoint")
    assert "SetCheckpoint" not in template_members(str(MICRO / "saveload"), "slots")


def test_no_variant_still_reads_the_whole_template():
    """Callers that cannot say which variant keep the old union — too WIDE is
    the safe direction here, because too narrow would accuse legal code."""
    assert "SetCheckpoint" in template_members(str(MICRO / "saveload"))


def test_a_condition_that_is_not_about_the_variant_keeps_its_body():
    from src.micro.code_fields import _drop_dead_variant_branches
    src = "{% if s.wants_audio %}\npublic void Boom() {}\n{% endif %}"
    assert "Boom" in _drop_dead_variant_branches(src, "slots")


# ── The schema's OTHER enums decide scope too ────────────────────────────────
#
# Provenance: TEST DATA, transcribed from the generated .cs at
# C:\u\RepoQuestEnterprise\Assets\Micros\spawner\Scripts\EnemySpawner.cs. The
# spawner template declares `spawnPoints` under
# `{% if s.spawn_shape == "spawn_points" %}`; the schema said `point`; the two
# authored methods indexing `spawnPoints` shipped, because the branch pruner
# read `variant` and nothing else.


# ── A LITERAL-in-list-FIELD condition decides scope too ─────────────────────
#
# Provenance: live, C:\u\RedlineRidge\Assets\Micros\controller\Scripts\
# PlayerController.cs(258): CS0103 on `_boostCooldownTimer`. Controller.cs.j2
# gates its boost fields on `{% if "boost" in s.abilities %}` — the OPPOSITE
# shape from the spawn_shape test above (a literal on the left, a bare
# list-typed field on the right), which `_ATOM_IN_RE` cannot parse at all. Every
# ability-gated block in that template (35+: jump, dash, shoot, block, grab,
# pause, ...) was therefore undecidable and counted as always-declared by the
# dedup pass regardless of this schema's real abilities — so an author's own
# `_boostTimer`/`_boostCooldownTimer` fields got deleted as false CS0102
# duplicates of a block that, for a boost-less abilities list, never renders.


def test_an_undecidable_condition_still_keeps_its_body():
    from src.micro.code_fields import _drop_dead_variant_branches
    # A field this pass has no value for, and a mixed and/or it will not
    # untangle — both keep their bodies. Too WIDE is the safe direction.
    src = '{% if s.mood == "grim" %}\npublic void Boom() {}\n{% endif %}'
    assert "Boom" in _drop_dead_variant_branches(src, "", (("shape", "point"),))
    mixed = ('{% if s.shape == "point" and s.pooling == "pooled" '
             'or s.shape == "box" %}\npublic void Bang() {}\n{% endif %}')
    assert "Bang" in _drop_dead_variant_branches(
        mixed, "", (("shape", "point"), ("pooling", "instantiate")))


