"""EntityBehavior.cs.j2: a looping atom sequence yields a frame on every pass.

Regression for a Unity Play Mode defect: the template used to restart a
looping sequence with ``StartCoroutine(Run())`` placed BEFORE its trailing
``yield return null;``. Unity runs a coroutine synchronously up to its first
yield, so a sequence with no yielding statement (``[Reload]``, ``[Shoot]``,
``[PlaySFX]``, or ``[LightFlash]`` on an entity without a Light) re-entered
``Run()`` inside the same frame without bound. A real Unity 6000.1.4f1 run
logged repeatedly and then hit ``TLS Allocator ALLOC_TEMP_TLS`` asserts.

Every schema below is engineer-authored test data, never Pipeline LLM output.
"""
from __future__ import annotations

import pytest

from src.atoms import ATOM_REGISTRY
from src.atoms.backends.unity import unity_coroutine_body
from src.atoms.base import AtomInvocation
from src.atoms.behavior_map import _BEHAVIOR_MAP, behavior_loops
from src.unity_generator import UnityProjectGenerator
from src.unity_schema import EntitySpec, UnitySchema

_RESTART = "StartCoroutine(Run())"
_RUN_HEADER = "private IEnumerator Run()"
_LOOP_HEADER = "while (true)"


def _block_after(text: str, header: str) -> tuple[str, int]:
    """Return the body of the brace block that follows ``header`` and the
    index just past its closing brace.

    Braces inside string/char literals and ``//`` comments are ignored, so an
    atom snippet's log text cannot unbalance the match.
    """
    start = text.index(header) + len(header)
    open_at = text.index("{", start)
    assert text[start:open_at].strip() == "", f"{header!r} is not followed by a block"
    depth, i = 0, open_at
    while i < len(text):
        ch = text[i]
        if ch == "/" and text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if ch in "\"'":
            i += 1
            while text[i] != ch:
                i += 2 if text[i] == "\\" else 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i], i + 1
        i += 1
    raise AssertionError(f"unbalanced braces after {header!r}")


def _assert_loops_in_place(cs: str) -> None:
    """The generated class runs ONE coroutine whose loop ends every pass on an
    unconditional frame yield, and never restarts itself."""
    assert cs.count(_RESTART) == 1, "Run() must be started once, from Start(), never restarted"
    assert "private void Start() => StartCoroutine(Run());" in cs
    run, _ = _block_after(cs, _RUN_HEADER)
    assert _RESTART not in run
    assert run.lstrip().startswith("//") or run.lstrip().startswith(_LOOP_HEADER)
    loop, loop_end = _block_after(run, _LOOP_HEADER)
    # The last statement of the loop block is a top-level frame yield: no
    # branch of the atom body can skip it, whatever the atoms lower to.
    assert loop.rstrip().endswith("yield return null;")
    assert run[loop_end:].strip() == "", "nothing may follow the infinite loop in Run()"


def _assert_runs_once(cs: str) -> None:
    assert cs.count(_RESTART) == 1
    run, _ = _block_after(cs, _RUN_HEADER)
    assert _LOOP_HEADER not in run
    assert _RESTART not in run
    assert run.rstrip().endswith("yield return null;")


def _generate_entities(**sequences: list[AtomInvocation]) -> dict[str, str]:
    entities = [EntitySpec(name=name, kind="prop", count=1, behavior="", atoms=atoms)
                for name, atoms in sequences.items()]
    schema = UnitySchema(project_name="LoopProof", game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["MainMenu", "Main"],
                         entities=entities)
    files = UnityProjectGenerator("templates/unity").generate(schema, {})
    return {name: files[f"LoopProof/Assets/Scripts/{name}Behavior.cs"] for name in sequences}


def _render_behavior(atoms: list[AtomInvocation]) -> str:
    generator = UnityProjectGenerator("templates/unity")
    entity = EntitySpec(name="Probe", kind="prop", count=1, behavior="ProbeBehavior", atoms=atoms)
    return generator._render("EntityBehavior.cs.j2", {
        "entity": entity,
        "class_name": "ProbeBehavior",
        "atom_body": unity_coroutine_body([inv.model_dump() for inv in atoms]),
        "loop_behavior": behavior_loops(atoms),
    })


def test_a_looping_instant_only_sequence_yields_before_it_repeats():
    # Each of these lowers to statements with no unconditional yield.
    rendered = _generate_entities(
        Reloader=[AtomInvocation(atom="Reload")],
        Shooter=[AtomInvocation(atom="Shoot")],
        Chimer=[AtomInvocation(atom="PlaySFX", args={"sound": "chime"})],
        Flasher=[AtomInvocation(atom="LightFlash", args={"intensity": 2.0, "duration": 0.1})],
    )
    for name, cs in rendered.items():
        _assert_loops_in_place(cs)
        assert f"public class {name}Behavior : MonoBehaviour" in cs


def test_a_despawn_terminated_sequence_renders_no_restart():
    rendered = _generate_entities(
        Pickup=[AtomInvocation(atom="PlaySFX", args={"sound": "pickup"}),
                AtomInvocation(atom="Despawn", args={"entity": "self"})],
        Collectible=[AtomInvocation(**a) for a in _BEHAVIOR_MAP["collect"]],
    )
    for cs in rendered.values():
        _assert_runs_once(cs)


def test_a_despawn_terminated_body_is_emitted_directly_in_run():
    atoms = [AtomInvocation(atom="Wait", args={"seconds": 0.5}),
             AtomInvocation(atom="Despawn", args={"entity": "self"})]
    cs = _render_behavior(atoms)
    run, _ = _block_after(cs, _RUN_HEADER)
    body = unity_coroutine_body([inv.model_dump() for inv in atoms])
    expected = "\n".join(("        " + line) if line else "" for line in body.split("\n"))
    assert run == "\n" + expected + "\n        yield return null;\n    "


@pytest.mark.parametrize("atom", sorted(
    name for name, definition in ATOM_REGISTRY.items()
    if "unity" in definition.engines and name != "Despawn"
))
def test_every_unity_atom_alone_loops_with_a_frame_yield_per_pass(atom):
    _assert_loops_in_place(_render_behavior([AtomInvocation(atom=atom)]))


@pytest.mark.parametrize("behavior", sorted(_BEHAVIOR_MAP))
def test_every_behavior_map_sequence_renders_its_loop_shape(behavior):
    atoms = [AtomInvocation(**a) for a in _BEHAVIOR_MAP[behavior]]
    cs = _render_behavior(atoms)
    if behavior_loops(atoms):
        _assert_loops_in_place(cs)
    else:
        _assert_runs_once(cs)
