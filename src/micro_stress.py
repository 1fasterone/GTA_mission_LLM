"""
Depth-stress every Unity micro-generator, one kind at a time, through the REAL
composed pipeline: a single-subsystem project, `MicroComposer.plan()`, the
same `sanitize_subsystem_*` passes `unity_pipeline.py` runs before
`composer.emit()`, then a real `emit()` producing actual `.cs` files on disk —
mirroring `tests/test_hud_e2e_stress.py`'s harness generalized across the
whole registry instead of hand-tuned for one kind.

Every kind gets the SAME adversarial payload (sized to probe every sanitizer
layer this project has — see `_POISON`), across EVERY variant it declares and
both backends where its schema supports one ("depth" = structural coverage,
not just one lucky config), plus a handful of `extra_fields` colliding with
that kind's own `_RESERVED_FIELD_NAMES` to stress the per-kind collision-drop
logic. `run_all`/`run_one` return a report; nothing here prints on its own —
callers (the `micro-stress-test` CLI command, or a pytest parametrization)
decide how to surface it.

A note on what this can and cannot prove, learned building the HUD version of
this harness: `harden_against_template` (code_fields.py), which every kind
gets automatically, does not know `GameContext` or any composed-sibling class
name — it neutralizes ANY `GameContext.Instance.Get<T>()` reference from a
micro-generator's own content wholesale, correct or not. So the composed-
sibling poison lines here mostly re-confirm that blunter net rather than
uniquely proving this project's arity-aware sanitizers fired — the lines that
DO isolate those (an invented constructor whose every token otherwise
resolves, a GetComponent call with a real overload's argument count) are the
ones this harness leans on for that claim. See test_hud_e2e_stress.py's
module docstring for the full story.

Offline and deterministic — no LLM, no Unity. The optional syntax-gate pass
shells out to the real Roslyn parser when a .NET SDK is available and no-ops
(never fails) otherwise.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from src.micro import REGISTRY
from src.unity_compose import MicroComposer, Subsystem
from src.unity_micro_code import (
    code_keys_for,
    sanitize_subsystem_builtin_call_arity,
    sanitize_subsystem_composed_calls,
    sanitize_subsystem_invented_constructors,
)

_PROJECT_CONFIG = {"audio": {"backend": "dsp"}}

# `model` has no schema_cls at all (it drives Blender, not a .cs.j2 template —
# nothing here applies). `input` has a schema but zero code-authoring surface
# (code_keys_for returns ()) — it still gets a structural/variant sweep below,
# just no content poison (there is nowhere to put it).
_NO_SCHEMA = {"model"}

# One poison line per mechanism this project has a name for, all aimed at
# whichever kind is under test. Keys double as the finding label.
_POISON: dict[str, str] = {
    # Invented constructor whose OTHER tokens are all well-known (gameObject,
    # a Camera field) — isolates _sanitize_invented_constructor_calls /
    # sanitize_subsystem_invented_constructors specifically: harden_against_
    # template's unbound-identifier scan has nothing else to flag here.
    "invented_ctor_isolated": "Graphics.Blit(gameObject.GetComponent<Camera>().targetTexture, null, new ShaderPass(gameObject), 0);",
    # Same defect, but every OTHER token is ALSO unknown — the shape that hit
    # live (see unity_generator.py's docstring), kept as a regression pin.
    "invented_ctor_live_shape": "Graphics.Blit(source, dest, new ShaderPass(profile), 0);",
    # Real member, wrong arity, on a composed sibling — defense-in-depth with
    # harden_against_template's wholesale GameContext strip (see module doc).
    "composed_arity_mismatch": "GameContext.Instance.Get<PauseMenu>()?.Activate();",
    "composed_invented_member": "GameContext.Instance.Get<LightingDirector>()?.SetTimeOfDay(0.5f);",
    # GetComponent-family call, wrong arity — isolates _MB_METHOD_ARITY /
    # sanitize_subsystem_builtin_call_arity: every token here (GetComponent,
    # gameObject, Rigidbody, true) is individually well-known, so harden_
    # against_template's unbound-identifier scan finds nothing wrong at all.
    "getcomponent_arity": "var rb = GetComponent<Rigidbody>(gameObject, true);",
}
# UI-Toolkit-only field, poisoned only when the kind under test declares a
# `backend` field and we're exercising the uGUI side of it (see hud.py /
# menu.py's `neutralize_unavailable_names` call).
_BACKEND_MISMATCH_POISON = "if (_root != null) _root.style.opacity = 1f;"


@dataclass
class Finding:
    kind: str
    variant: str
    backend: str
    poison_key: str


@dataclass
class KindResult:
    kind: str
    variants_tested: int = 0
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None   # e.g. "no schema_cls" — not a failure, nothing to test

    @property
    def ok(self) -> bool:
        return self.error is None and not self.findings


def _live_code(src: str) -> str:
    """`src` with every `// AUDIT:` line dropped (see test_hud_e2e_stress.py's
    `_live_code` for why a plain substring check is not safe here)."""
    return "\n".join(line for line in src.splitlines() if "// AUDIT:" not in line)


def _target_subsystems(kind_under_test: str) -> list[Subsystem]:
    """Real composed siblings for the composed-sibling poison lines to
    target — added as genuine Subsystems (not fabricated ComposedUnits) so
    `plan()`'s derived class names/GUIDs agree with what `emit()` later
    renders. Skips the one matching the kind under test to avoid a name
    collision with the stressed subsystem itself."""
    candidates = [
        Subsystem(kind="menu", variant="pause_menu", target="ui", name="PauseMenu"),
        Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth"),
        Subsystem(kind="lighting",
                  variant=REGISTRY["lighting"].default_variant,
                  target="world", name="LightingDirector"),
    ]
    return [s for s in candidates if s.kind != kind_under_test]


def _reserved_field_poison(kind: str) -> list[dict]:
    """A few fields colliding with this kind's OWN `_RESERVED_FIELD_NAMES` —
    stresses the collision-drop logic every generator's default_fix runs
    (lint_fields), same as HUD's widget-collision test but generic to any
    kind's own extra_fields channel."""
    reserved = sorted(getattr(REGISTRY[kind], "_RESERVED_FIELD_NAMES", None) or [])[:3]
    return [{"name": n, "type": "float", "default": "0f"} for n in reserved]


def run_one(kind: str, *, variant: str, backend: str | None) -> list[Finding]:
    """Stress one (kind, variant, backend) combination; return live poison
    that survived, if any."""
    keys = code_keys_for(kind)
    poison_lines = dict(_POISON)
    options: dict = {}
    if backend:
        options["backend"] = backend
        if backend == "uGUI":
            poison_lines["backend_mismatch"] = _BACKEND_MISMATCH_POISON

    block = "\n".join(poison_lines.values())
    if "update_extra" in keys:
        options["update_extra"] = block
    elif "awake_extra" in keys:
        options["awake_extra"] = block
    if "extra_methods" in keys:
        options["extra_methods"] = [{"signature": "private void StressMethod()", "body": block}]
    if "extra_fields" in keys:
        options["extra_fields"] = _reserved_field_poison(kind)
    if not options.get("update_extra") and not options.get("awake_extra") \
            and not options.get("extra_methods"):
        poison_lines = {}   # nowhere to put content — structural-only sweep

    name = "".join(w.title() for w in kind.split("_")) + "Stress"
    stressed = Subsystem(kind=kind, variant=variant, target="world", name=name, options=options)
    subs = [stressed, *_target_subsystems(kind)]

    plan_composer = MicroComposer("MicroStress", project_config=_PROJECT_CONFIG)
    composed_units = plan_composer.plan(subs)
    sanitize_subsystem_composed_calls(subs, composed_units)
    sanitize_subsystem_invented_constructors(subs, composed_units)
    sanitize_subsystem_builtin_call_arity(subs)

    with tempfile.TemporaryDirectory() as d:
        assets = Path(d) / "Assets"
        emit_composer = MicroComposer("MicroStress", project_config=_PROJECT_CONFIG)
        emit_composer.emit(subs, assets, has_game_manager=False)
        kind_dir = assets / "Micros" / kind
        src = "\n".join(f.read_text(encoding="utf-8", errors="replace")
                        for f in sorted(kind_dir.rglob("*")) if f.is_file()
                        and f.suffix in (".cs", ".uxml", ".uss"))

    live = _live_code(src)
    findings = []
    for poison_key, line in poison_lines.items():
        if poison_key == "backend_mismatch":
            # Must be gone from LIVE code specifically — it legitimately
            # survives, unaudited, when this kind's schema has no `backend`
            # field at all (never authored in the first place, not "caught").
            if line in live:
                findings.append(Finding(kind, variant, backend or "-", poison_key))
            continue
        if line in live:
            findings.append(Finding(kind, variant, backend or "-", poison_key))
    return findings


def run_kind(kind: str) -> KindResult:
    """Every variant this kind declares, both backends where it has one."""
    if kind not in REGISTRY:
        return KindResult(kind=kind, error=f"unknown kind {kind!r}")
    if kind in _NO_SCHEMA:
        return KindResult(kind=kind, skipped="no schema_cls (drives Blender, not a .cs.j2 template)")
    pipeline_cls = REGISTRY[kind]
    variants = list(getattr(pipeline_cls, "variants", None) or [""])
    has_backend = "backend" in code_keys_for(kind) or _schema_has_backend(kind)
    backends: list[str | None] = ["uGUI", "UIToolkit"] if has_backend else [None]

    result = KindResult(kind=kind)
    try:
        for variant in variants:
            for backend in backends:
                result.findings.extend(run_one(kind, variant=variant, backend=backend))
                result.variants_tested += 1
    except Exception as exc:                       # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _schema_has_backend(kind: str) -> bool:
    schema_cls = getattr(REGISTRY.get(kind), "schema_cls", None)
    fields = getattr(schema_cls, "model_fields", {}) if schema_cls else {}
    return "backend" in fields


def run_all(only: list[str] | None = None) -> list[KindResult]:
    kinds = only if only else sorted(REGISTRY)
    return [run_kind(k) for k in kinds]


def render_scorecard(results: list[KindResult]) -> str:
    lines = []
    width = max((len(r.kind) for r in results), default=4)
    for r in results:
        if r.skipped:
            status = f"SKIP — {r.skipped}"
        elif r.error:
            status = f"ERROR — {r.error}"
        elif r.findings:
            keys = sorted({f"{f.poison_key}@{f.variant}/{f.backend}" for f in r.findings})
            status = f"FAIL ({len(r.findings)}) — " + ", ".join(keys[:4]) + (
                ", ..." if len(keys) > 4 else "")
        else:
            status = f"PASS ({r.variants_tested} config(s))"
        lines.append(f"  {r.kind.ljust(width)}  {status}")
    tested = [r for r in results if not r.skipped]
    failed = sum(1 for r in tested if not r.ok)
    skipped = len(results) - len(tested)
    lines.append("")
    lines.append(f"{len(tested) - failed}/{len(tested)} kind(s) clean" +
                 (f" — {failed} FAILED" if failed else "") +
                 (f" ({skipped} skipped)" if skipped else ""))
    return "\n".join(lines)
