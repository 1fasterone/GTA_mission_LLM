"""
Step 3.5 — LLM-authored per-micro code fields (src/unity_micro_code.py).

Two suppliers write into Subsystem.options: `enrich_subsystems` (one focused
LLM call per subsystem) and `merge_authored_code` (schema.microgens[].input,
no LLM). Both must (a) actually reach the rendered .cs, (b) never be able to
move a GUID / class name / scene fileID, and (c) never abort a build on a
malformed model response.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.unity_compose import MicroComposer, Subsystem, subsystems_for
from src.unity_micro_code import (
    CODE_KEYS,
    code_keys_for,
    enrich_subsystems,
    merge_authored_code,
)
from src.unity_schema import MicrogensSpec


# ── helpers ───────────────────────────────────────────────────────────────────

class FakeLLM:
    """Minimal stand-in for the LM Studio client used by enrich_subsystems."""

    def __init__(self, by_kind: dict[str, dict] | None = None, raises: bool = False):
        self.by_kind = by_kind or {}
        self.raises = raises
        self.calls: list[str] = []

    def chat(self, *, system, user, temperature, thinking):  # noqa: ARG002
        if self.raises:
            raise RuntimeError("model offline")
        # Recover the kind from the prompt body the module builds.
        kind = ""
        for line in user.splitlines():
            if line.strip().startswith("kind:"):
                kind = line.split("kind:", 1)[1].strip().split(" ")[0]
                break
        self.calls.append(kind)
        return json.dumps(self.by_kind.get(kind, {}))

    @staticmethod
    def extract_json(raw: str) -> dict:
        return json.loads(raw)


_SWIM_CODE = {
    "extra_usings": ["UnityEngine.Events"],
    "extra_fields": [{"name": "_oxygen", "type": "float", "default": "100f",
                      "tooltip": "Remaining oxygen"}],
    "update_extra": "if (_oxygen > 0f) { _oxygen -= Time.deltaTime * 2f; }",
    "extra_methods": [{"signature": "private void OnAirEmpty()",
                       "body": "Debug.Log(\"drowned\");"}],
}


def _controller_sub(**opts) -> Subsystem:
    return Subsystem(kind="controller", variant="swimming", target="player",
                     name="DiverController", options=dict(opts))


def _emit(tmp_path: Path, subsystems) -> dict[str, str]:
    assets = tmp_path / "Assets"
    composer = MicroComposer("ProbeProj", project_config={"audio": {"backend": "dsp"}})
    composer.emit(subsystems, assets, has_game_manager=False)
    return {str(f.relative_to(assets)).replace("\\", "/"): f.read_text(encoding="utf-8")
            for f in sorted(assets.rglob("*.cs"))}


# ── vocabulary ────────────────────────────────────────────────────────────────

def test_code_keys_are_per_kind_not_hardcoded():
    # CodeExtensibleSchema subclass — full vocabulary.
    assert code_keys_for("controller") == CODE_KEYS
    # Declaration-only micros: no lifecycle bodies on the schema.
    assert code_keys_for("vfx") == ("extra_usings", "extra_fields", "extra_methods")
    # No support at all, and unknown kinds.
    assert code_keys_for("input") == ()
    assert code_keys_for("model") == ()
    assert code_keys_for("nope") == ()


# ── supplier B — LLM enrichment ───────────────────────────────────────────────

def test_enrichment_reaches_the_rendered_cs(tmp_path):
    sub = _controller_sub()
    enrich_subsystems([sub], llm=FakeLLM({"controller": _SWIM_CODE}),
                      concept="A deep-sea salvage diver runs out of air.")
    files = _emit(tmp_path, [sub])
    src = files["Micros/controller/Scripts/DiverController.cs"]
    for probe in ("_oxygen", "OnAirEmpty", "UnityEngine.Events", "drowned",
                  "Remaining oxygen"):
        assert probe in src, f"{probe!r} missing from composed output"


def test_enrichment_only_asks_micros_that_support_code():
    subs = [_controller_sub(),
            Subsystem(kind="input", variant="keyboard_mouse", name="InputReader")]
    llm = FakeLLM({"controller": _SWIM_CODE})
    msgs = enrich_subsystems(subs, llm=llm, concept="diving")
    assert llm.calls == ["controller"]          # `input` never asked
    assert subs[1].options == {}
    assert any("no code-field support in input" in m for m in msgs)


def test_enrichment_never_raises_when_the_model_is_down():
    sub = _controller_sub()
    msgs = enrich_subsystems([sub], llm=FakeLLM(raises=True), concept="diving")
    assert not any(sub.options.get(k) for k in CODE_KEYS)
    assert any("enrichment failed" in m for m in msgs)


def test_enrichment_skipped_without_a_concept():
    sub = _controller_sub()
    llm = FakeLLM({"controller": _SWIM_CODE})
    msgs = enrich_subsystems([sub], llm=llm, concept="")
    assert llm.calls == []
    assert any("no concept" in m for m in msgs)


# ── payload safety ────────────────────────────────────────────────────────────

def test_non_code_keys_are_never_writable():
    """variant/name/target decide class names and GUIDs — a model must not
    be able to touch them (see the determinism contract)."""
    sub = _controller_sub()
    enrich_subsystems([sub], llm=FakeLLM({"controller": {
        **_SWIM_CODE, "variant": "aircraft", "name": "Hijacked",
        "target": "world", "move_speed": 999.0}}), concept="diving")
    assert sub.variant == "swimming"
    assert sub.name == "DiverController"
    assert sub.target == "player"
    assert "move_speed" not in sub.options
    assert sub.options["extra_fields"] == _SWIM_CODE["extra_fields"]


def test_malformed_fragment_costs_only_itself(tmp_path):
    """A CodeMethod missing its signature would raise ValidationError inside
    MicroComposer._resolve and abort the build; it must be dropped instead,
    leaving the valid keys intact."""
    sub = _controller_sub()
    enrich_subsystems([sub], llm=FakeLLM({"controller": {
        "extra_fields": _SWIM_CODE["extra_fields"],
        "extra_methods": "not a list at all",
    }}), concept="diving")
    assert sub.options.get("extra_fields") == _SWIM_CODE["extra_fields"]
    assert not sub.options.get("extra_methods")
    src = _emit(tmp_path, [sub])["Micros/controller/Scripts/DiverController.cs"]
    assert "_oxygen" in src


# ── unresolvable collaborator types (CS0246) ──────────────────────────────────

# Verbatim shape of what qwen3.5-9b returned live for a "deep-sea diver /
# oxygen" brief: a field typed after a helper class that exists nowhere, and a
# method calling into it.
_INVENTED_COLLABORATOR = {
    "extra_fields": [
        {"name": "_oxygenManager", "type": "OxygenManager", "header": "Runtime"},
        {"name": "_drainRate", "type": "float", "default": "0.5f"},
    ],
    "update_extra": "if (_oxygenManager != null) ApplyDrain();",
    "extra_methods": [{"signature": "private void ApplyDrain()",
                       "body": "_oxygenManager.Consume(_drainRate);"}],
}


def test_invented_collaborator_type_is_dropped(tmp_path):
    sub = _controller_sub()
    msgs = enrich_subsystems([sub], llm=FakeLLM({"controller": _INVENTED_COLLABORATOR}),
                             concept="diver runs out of air")
    assert any("unresolvable collaborator type" in m for m in msgs)

    names = [f["name"] for f in sub.options["extra_fields"]]
    assert "_oxygenManager" not in names
    assert "_drainRate" in names, "the valid float field must survive"

    src = _emit(tmp_path, [sub])["Micros/controller/Scripts/DiverController.cs"]
    assert "private OxygenManager" not in src          # the CS0246 declaration
    # Surviving mentions are only inside AUDIT comments, which deliberately
    # preserve the original line for traceability (same as every other lint_*
    # pass). No live code may reference it.
    live = [ln for ln in src.splitlines()
            if "_oxygenManager" in ln and not ln.lstrip().startswith("//")]
    assert live == [], live
    # The method body depended entirely on the dropped field, so the whole
    # method goes rather than shipping an empty shell.
    assert "private void ApplyDrain()" not in src


def test_sibling_micro_class_is_a_resolvable_type():
    """A field typed after another composed micro compiles — it must not be
    swept up with the invented ones."""
    subs = [_controller_sub(), Subsystem(kind="health", variant="simple",
                                         target="player", name="DiverHealth")]
    enrich_subsystems(subs, llm=FakeLLM({"controller": {
        "extra_fields": [{"name": "_health", "type": "DiverHealth"}]}}),
        concept="diver")
    assert subs[0].options["extra_fields"][0]["type"] == "DiverHealth"


def test_known_unity_types_survive():
    sub = _controller_sub()
    enrich_subsystems([sub], llm=FakeLLM({"controller": {"extra_fields": [
        {"name": "_bubbles", "type": "ParticleSystem"},
        {"name": "_gauge", "type": "Light"},
        {"name": "_marks", "type": "List<Vector3>"},
    ]}}), concept="diver")
    assert len(sub.options["extra_fields"]) == 3


def test_guard_applies_to_the_fallback_path_too():
    sub = _controller_sub()
    merge_authored_code([sub], [MicrogensSpec(kind="controller",
                                              input=dict(_INVENTED_COLLABORATOR))])
    names = [f["name"] for f in sub.options.get("extra_fields", [])]
    assert names == ["_drainRate"]


# ── supplier A — microgens[].input fallback ───────────────────────────────────

def test_fallback_fills_from_microgens_input(tmp_path):
    sub = _controller_sub()
    msgs = merge_authored_code([sub], [MicrogensSpec(kind="controller",
                                                     input=dict(_SWIM_CODE))])
    assert any("schema.microgens[].input" in m for m in msgs)
    src = _emit(tmp_path, [sub])["Micros/controller/Scripts/DiverController.cs"]
    assert "OnAirEmpty" in src


def test_fallback_does_not_overwrite_enrichment():
    sub = _controller_sub()
    enrich_subsystems([sub], llm=FakeLLM({"controller": _SWIM_CODE}), concept="diving")
    merge_authored_code([sub], [MicrogensSpec(kind="controller", input={
        "update_extra": "Debug.Log(\"stale\");"})])
    assert sub.options["update_extra"] == _SWIM_CODE["update_extra"]


def test_fallback_ignores_non_subsystem_microgens():
    sub = _controller_sub()
    assert merge_authored_code([sub], [MicrogensSpec(kind="intro_sequence",
                                                     input={"update_extra": "x;"})]) == []
    assert sub.options == {}


# ── the wiring invariant ──────────────────────────────────────────────────────

def test_enrichment_cannot_move_a_guid_class_name_or_file_id():
    """The whole point: bodies vary, the wiring layer does not."""
    plain = subsystems_for("3D", ["audio"], locomotion="walk", genre="universal")
    coded = subsystems_for("3D", ["audio"], locomotion="walk", genre="universal")
    enrich_subsystems(coded, llm=FakeLLM({s.kind: _SWIM_CODE for s in coded}),
                      concept="A deep-sea salvage diver runs out of air.")
    assert any(s.options.get("extra_fields") for s in coded), "nothing was enriched"

    composer = MicroComposer("GoldenProj", project_config={"audio": {"backend": "dsp"}})
    before = [(u.kind, u.class_name, u.guid, u.target, u.file_id)
              for u in composer.plan(plain)]
    after = [(u.kind, u.class_name, u.guid, u.target, u.file_id)
             for u in composer.plan(coded)]
    assert before == after


def test_same_options_still_emit_byte_identical_bytes(tmp_path):
    """Step 6 stays a pure function of its input — enrichment widens the input,
    it does not add nondeterminism to codegen."""
    subs = subsystems_for("3D", ["audio"], locomotion="walk", genre="universal")
    enrich_subsystems(subs, llm=FakeLLM({s.kind: _SWIM_CODE for s in subs}),
                      concept="A deep-sea salvage diver runs out of air.")
    assert _emit(tmp_path / "a", subs) == _emit(tmp_path / "b", subs)
