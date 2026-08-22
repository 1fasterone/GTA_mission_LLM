"""
E2E stress test — HUD under maximum adversarial load.

HUD is the richest content model of any micro-generator: dynamic widgets (five
kinds, per-project ids), ten variants x two backends, and three wiring seams
(SEAM 1/9/10) that publish into it from elsewhere in the composed project. It
was also the direct subject of the backend-mismatched-field bug (`_root`
referenced under a uGUI backend — see micro/hud.py's `neutralize_unavailable_
names` call) and sits behind three of the composed-sibling-call sanitizers
(arity, invented constructors, GetComponent-family arity) that a unit test can
only ever poke one at a time.

This drives HUD through the REAL composed pipeline — `subsystems_for` (the
same function a live `--compose` run uses to build the project's subsystem
list), the same `sanitize_subsystem_*` passes `unity_pipeline.py` runs before
composer.emit(), then a real `MicroComposer.emit()` producing an actual
`GameHud.cs` on disk — with every sanitizer's poison pill present at once,
plus widget-count/collision stress beyond anything a single unit test
exercises, and confirms none of the poison reaches the rendered file while
legitimate content survives untouched.

A finding this test run surfaced along the way, worth recording here since it
shapes what "legitimate content" means below: `harden_against_template`
(code_fields.py, run on EVERY micro-generator schema via `UnityMicroPipeline.
harden()`) derives its known-identifier set purely from parsing that
generator's OWN template — `GameContext` is not in `_KNOWN_SYMBOLS` and no
composed-sibling class name ever is, so it neutralizes ANY
`GameContext.Instance.Get<T>()` reference authored into a micro-generator's
own extra_* fields wholesale, correct or not. That is a much blunter net than
this project's `_COMPOSED_CLASS_API`-based checks, and it means a composed
cross-subsystem call never survives inside microgen content regardless of
whether it is valid — only inside a `type: custom` script (which never goes
through `harden_against_template` at all) does a VALID one actually survive.
This test's "legitimate content" therefore uses only what a micro-generator's
own extra_* fields can realistically say about themselves (self-declared
fields, `Time.deltaTime`, a zero-arg `GetComponent<T>()`) — a composed-sibling
call belongs in the "must never survive" set on every path checked here.

Offline and deterministic — no LLM, no Unity. The optional final check shells
out to the real Roslyn syntax gate (see csharp_syntax_gate.py) when a .NET SDK
is available; it no-ops (never fails) otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_micro_code import (
    sanitize_subsystem_builtin_call_arity,
    sanitize_subsystem_composed_calls,
    sanitize_subsystem_invented_constructors,
)

# One poison line per sanitizer this project has (each keyed to the bug that
# motivated it), all aimed at GameHud from a single project's worth of authored
# content — the shape a genuinely adversarial (or just confidently wrong) LLM
# fill would produce, not four separate isolated unit-test fixtures.
_POISON = {
    # backend-mismatched field (neutralize_unavailable_names, micro/hud.py) —
    # `_root` is UI-Toolkit-only; live under a uGUI backend it's CS0103.
    "root_ref": "if (_root != null) _root.style.opacity = 1f;",
    # invented constructor, never touches a declared field
    # (_sanitize_invented_constructor_calls / sanitize_subsystem_invented_constructors)
    "invented_ctor": "Graphics.Blit(source, dest, new ShaderPass(profile), 0);",
    # real member, wrong arity, on a composed sibling
    # (_COMPOSED_METHOD_ARITY / sanitize_subsystem_composed_calls)
    "arity_mismatch": "GameContext.Instance.Get<PauseMenu>()?.Activate();",
    # invented member on a composed sibling that IS in _COMPOSED_CLASS_API
    # (the LightingDirector coverage gap)
    "invented_member": "GameContext.Instance.Get<LightingDirector>()?.SetTimeOfDay(0.5f);",
    # GetComponent-family call, wrong arity (_MB_METHOD_ARITY)
    "getcomponent_arity": "var rb = GetComponent<Rigidbody>(gameObject, true);",
}
# Content a micro-generator's own extra_* fields can legitimately say about
# THEMSELVES — self-declared fields, well-known Unity statics, a valid
# zero-arg GetComponent<T>() — never a composed-sibling reference (see the
# module docstring: harden_against_template strips those wholesale,
# correct or not, so that shape belongs in _POISON on every path, not here).
_LEGIT_FIELDS = [{"name": "_flashTimer", "type": "float", "default": "0f"}]
_LEGIT = {
    "decay": "_flashTimer -= Time.deltaTime;",
    "getcomponent_ok": "var rb = GetComponent<Rigidbody>();",
}

# Every widget kind at once, plus a setter collision with the racing_hud
# baseline (Speed -> SetSpeed, already emitted by the variant itself) and a
# duplicate id (Contacts twice) — both must be dropped by _lint_widgets, not
# just tolerated, or the class ships CS0111 (duplicate member).
_STRESS_WIDGETS = [
    {"id": "Oxygen",   "kind": "bar",       "label": "OXYGEN",   "max_value": 100},
    {"id": "Contacts", "kind": "counter",   "label": "CONTACTS"},
    {"id": "DiveTime", "kind": "timer",     "label": "DIVE TIME"},
    {"id": "MainQuest","kind": "objective", "label": "QUEST"},
    {"id": "Compass",  "kind": "text",      "label": "COMPASS"},
    {"id": "Speed",    "kind": "bar",       "label": "SPEED"},      # collides: racing_hud SetSpeed
    {"id": "Contacts", "kind": "counter",   "label": "DUPLICATE"}, # duplicate of the first Contacts
]


def _live_code(src: str) -> str:
    """`src` with every `// AUDIT:` line dropped — a plain `split("// AUDIT:
    ")[0]` only keeps the text BEFORE the FIRST such comment, wrongly treating
    every real line after it as dead too, when audit lines are interleaved
    with live ones throughout the file."""
    return "\n".join(line for line in src.splitlines()
                     if "// AUDIT:" not in line)


def _sanitize_subsystems(subs, composed_units) -> None:
    """Exactly the order unity_pipeline.py runs before composer.emit()."""
    sanitize_subsystem_composed_calls(subs, composed_units)
    sanitize_subsystem_invented_constructors(subs, composed_units)
    sanitize_subsystem_builtin_call_arity(subs)


def _build_stressed_project(tmp_path: Path, backend: str) -> str:
    """A full composed project (subsystems_for — the same call --compose makes)
    with GameHud's own content replaced by the maximal poison+legit+widget
    payload above, sanitized and emitted exactly like the real pipeline, and
    the rendered GameHud.cs returned."""
    subs = subsystems_for("3D", genre="racing", locomotion="walk")
    hud = next(s for s in subs if s.kind == "hud")
    assert hud.variant == "racing_hud", "test assumes the richest HUD baseline"

    hud.options["backend"] = backend
    hud.options["widgets"] = _STRESS_WIDGETS
    hud.options["extra_fields"] = _LEGIT_FIELDS
    poison_block = "\n".join(_POISON.values())
    legit_block = "\n".join(_LEGIT.values())
    hud.options["update_extra"] = poison_block + "\n" + legit_block
    hud.options["extra_methods"] = [{
        "signature": "private void OnBossPhaseTwo()",
        "body": poison_block,
    }]

    plan_composer = MicroComposer("HudStress", project_config={"audio": {"backend": "dsp"}})
    composed_units = plan_composer.plan(subs)
    _sanitize_subsystems(subs, composed_units)

    # Every poison line must be gone from the SUBSYSTEM options before a
    # schema is even built from them — this is what the pipeline actually
    # gates on; the rendered file (checked below) confirms it stuck.
    for name, line in _POISON.items():
        if name == "root_ref":
            continue   # backend-conditional; only micro/hud.py's own default_fix can judge it
        assert line not in hud.options["update_extra"], f"{name!r} survived subsystem sanitization"
        assert line not in hud.options["extra_methods"][0]["body"], f"{name!r} survived (extra_methods)"

    assets = tmp_path / "Assets"
    emit_composer = MicroComposer("HudStress", project_config={"audio": {"backend": "dsp"}})
    emit_composer.emit(subs, assets, has_game_manager=False)
    return (assets / "Micros" / "hud" / "Scripts" / "GameHud.cs").read_text(encoding="utf-8")


@pytest.mark.parametrize("backend", ["uGUI", "UIToolkit"])
def test_hud_poison_never_reaches_the_rendered_file(tmp_path, backend):
    src = _build_stressed_project(tmp_path / backend, backend)

    live_code = _live_code(src)
    for name, line in _POISON.items():
        if name == "root_ref":
            continue   # asserted separately below — its fate depends on `backend`
        assert line not in src, f"{name!r} reached GameHud.cs at all"

    if backend == "uGUI":
        # _root does not exist under this backend — live only inside the
        # AUDIT trail, never as executable code.
        assert "_root" not in live_code
        assert "// AUDIT: removed reference to unavailable field" in src
    else:
        # Under UIToolkit, _root is real — the same line must survive.
        assert "_root.style.opacity = 1f" in live_code


@pytest.mark.parametrize("backend", ["uGUI", "UIToolkit"])
def test_hud_legitimate_content_survives_the_same_pass(tmp_path, backend):
    src = _build_stressed_project(tmp_path / backend, backend)
    # Must be LIVE code, not merely present somewhere in the file — the
    # backend-mismatch neutralizer (and harden_against_template) both leave
    # the original line quoted inside a "// AUDIT:" comment when they strip
    # something, so a bare substring check would pass even for dead code.
    live_code = _live_code(src)
    for name, line in _LEGIT.items():
        assert line in live_code, f"legitimate line {name!r} was over-stripped"


@pytest.mark.parametrize("backend", ["uGUI", "UIToolkit"])
def test_hud_widget_collisions_and_duplicates_are_dropped_not_just_tolerated(tmp_path, backend):
    src = _build_stressed_project(tmp_path / backend, backend)
    # Speed collided with racing_hud's own SetSpeed(float) — must not gain a
    # second, incompatible SetSpeed(...) overload/duplicate.
    assert src.count("public void SetSpeed(") == 1
    # The duplicate Contacts widget must not double-emit SetContacts.
    assert src.count("SetContacts(") <= (2 if backend == "uGUI" else 1)
    # Every surviving widget still gets its typed setter.
    for setter in ("SetOxygen", "SetDiveTime", "SetMainQuest", "SetCompass"):
        assert setter in src


def test_hud_stressed_output_passes_the_real_syntax_gate(tmp_path):
    """Belt-and-suspenders: shell out to the actual Roslyn parser used before
    a project ships (see csharp_syntax_gate.py). Syntax-only — it cannot see
    the hallucinated-identifier class of bug the sanitizers above exist for
    (that needs binding, which this gate deliberately skips — see its module
    docstring) — but it catches the OTHER failure mode: a sanitizer that
    strips a line badly enough to leave a dangling brace or an unterminated
    statement behind. No-ops (never fails) without a .NET SDK installed.
    """
    from src.csharp_syntax_gate import check_files

    src = _build_stressed_project(tmp_path, "uGUI")
    cs_path = tmp_path / "GameHud.cs"
    cs_path.write_text(src, encoding="utf-8")
    result = check_files([cs_path])
    if result.skipped:
        pytest.skip(result.skipped_reason)
    assert result.ok, "\n".join(str(d) for d in result.diagnostics)
