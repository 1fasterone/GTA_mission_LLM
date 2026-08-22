"""
Tests for the binding gate over LLM-authored micro code (`bind_authored_code`).

The syntax gate is parse-only by design, so nothing in the pipeline ever looked
at NAMES. A TurboToys run shipped 49 compile errors across 10 micros and 41 of
them were the same defect: a fragment referencing state the model never
declared. Every case below is transcribed from that build — the fixtures are
the authored C# as it reached the file, and the assertion is that the reference
no longer survives to the assembly.

Fixture provenance: these are TEST DATA (hand-transcribed from generated .cs),
not pipeline output. They exercise the deterministic lint layer only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.code_fields import (
    CodeExtensibleSchema, CodeField, CodeMethod,
    bind_authored_code, find_unbound_identifiers, harden_against_template,
    sanitize_default, template_method_arities, template_scope_members,
    template_types, template_members, template_method_params,
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

def test_camera_undeclared_drift_state_is_neutralized():
    """PlayerCamera.cs(60,13) / (68,55) — driftIntensity, zoomFactor, velocity
    and speed were never declared; 16 of the run's 49 errors."""
    s = _schema(
        fields=[("_cameraZoom", "float"), ("_driftThreshold", "float")],
        methods=[("private void OnLateUpdate()",
                  "float lateralG = Vector3.Dot(transform.right, velocity.normalized) * speed; "
                  "if (lateralG > _driftThreshold) driftIntensity = Mathf.Max(driftIntensity, lateralG);")],
        update_extra=("if (driftIntensity > _driftThreshold) { zoomFactor = "
                      "Mathf.Lerp(zoomFactor, _cameraZoom, 20f * Time.deltaTime); }"),
    )
    msgs = bind_authored_code(s, MICRO / "camera")
    code = _all_code(s)
    for name in ("driftIntensity", "zoomFactor", "velocity", "speed"):
        assert name not in "\n".join(_live_lines(code)), name
    assert any("driftIntensity" in m for m in msgs)
    # The fields the model DID declare must survive untouched.
    assert {f.name for f in s.extra_fields} == {"_cameraZoom", "_driftThreshold"}


def test_health_reference_to_wrong_state_name_is_neutralized():
    """PlayerHealth.cs(51,13) — the model wrote `health`; the template's field
    is `_current`. A near-miss on a real member, not an invention."""
    s = _schema(
        fields=[("_boostThreshold", "float"), ("_driftBoostAmount", "float")],
        methods=[("private bool IsDrifting()",
                  "return transform.rotation != Quaternion.identity;")],
        update_extra="if (health < _boostThreshold && IsDrifting()) "
                     "health += _driftBoostAmount * Time.deltaTime;",
    )
    bind_authored_code(s, MICRO / "health")
    assert not _live_lines(s.update_extra)
    # An authored method that binds cleanly is NOT collateral damage.
    assert [m.signature for m in s.extra_methods] == ["private bool IsDrifting()"]


def test_controller_orphaned_local_is_caught_on_a_second_round():
    """PlayerController.cs(250..266) — `rb` is undeclared, and neutralizing the
    line that declares `lateralSpeed` FROM it orphans the line below. A
    single-pass gate leaves the second error behind."""
    s = _schema(
        fields=[("_boostMeter", "float"), ("_driftSquealSource", "AudioSource")],
        update_extra="""if (rb.velocity.magnitude > 2f && _driftSquealSource != null)
{
    float lateralSpeed = Mathf.Abs(rb.velocity.x) + Mathf.Abs(rb.velocity.z);
    if (lateralSpeed > rb.velocity.magnitude * 0.5f)
    {
        _driftSquealSource.volume = Mathf.Clamp(lateralSpeed / 10f, 0f, 1f);
    }
}""",
    )
    bind_authored_code(s, MICRO / "controller")
    live = "\n".join(_live_lines(s.update_extra))
    assert "rb." not in live
    assert "lateralSpeed" not in live
    # Braces are untouched — a disabled condition must never restructure code.
    assert s.update_extra.count("{") == s.update_extra.count("}")


def test_controller_comment_prose_does_not_declare_a_variable():
    """The regression that hid the five `rb` errors: `// ...increase speed` on
    the line above `rb.AddForce(...)` reads as a `<type> <name>` pair, so `rb`
    looked declared. Declarations must come from CODE only."""
    frag = "// Consume boost to increase speed\nrb.AddForce(Vector3.up);"
    assert "rb" not in _fragment_declares(frag)
    assert "boost" not in _fragment_declares(frag)
    # ...while a real declaration on a real line still registers.
    assert "lateralG" in _fragment_declares("float lateralG = 1f;")


def test_saveload_unknown_collaborator_and_state_are_neutralized():
    """SaveSystem.cs(81..95) — playerCurrentLap/playerTransform/boostCharge/
    rivalPositions never declared, `FileUtility` is a class the project never
    generates. The CS0077 `as int` errors sit on those same lines."""
    s = _schema(
        fields=[("_lastSaveTime", "float")],
        methods=[("private void LoadGame(string saveName)",
                  'Dictionary<string, object> data = FileUtility.LoadJsonFromFile(saveName) '
                  'as Dictionary<string, object>;\n'
                  'if (data != null)\n'
                  '{\n'
                  '  playerCurrentLap = data["playerLap"] as int ?? 1;\n'
                  '}')],
    )
    bind_authored_code(s, MICRO / "saveload")
    live = "\n".join(ln for m in s.extra_methods for ln in _live_lines(m.body))
    assert "FileUtility" not in live
    assert "playerCurrentLap" not in live
    # `data` is orphaned once its declaration goes — the guard that reads it
    # must be disabled too, not left pointing at a name that no longer exists.
    assert "data != null" not in live
    assert "if (false)" in live


def test_shader_this_qualified_call_to_a_missing_method_is_caught():
    """UIOverlayShaderController.cs(101,233) CS1061 — `this.GetFloatProperty()`.
    A member access is normally unjudgeable, but `this.` has complete ground
    truth. The CS0019 `Color - Vector3` on the same line goes with it."""
    s = _schema(
        fields=[("_colorCycleTime", "float")],
        methods=[("private void CycleToyColors()",
                  'float r = this.GetFloatProperty("_r", 0f); '
                  'this.SetFloatProperty("_g", r);')],
    )
    msgs = bind_authored_code(s, MICRO / "shader")
    assert not s.extra_methods                     # body went empty -> dropped
    assert any("GetFloatProperty" in m for m in msgs)


def test_dialogue_sibling_class_member_is_not_in_scope():
    """SparkyDialogue.cs(120,16) — `text` is declared in the folder's OTHER
    template (Node.cs.j2), on a different class. A folder-wide member set
    swears it is in scope; only the injected file's members really are."""
    from src.micro.code_fields import template_members
    assert "text" in template_members(str(MICRO / "dialogue"))          # Node.cs.j2
    assert "text" not in template_scope_members(str(MICRO / "dialogue"))
    s = _schema(methods=[("public string GetDisplayedText()",
                          'return text.Replace("_", "");')])
    bind_authored_code(s, MICRO / "dialogue")
    assert not s.extra_methods


def test_objectives_uppercase_assignment_target_is_caught():
    """PitStopTalkObjectives.cs(90,10) — `Sparky = ...`. An assignment target
    must be a member or a local whatever its casing, so the ground truth is
    complete here even though the name looks like a type. The CS0246 on
    `CharController` is on the same statement."""
    s = _schema(
        fields=[("_sparkyRef", "GameObject")],
        methods=[("private void OnInteract()",
                  "if (_sparkyRef != null) {\n"
                  "\tSparky = _sparkyRef.GetComponent<CharController>();\n"
                  "}")],
    )
    bind_authored_code(s, MICRO / "objectives")
    live = "\n".join(ln for m in s.extra_methods for ln in _live_lines(m.body))
    assert "Sparky" not in live and "CharController" not in live


def test_objectives_unqualified_call_to_a_missing_method_is_caught():
    """FirstLapChallengeObjectives.cs(106,35) — `IsReachedFinishLine()`. An
    unqualified call must resolve on this class, so an uppercase name is safe
    to judge in call position."""
    s = _schema(
        fields=[("_starCount", "int")],
        methods=[("public bool IsChallengeComplete()",
                  "return _starCount >= 8 && IsReachedFinishLine();")],
    )
    bind_authored_code(s, MICRO / "objectives")
    # A `bool` method left with no return would be CS0161 — it must be dropped,
    # not emptied.
    assert not s.extra_methods


def test_interactable_wrong_arity_against_a_template_method():
    """WorldInteractable.cs(87,9) CS7036 — `OnTriggerEnter()` with no argument
    against the template's `OnTriggerEnter(Collider other)`. The name binds; the
    call does not."""
    s = _schema(methods=[("private void OnPickup()",
                          "PlayBoostEffect();\nOnTriggerEnter();"),
                         ("private void PlayBoostEffect()",
                          "Debug.Log(\"boost\");")])
    msgs = bind_authored_code(s, MICRO / "interactable")
    bodies = {m.signature: m.body for m in s.extra_methods}
    assert "OnTriggerEnter()" not in "\n".join(
        _live_lines(bodies["private void OnPickup()"]))
    # The sibling call, which is a real authored method, must survive.
    assert "PlayBoostEffect();" in bodies["private void OnPickup()"]
    assert any("CS7036" in m or "needs" in m for m in msgs)


def test_behavior_tree_parameter_scope_is_per_method():
    """RivalPathfinderBT.cs(132,13) — `playerPositionPercent` is a parameter of
    ApplyRubberBandLogic and is referenced from TauntPlayerIfBehind, where it
    does not exist. A fragment-wide scope would miss it."""
    s = _schema(
        fields=[("_targetSpeedOffset", "float"), ("_rubberBandFactor", "float")],
        methods=[
            ("private void ApplyRubberBandLogic(float playerPositionPercent)",
             "_targetSpeedOffset = _rubberBandFactor * (playerPositionPercent - 0.5f);"),
            ("private void TauntPlayerIfBehind()",
             "if (playerPositionPercent < 0.45f) { // taunt here }\n}"),
        ],
    )
    bind_authored_code(s, MICRO / "behavior_tree")
    bodies = {m.signature: m.body for m in s.extra_methods}
    # The parameter is legal in its OWN method — that body is untouched.
    assert ("_targetSpeedOffset = _rubberBandFactor"
            in bodies["private void ApplyRubberBandLogic(float playerPositionPercent)"])
    taunt = bodies["private void TauntPlayerIfBehind()"]
    assert "playerPositionPercent < 0.45f" not in taunt
    assert "if (false)" in taunt


# ── the pass must not accuse legal code ──────────────────────────────────────

@pytest.mark.parametrize("frag", [
    "transform.position = Vector3.zero;",
    "_cam = GetComponent<Camera>();",
    "if (Input.GetKeyDown(KeyCode.Space)) Debug.Log(\"jump\");",
    "foreach (var hit in Physics.OverlapSphere(transform.position, 3f)) hit.SendMessage(\"Ping\");",
    "for (int i = 0; i < 4; i++) { float t = i * 0.25f; Debug.Log(t); }",
    "if (Physics.Raycast(transform.position, Vector3.down, out var hit, 2f)) Debug.Log(hit.distance);",
    "var list = new List<string>(); list.Add(gameObject.name);",
    "try { File.ReadAllText(Application.persistentDataPath); } catch (IOException e) { Debug.Log(e.Message); }",
    "StartCoroutine(Wait());",
    "_t = Mathf.Clamp01(_t + Time.deltaTime); if (_t >= 1f) CompareTag(\"Player\");",
])
def test_legal_unity_code_is_left_alone(frag):
    """A false positive costs a real statement, so the everyday shapes are
    locked in: inherited members, statics, out-vars, foreach/for/catch
    bindings, generics."""
    s = _schema(fields=[("_t", "float"), ("_cam", "Camera")],
                methods=[("private IEnumerator Wait()", "yield return null;")],
                update_extra=frag)
    bind_authored_code(s, MICRO / "camera")
    assert s.update_extra == frag


def test_gta_micros_abstain_rather_than_judging_unity_scope():
    """A SHVDN micro derives from Script, not MonoBehaviour — judging it against
    Unity's symbol universe would accuse `Tick` and `Interval` of not existing."""
    gta = Path("templates/gta5/micro")
    if not gta.is_dir():
        pytest.skip("no GTA micro templates in this tree")
    s = _schema(update_extra="Interval = 100; Tick += OnTick;")
    harden_against_template(s, gta, engine="gta5")
    assert s.update_extra == "Interval = 100; Tick += OnTick;"


# ── the supporting ground truth ──────────────────────────────────────────────

def test_a_schema_named_sibling_type_binds():
    """saveload declares `class {{ s.name }}Data`, so the type the file really
    contains is `<schema.name>Data`. The neutralised template says `_J_Data`,
    which matches nothing a fragment could write — without expanding the
    placeholder, `new SaveSystemData()` is a false positive."""
    assert "_J_Data" in template_types(str(MICRO / "saveload"))

    class _Named(CodeExtensibleSchema):
        name: str = "SaveSystem"

    s = _Named(update_extra="var d = new SaveSystemData(); Debug.Log(d);")
    bind_authored_code(s, MICRO / "saveload")
    assert "new SaveSystemData()" in s.update_extra


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

def _typed(kind, variant, body, signature=""):
    """Every name the real gate would have in scope, so the only thing left to
    flag is the signature mismatch under test."""
    known = (set(template_scope_members(str(MICRO / kind), variant))
             | set(template_types(str(MICRO / kind), variant))
             | {"node", "file", "_selectedNode", "_autosaveInterval",
                "_lastSaveTime", "Time", "Debug"})
    bad = find_unbound_identifiers(
        body, known=known,
        arities=template_method_arities(str(MICRO / kind), variant),
        params=template_method_params(str(MICRO / kind), variant),
        signature=signature)
    return sorted({n for names in bad.values() for n in names})


@pytest.mark.parametrize("kind,variant,body,signature,needle", [
    # AudioDirectorAudioManager.cs(113,25) — Play(string, Vector3? = null).
    # Two arguments is a legal COUNT, which is all anything checked.
    ("audio", "director", 'Play("success", 1f);', "", "needs vector3?"),
    # PointerController.cs(253,46) — ApplyHighlight(GameObject), handed the
    # object's NAME. `.name` is a string on every Unity type.
    ("controller", "pointer", "ApplyHighlight(node.name);",
     "private void SelectCommitNode(GameObject node)", "needs gameobject"),
    # InitializeRepositoryObjectives.cs(134,29) — the event is
    # Action<string,int,int> and every one of the three arguments is wrong.
    # The first is only knowable from the enclosing signature.
    ("objectives", "tracker", 'OnObjectiveProgress(node, 1f, "File Node");',
     "public void CollectFileNode(GameObject node)", "needs string"),
])
def test_a_wrong_argument_type_is_caught(kind, variant, body, signature, needle):
    assert any(needle in f for f in _typed(kind, variant, body, signature))


@pytest.mark.parametrize("kind,variant,body,signature", [
    ("audio", "director", 'Play("success");', ""),
    ("audio", "director", 'Play("success", transform.position);', ""),
    ("objectives", "tracker", 'ReportProgress("obj_1", 2);', ""),
    # null into a nullable, and an int where a float is wanted (a widening
    # conversion) — both legal, and both shapes the checker could get wrong.
    ("audio", "director", 'Play("success", null);', ""),
    ("objectives", "tracker", 'ReportProgress("obj_1", 2);', ""),
])
def test_a_legal_call_is_left_alone(kind, variant, body, signature):
    """The gate NEUTRALISES what it flags, so a false positive deletes working
    code — strictly worse than the CS1503 it would have prevented."""
    assert not [f for f in _typed(kind, variant, body, signature) if "(" in f]


def test_two_class_types_are_never_judged():
    """Inheritance could make it legal and this pass cannot see a base list.
    `Renderer` where `Component` is wanted must not be touched."""
    assert not [f for f in _typed(
        "controller", "pointer", "ApplyHighlight(rend);",
        "private void Go(Renderer rend)") if "ApplyHighlight(" in f]


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

def test_audio_member_call_on_the_wrong_builtin_type_is_neutralized():
    """AudioDirectorAudioManager.cs(119) CS1061 — `_music.PickClip(...)`.
    `_music` is the template's own `private AudioSource _music;`; `PickClip()`
    is a real, zero-argument method — on the unrelated `AudioCue` class the
    same template folder also declares."""
    s = _schema(methods=[
        ("private void PlaySpark()",
         '_music.Stop();\n_music.PickClip("brass_spark");'),
    ])
    msgs = bind_authored_code(s, MICRO / "audio")
    live = "\n".join(ln for m in s.extra_methods for ln in _live_lines(m.body))
    assert "PickClip" not in live
    assert "_music.Stop();" in live             # the legal call is not collateral
    assert any("_music" in m and "PickClip" in m for m in msgs)


def test_health_primitive_field_called_as_if_it_were_an_object():
    """PlayerHealth.cs(62) CS1061 — `_lastHit.CompareTag(...)`. `_lastHit` is
    the template's own invulnerability-timer float, used correctly elsewhere
    as `Time.time - _lastHit`."""
    s = _schema(methods=[
        ("private void OnHit(Collider other)",
         'if (_lastHit != null && _lastHit.CompareTag("Cog")) { _current -= 1f; }'),
    ])
    bind_authored_code(s, MICRO / "health")
    assert not [m for m in s.extra_methods if _live_lines(m.body)]


def test_lighting_struct_field_assigned_a_numeric_expression():
    """LightingDirector.cs(242) CS1503 — `ambientGround = Mathf.Lerp(...)`.
    `ambientGround` is `[SerializeField] private Color ambientGround`, and
    `Mathf.Lerp` always returns float."""
    s = _schema(methods=[
        ("private void SetCoalPalette(float darkness)",
         "ambientGround = Mathf.Lerp(ambientGround, darkness * 0.2f, "
         "Time.deltaTime * 3f);"),
    ])
    bind_authored_code(s, MICRO / "lighting")
    assert not [m for m in s.extra_methods if _live_lines(m.body)]


def test_a_legitimate_call_on_a_known_builtin_field_survives():
    """A false positive here deletes WORKING code — strictly worse than the
    CS1061 it would have prevented. Every member `_music` really has on
    AudioSource must stay untouched."""
    s = _schema(methods=[
        ("private void DuckMusic()",
         "_music.volume = 0.2f; _music.Pause(); _music.UnPause();"),
    ])
    bind_authored_code(s, MICRO / "audio")
    live = "\n".join(ln for m in s.extra_methods for ln in _live_lines(m.body))
    assert "_music.volume = 0.2f;" in live
    assert "_music.Pause();" in live
    assert "_music.UnPause();" in live


def test_numeric_assignment_to_a_numeric_field_is_not_judged():
    """Only struct fields (Color, Vector3, ...) are guarded against a numeric
    RHS — a float field taking a float value is exactly what should happen."""
    s = _schema(methods=[("private void ResetTimer()", "_lastHit = Time.time;")])
    bind_authored_code(s, MICRO / "health")
    assert "_lastHit = Time.time;" in s.extra_methods[0].body


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


def test_the_call_that_shipped_is_now_flagged():
    assert "SetCheckpoint()" in _typed(
        "saveload", "slots",
        "if (_autosaveInterval > 0f) { SetCheckpoint(); _lastSaveTime = Time.time; }")


def test_the_same_call_binds_on_the_variant_that_declares_it():
    assert "SetCheckpoint()" not in _typed(
        "saveload", "checkpoint",
        "if (_autosaveInterval > 0f) { SetCheckpoint(); _lastSaveTime = Time.time; }")


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

def test_a_field_declared_under_another_enum_is_out_of_scope():
    from src.micro.code_fields import render_facts
    from src.micro.spawner import SpawnerSchema

    tdir = str(MICRO / "spawner")
    points = render_facts(SpawnerSchema(spawn_shape="spawn_points"))
    plain  = render_facts(SpawnerSchema(spawn_shape="point"))
    assert "spawnPoints" in template_scope_members(tdir, "wave_spawner", points)
    assert "spawnPoints" not in template_scope_members(tdir, "wave_spawner", plain)
    # `or`-chained atoms are decided as one condition, so `circle`-only fields
    # go with them; a caller that supplies no facts keeps the whole union.
    assert "spawnRadius" not in template_scope_members(tdir, "wave_spawner", plain)
    assert "spawnPoints" in template_scope_members(tdir, "wave_spawner")


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


def test_neutralizing_an_if_head_leaves_its_else_attached():
    """`if (bad) x; else if (bad) y; else z;` — commenting both heads out
    orphans the author's `else` (CS1525). The chain keeps its shape instead."""
    from src.micro.spawner import SpawnerSchema
    s = SpawnerSchema(                      # `point` — so `spawnPoints` is unbindable
        spawn_shape="point",
        extra_fields=[CodeField(name="_i", type="int")],
        update_extra=("if (spawnPoints != null) _i = 0;\n"
                      "else if (_i >= spawnPoints.Length - 1) _i = 0;\n"
                      "else _i++;"),
    )
    bind_authored_code(s, MICRO / "spawner")
    body = s.update_extra
    assert "if (false) { }" in body
    assert "else if (false) { }" in body
    assert "else _i++;" in body            # the surviving branch is untouched
    # Every surviving reference is inside the AUDIT trail, not in live code.
    assert not [ln for ln in _live_lines(body)
                if "spawnPoints" in ln.split("// AUDIT:")[0]]


def test_a_disabled_braceless_head_does_not_adopt_the_next_statement():
    """`if (false)` over a removed body swallows the statement below it — which
    on a typed method is its only unconditional return (CS0161)."""
    from src.micro.spawner import SpawnerSchema
    s = SpawnerSchema(
        spawn_shape="point",
        extra_fields=[CodeField(name="_i", type="int")],
        extra_methods=[CodeMethod(
            signature="private Vector3 GetNextSpawnPosition()",
            body=("if (spawnPoints != null && _i < spawnPoints.Length)\n"
                  "    return spawnPoints[_i].position;\n"
                  "return transform.position;"))],
    )
    bind_authored_code(s, MICRO / "spawner")
    body = s.extra_methods[0].body
    head = next(ln for ln in body.splitlines() if "if (false)" in ln)
    assert head.rstrip().endswith("{ }")
    assert "return transform.position;" in body
