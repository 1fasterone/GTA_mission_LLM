"""
Step 3.5a — LLM-authored bodies for `schema.scripts` (`enrich_scripts`).

`enrich_subsystems` has always authored bodies for composed MICROS. Nothing did
the same for prompt-authored scripts, and a `type: custom` script's template is
a bare shell: it renders `extra_fields` and the `extra_*` bodies and declares
nothing else. So the class emitted had fields and no behaviour, and was attached
to a live GameObject anyway.

Measured on CodeBranchSim (C:\\u\\CodeBranchSim): 14 prompt-specific method
blocks across the micros, 0 across the four custom scripts. Their schema entries
came back `"awake_extra": ""`, `"extra_methods": []` — the generator was
faithful; the fields simply had no producer.

Fixture provenance: the four scripts below are TEST DATA transcribed from that
build's schema. The LLM is a stub — no network, no model.
"""

from __future__ import annotations

import pytest

from src.unity_micro_code import (enrich_scripts, _script_contract,
                                  _script_has_code, _script_would_not_bind)
from src.unity_schema import CodeField, CodeMethod, UnityScript


def _codebranchsim() -> list[UnityScript]:
    """The four custom scripts as Step 3 really wrote them: declarations
    filled, every behaviour field empty."""
    return [
        UnityScript(name="FileNodeController", type="custom",
                    description="Handles dragging and dropping file node sprites.",
                    extra_usings=["System.Collections.Generic"],
                    extra_fields=[CodeField(name="_fileNodes",
                                            type="List<GameObject>",
                                            default="new List<GameObject>()")]),
        UnityScript(name="RepositoryState", type="custom",
                    description="Tracks repository lifecycle events.",
                    extra_fields=[CodeField(name="_commitCount", type="int",
                                            default="0")]),
        UnityScript(name="CommitGraphManager", type="custom",
                    description="Manages commit nodes on a timeline.",
                    extra_fields=[CodeField(name="_timelineTransform",
                                            type="Transform", default="null")]),
        UnityScript(name="CodeTextRenderer", type="custom",
                    description="Renders code snippets with syntax highlighting.",
                    extra_fields=[]),
    ]


class _StubLLM:
    """Returns a fixed payload and records what it was asked. No network."""

    def __init__(self, payload=None, fail=False):
        self._payload = payload if payload is not None else {
            "update_extra": "if (_commitCount > 0) transform.Rotate(0f, 1f, 0f);",
            "extra_methods": [{"signature": "public void Commit()",
                               "body": "_commitCount++;"}],
        }
        self._fail = fail
        self.prompts: list[str] = []

    def chat(self, *, system, user, temperature=0.0, thinking=False):
        self.prompts.append(user)
        if self._fail:
            raise RuntimeError("model unavailable")
        return "{}"

    def extract_json(self, _raw):
        if self._fail:
            raise RuntimeError("model unavailable")
        return dict(self._payload)


# ── The gap this closes ──────────────────────────────────────────────────────

def test_an_empty_custom_script_is_a_target():
    assert all(not _script_has_code(s) for s in _codebranchsim())


def test_a_method_with_a_blank_body_is_not_code():
    """Live: VectorCore's SurfaceModifier came back from Step 3 with
    `extra_methods=[{"body": ""}]` — a signature and no body. A plain
    truthiness check over `extra_methods` reads that as "already has code"
    (a non-empty list), so this pass skipped it entirely; nothing else ever
    gave it real behaviour, and it reached codegen as a hollow shell."""
    s = UnityScript(name="SurfaceModifier", type="custom",
                    description="d",
                    extra_methods=[CodeMethod(signature="void Foo()", body="")])
    assert not _script_has_code(s)


def test_a_whitespace_only_fragment_is_not_code():
    s = UnityScript(name="X", type="custom", description="d",
                    update_extra="   \n  ")
    assert not _script_has_code(s)


def test_bodies_are_authored_for_every_empty_custom_script():
    scripts = _codebranchsim()
    msgs = enrich_scripts(scripts, llm=_StubLLM(), concept="A git teaching sim.")
    assert all(s.update_extra and s.extra_methods for s in scripts)
    assert any("authored behaviour for 4/4" in m for m in msgs)


def test_a_script_that_already_has_code_is_left_alone():
    """The supplier is a fallback behind Step 3, not an overwrite of it."""
    scripts = _codebranchsim()
    scripts[0].update_extra = "// authored at schema-fill"
    llm = _StubLLM()
    enrich_scripts(scripts, llm=llm, concept="x")
    assert scripts[0].update_extra == "// authored at schema-fill"
    # Never asked about at all — not asked and then discarded.
    assert not any("FileNodeController" in p for p in llm.prompts)
    assert any("RepositoryState" in p for p in llm.prompts)


def test_a_typed_script_is_not_touched():
    """GameManager.cs.j2 already carries its behaviour. Injecting an
    update_extra into a class that has its own Update loop is a different and
    riskier change than filling an empty shell."""
    scripts = [UnityScript(name="GameManager", type="game_manager",
                           description="Singleton game manager")]
    llm = _StubLLM()
    assert enrich_scripts(scripts, llm=llm, concept="x") == []
    assert not llm.prompts and not scripts[0].update_extra


# ── The contract the author is shown ─────────────────────────────────────────

def test_the_contract_names_the_fields_the_script_really_has():
    script = _codebranchsim()[0]
    contract = _script_contract(script, [])
    assert "List<GameObject> _fileNodes" in contract
    assert "MonoBehaviour" in contract


def test_a_script_with_no_fields_is_told_to_declare_them():
    contract = _script_contract(_codebranchsim()[3], [])
    assert "none — declare what you need" in contract


def test_only_a_siblings_PUBLIC_methods_are_offered():
    """A sibling's extra_fields always render `[SerializeField] private`, so
    reaching one is CS0122 — which is exactly what the generator's
    _sanitize_project_service_calls strips after the fact."""
    scripts = _codebranchsim()
    scripts[1].extra_methods = [
        CodeMethod(signature="public void Commit()", body="_commitCount++;"),
        CodeMethod(signature="private void Tick()", body="// internal"),
    ]
    contract = _script_contract(scripts[0], scripts)
    assert "GameContext.Instance.Get<RepositoryState>()" in contract
    assert "public void Commit()" in contract
    assert "Tick" not in contract


def test_composed_micros_offer_only_their_verified_public_api():
    """The same table `unity_generator._sanitize_project_service_calls` uses to
    strip a call it can't verify — shown up front instead of only at judge
    time. Confirmed live: OxygenManager called Get<PlayerHealth>().Die()
    (private, CS0122); the author never had a reason not to reach for it."""
    from src.unity_compose import Subsystem
    subsystems = [Subsystem(kind="health", target="player", name="PlayerHealth"),
                  Subsystem(kind="camera", target="camera", name="PlayerCamera")]
    contract = _script_contract(_codebranchsim()[0], [], subsystems)
    assert "GameContext.Instance.Get<PlayerHealth>()" in contract
    assert "TakeDamage" in contract
    assert "Die" not in contract          # private — never offered
    assert "GameContext.Instance.Get<PlayerCamera>()" in contract
    assert "no public members beyond MonoBehaviour basics" in contract


def test_a_composed_kind_with_no_verified_api_stays_unmentioned():
    """VFX has no _COMPOSED_CLASS_API entry — no ground truth to show, so
    (same restraint _composed_member_ok applies at judge time) it is left
    out rather than guessed at. (LightingDirector used to be this example —
    it moved once LightingDirector got a verified entry; see
    _COMPOSED_CLASS_API's own docstring for why that was a live gap.)"""
    from src.unity_compose import Subsystem
    subsystems = [Subsystem(kind="vfx", target="world", name="VFX")]
    contract = _script_contract(_codebranchsim()[0], [], subsystems)
    assert "VFX" not in contract


def test_no_compose_means_no_composed_systems_section():
    contract = _script_contract(_codebranchsim()[0], [])
    assert "COMPOSED PROJECT SYSTEMS" not in contract


def test_the_prompt_carries_the_concept_and_the_scripts_own_job():
    scripts = _codebranchsim()
    llm = _StubLLM()
    enrich_scripts(scripts, llm=llm, concept="A git teaching sim.",
                   user_prompt="nine file icons on a desk")
    assert "A git teaching sim." in llm.prompts[0]
    assert "nine file icons on a desk" in llm.prompts[0]
    assert "Handles dragging and dropping" in llm.prompts[0]


def test_enrich_scripts_passes_subsystems_into_the_contract():
    """unity_pipeline.run() now computes `subsystems` before this call so the
    writer can be shown the real composed-micro surface — this pins the plumbing."""
    from src.unity_compose import Subsystem
    scripts = _codebranchsim()
    llm = _StubLLM()
    enrich_scripts(scripts, llm=llm, concept="A git teaching sim.",
                   subsystems=[Subsystem(kind="health", target="player",
                                         name="PlayerHealth")])
    assert "GameContext.Instance.Get<PlayerHealth>()" in llm.prompts[0]


# ── Binding feedback, the same loop the micros get ───────────────────────────

def test_an_undeclared_name_is_reported_back_to_the_author():
    script = _codebranchsim()[1]
    cuts = _script_would_not_bind(
        script, {"update_extra": "_undeclaredThing += Time.deltaTime;"})
    assert "_undeclaredThing" in cuts


def test_a_field_the_payload_itself_declares_binds():
    script = _codebranchsim()[1]
    payload = {"extra_fields": [{"name": "_timer", "type": "float", "default": "0f"}],
               "update_extra": "_timer += Time.deltaTime;"}
    assert not _script_would_not_bind(script, payload)


def test_a_method_binds_against_its_own_parameters():
    script = _codebranchsim()[0]
    payload = {"extra_methods": [{"signature": "public void Add(GameObject node)",
                                  "body": "_fileNodes.Add(node);"}]}
    assert not _script_would_not_bind(script, payload)


def test_the_author_gets_a_correction_round():
    class _Fixes(_StubLLM):
        def __init__(self):
            super().__init__()
            self.n = 0

        def extract_json(self, _raw):
            self.n += 1
            if self.n == 1:
                return {"update_extra": "_ghost += 1;"}
            return {"extra_fields": [{"name": "_ghost", "type": "int",
                                      "default": "0"}],
                    "update_extra": "_ghost += 1;"}

    scripts = [_codebranchsim()[1]]
    llm = _Fixes()
    msgs = enrich_scripts(scripts, llm=llm, concept="x")
    assert any("re-authored after" in m for m in msgs)
    assert {f.name for f in scripts[0].extra_fields} >= {"_ghost"}
    assert "DOES NOT COMPILE" in llm.prompts[1]


# ── Failure is loud, never silent ────────────────────────────────────────────

def test_a_failing_model_leaves_the_shell_and_says_so():
    scripts = _codebranchsim()
    msgs = enrich_scripts(scripts, llm=_StubLLM(fail=True), concept="x")
    assert all(not _script_has_code(s) for s in scripts)
    assert any("stay empty shells" in m for m in msgs)
    assert any("enrichment failed" in m for m in msgs)


def test_no_concept_abstains_loudly():
    msgs = enrich_scripts(_codebranchsim(), llm=_StubLLM(), concept="")
    assert any("no concept available" in m for m in msgs)


def test_a_malformed_method_costs_only_itself():
    """pydantic validates on assignment here, unlike a Subsystem's untyped
    options dict — so a bad row is dropped rather than carried into codegen."""
    llm = _StubLLM(payload={
        "update_extra": "transform.Rotate(0f, 1f, 0f);",
        "extra_methods": [{"body": "no signature at all"},
                          {"signature": "public void Good()", "body": "return;"}],
    })
    scripts = [_codebranchsim()[1]]
    enrich_scripts(scripts, llm=llm, concept="x")
    assert [m.signature for m in scripts[0].extra_methods] == ["public void Good()"]
    assert scripts[0].update_extra


@pytest.mark.parametrize("payload", [
    {}, {"name": "Renamed"}, {"variant": "other"}, {"nonsense": 1},
])
def test_nothing_outside_the_code_vocabulary_is_ever_written(payload):
    """The determinism contract: a model may write bodies, never identity."""
    scripts = [_codebranchsim()[1]]
    before = scripts[0].name
    enrich_scripts(scripts, llm=_StubLLM(payload=payload), concept="x")
    assert scripts[0].name == before
    assert not _script_has_code(scripts[0])


# ── The two halves must agree ────────────────────────────────────────────────

def test_the_playability_check_and_the_supplier_share_one_definition():
    """Step 7 grades a script as bodiless; Step 3.5a decides whether to author
    one. If the two lists drift, the evaluator reports a defect the supplier
    already declined to fix, or the reverse."""
    from src.unity_micro_code import _BEHAVIOUR_KEYS
    from src.unity_playability import _SCRIPT_BEHAVIOUR_KEYS
    assert set(_BEHAVIOUR_KEYS) == set(_SCRIPT_BEHAVIOUR_KEYS)


def test_the_schema_prompt_shows_a_filled_custom_script():
    """Step 3's JSON skeleton is what the model copies, and it showed every code
    field pre-emptied — contradicting the CUSTOM SCRIPTS guidance 120 lines
    above it, which carries a fully worked example. The skeleton wins, which is
    why four custom scripts shipped with `"awake_extra": ""`.

    It now demonstrates the filled shape and says outright what an empty one
    costs. The instruction lives in the placeholder VALUES, the way every other
    entry in this skeleton carries its own — prose between the keys would sit
    inside the very object the model is told to emit.
    """
    import json
    import string

    from src.unity_prompts import UNITY_SCHEMA_FILL_USER as T

    keys = {f[1] for f in string.Formatter().parse(T) if f[1]}
    text = T.format(**{k: "" for k in keys})
    start = text.index("OUTPUT this exact JSON structure")
    blob = text[text.index("{", start):]
    depth = 0
    for i, ch in enumerate(blob):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            blob = blob[:i + 1]
            break
    script = json.loads(blob)["scripts"][0]
    assert script["extra_fields"] and script["extra_methods"]
    assert "NOTHING ELSE RENDERS" in script["update_extra"]
    assert script["awake_extra"].strip()
