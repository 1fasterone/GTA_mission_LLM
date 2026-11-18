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
from src.unity_generator import (UnityProjectGenerator, entity_behavior_class_name,
                                 entity_behavior_entry)
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


def _render_behavior(atoms: list[AtomInvocation], game_type: str = "3D") -> str:
    generator = UnityProjectGenerator("templates/unity")
    entity = EntitySpec(name="Probe", kind="prop", count=1, behavior="ProbeBehavior", atoms=atoms)
    return generator._render("EntityBehavior.cs.j2", {
        # The generator's own entry-point context (trigger + physics).
        **entity_behavior_entry(atoms, game_type),
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



# ══════════════════════════════════════════════════════════════════════════════
# Despawn "self" and trigger-gated entry points
#
# `Despawn self` used to lower to GameObject.Find("self"), which matches
# nothing, so every canned collect/pickup/die sequence left its host standing.
# Fixing that alone would have destroyed every pickup the frame it spawned,
# because every sequence ran from Start() and "Collectible", "soldier" or
# "purchase" matched collect/die/chase by substring. The trigger now comes from
# the atoms themselves (behavior_map.behavior_trigger).
# ══════════════════════════════════════════════════════════════════════════════

from src.atom_bindings import DEATH_DORMANT_FINDING  # noqa: E402
from src.atoms.backends.unity import unity_atom_snippet  # noqa: E402
from src.atoms.behavior_map import (  # noqa: E402
    TRIGGER_DEATH,
    TRIGGER_PLAYER_CONTACT,
    TRIGGER_SPAWN,
    behavior_keyword,
    behavior_trigger,
    expand_behavior,
)

_SPAWN_ENTRY = "private void Start() => StartCoroutine(Run());"


def _canned(behavior: str) -> list[AtomInvocation]:
    return [AtomInvocation(**a) for a in _BEHAVIOR_MAP[behavior]]


def _generate(game_type: str = "3D", **sequences: list[AtomInvocation]):
    entities = [EntitySpec(name=name, kind="prop", count=1, behavior="", atoms=atoms)
                for name, atoms in sequences.items()]
    schema = UnitySchema(project_name="TriggerProof", game_type=game_type, platform="PC",
                         render_pipeline="URP", scenes=["MainMenu", "Main"],
                         entities=entities)
    generator = UnityProjectGenerator("templates/unity")
    files = generator.generate(schema, {})
    return generator, {
        name: files[f"TriggerProof/Assets/Scripts/{entity_behavior_class_name(name)}.cs"]
        for name in sequences}, files


def test_no_generated_class_looks_itself_up_by_the_name_self():
    _, _, files = _generate(**{key.title().replace("_", ""): _canned(key)
                               for key in _BEHAVIOR_MAP})
    offenders = [path for path, text in files.items()
                 if path.endswith(".cs") and 'Find("self")' in text]
    assert offenders == []


@pytest.mark.parametrize("token", ["self", "Self", "SELF", " self "])
def test_despawn_self_destroys_the_host(token):
    assert unity_atom_snippet("Despawn", {"entity": token}) == "Destroy(gameObject);"


def test_despawn_by_name_still_finds_the_named_object():
    assert unity_atom_snippet("Despawn", {"entity": "Gate"}) == (
        'var _dgo = GameObject.Find("Gate");\nif (_dgo != null) Destroy(_dgo);')


@pytest.mark.parametrize("atom, component", [("SetHealth", "HealthSystem"),
                                             ("EquipWeapon", "Inventory")])
def test_set_health_and_equip_weapon_honour_self(atom, component):
    code = unity_atom_snippet(atom, {"entity": "self", "value": 5, "weapon": "rifle"})
    first = code.splitlines()[0]
    assert first.endswith(" = gameObject;") and "Find" not in code
    assert f"GetComponent<{component}>()" in code
    # 'Player' keeps its historical reading: the host, via the ternary.
    player = unity_atom_snippet(atom, {"entity": "Player", "value": 5, "weapon": "rifle"})
    assert '"Player" == "Player" ? gameObject : GameObject.Find("Player")' in player


@pytest.mark.parametrize("behavior", ["collect", "pickup"])
def test_collect_and_pickup_run_on_player_contact_never_at_start(behavior):
    _, rendered, _ = _generate(Probe=_canned(behavior))
    cs = rendered["Probe"]
    assert _SPAWN_ENTRY not in cs
    start, _ = _block_after(cs, "private void Start()")
    assert "StartCoroutine" not in start
    assert "foreach (var c in GetComponents<Collider>()) c.isTrigger = true;" in start
    contact, _ = _block_after(cs, "private void OnTriggerEnter(Collider other)")
    assert "if (_ran || !IsPlayer(other.gameObject, body)) return;" in contact
    assert contact.rstrip().endswith("StartCoroutine(Run());")
    assert 'hit.CompareTag("Player") || (body != null && body.CompareTag("Player"))' in cs
    assert "Collider2D" not in cs and "OnTriggerEnter2D" not in cs
    assert "Destroy(gameObject);" in cs
    _assert_runs_once(cs)


@pytest.mark.parametrize("game_type", ["2D", "2d_topdown"])
def test_a_2d_contact_listens_on_2d_physics_only(game_type):
    _, rendered, _ = _generate(game_type=game_type, Probe=_canned("collect"))
    cs = rendered["Probe"]
    assert "private void OnTriggerEnter2D(Collider2D other)" in cs
    assert "foreach (var c in GetComponents<Collider2D>()) c.isTrigger = true;" in cs
    assert "OnTriggerEnter(Collider other)" not in cs
    assert "GetComponents<Collider>()" not in cs


def test_die_waits_for_on_death_and_has_no_start():
    _, rendered, _ = _generate(Probe=_canned("die"))
    cs = rendered["Probe"]
    assert "private void Start()" not in cs
    on_death, _ = _block_after(cs, "public void OnDeath()")
    assert on_death.split() == "if (_ran) return; _ran = true; StartCoroutine(Run());".split()
    _assert_runs_once(cs)


@pytest.mark.parametrize("behavior", sorted(set(_BEHAVIOR_MAP) - {"collect", "pickup", "die"}))
def test_spawn_sequences_keep_their_start_entry(behavior):
    cs = _render_behavior(_canned(behavior))
    assert cs.splitlines()[:5] == [
        "// Auto-generated — DO NOT EDIT",
        "// Atom-driven behavior for entity 'Probe'.",
        '// Attached at runtime by EntitySpawner via AddComponent("ProbeBehavior").',
        "using System.Collections;",
        "using UnityEngine;",
    ]
    assert cs.count(_SPAWN_ENTRY) == 1
    for marker in ("OnTriggerEnter", "OnDeath", "_ran", "isTrigger", "IsPlayer"):
        assert marker not in cs


def test_trigger_is_read_from_the_exact_canned_sequence():
    assert behavior_trigger(_canned("collect")) == TRIGGER_PLAYER_CONTACT
    assert behavior_trigger(_canned("pickup")) == TRIGGER_PLAYER_CONTACT
    assert behavior_trigger(_canned("die")) == TRIGGER_DEATH
    assert behavior_trigger(_canned("patrol")) == TRIGGER_SPAWN
    assert behavior_trigger([]) == TRIGGER_SPAWN
    # A saved schema's JSON dicts carry the same trigger, and a step label is
    # a comment, not behaviour.
    assert behavior_trigger([dict(a) for a in _BEHAVIOR_MAP["collect"]]) == TRIGGER_PLAYER_CONTACT
    relabelled = [AtomInvocation(atom=a.atom, args=a.args, label="x") for a in _canned("die")]
    assert behavior_trigger(relabelled) == TRIGGER_DEATH


@pytest.mark.parametrize("change", ["arg", "extra_step", "dropped_step"])
def test_a_modified_canned_sequence_starts_at_spawn(change):
    atoms = _canned("collect")
    if change == "arg":
        atoms[0] = AtomInvocation(atom="PlaySFX", args={"sound": "coin", "volume": 0.8})
    elif change == "extra_step":
        atoms.insert(0, AtomInvocation(atom="Wait", args={"seconds": 3.0}))
    else:
        atoms = atoms[1:]
    assert behavior_trigger(atoms) == TRIGGER_SPAWN


@pytest.mark.parametrize("text, key", [
    ("patrol", "patrol"),
    ("patrolling the gate", "patrol"),
    ("Collectible", "collect"),
    ("PlayerCollectible", "collect"),
    ("collected on touch", "collect"),
    ("pick up the coin", "pickup"),
    ("dies when shot", "die"),
    ("chasing the player", "chase"),
    ("guard the vault", "guard"),
    ("intro_fade", "intro_fade"),
    ("screen transition", "screen_transition"),
    ("HUDPanel idles", "idle"),
    # Substring false positives the whole-token rule ends.
    ("soldier", None),
    ("diesel generator", None),
    ("purchase counter", None),
    ("guardrail", None),
    ("recollection", None),
    ("fade the intro", None),
    ("", None),
])
def test_expand_behavior_matches_whole_words_only(text, key):
    assert behavior_keyword(text) == key
    expected = [] if key is None else _canned(key)
    assert expand_behavior(text) == expected


def test_atom_routes_name_each_trigger_and_the_dormant_death():
    generator, _, _ = _generate(Coin=_canned("collect"), Zombie=_canned("die"),
                                Guard=_canned("guard"))
    record = generator.last_atom_bindings
    coin = record.route("entities[Coin].atoms")
    zombie = record.route("entities[Zombie].atoms")
    guard = record.route("entities[Guard].atoms")
    assert (coin.trigger, coin.phase, coin.finding) == ("player_contact", "start_coroutine_once", None)
    assert (guard.trigger, guard.phase, guard.finding) == ("entity_spawn", "start_coroutine_loop", None)
    assert zombie.trigger == "death" and zombie.finding == DEATH_DORMANT_FINDING
    # EntitySpawner still adds the class: the death route is attached, and dormant.
    assert zombie.host == "EntitySpawner.AddComponent" and zombie.scenes == ["Main"]


def test_the_editor_acceptance_runs_the_entity_trigger_cases():
    from pathlib import Path

    from tools.generate_firing_provider_acceptance import (CONTACT_ENTITY_ATOMS,
                                                           RELOAD_ENTITY_ATOMS)

    root = Path(__file__).resolve().parents[1]
    assert behavior_trigger(CONTACT_ENTITY_ATOMS) == TRIGGER_PLAYER_CONTACT
    assert behavior_trigger(RELOAD_ENTITY_ATOMS) == TRIGGER_SPAWN
    assert RELOAD_ENTITY_ATOMS[-1].args == {"entity": "self"}
    runner = (root / "tools/run_firing_provider_acceptance.ps1").read_text(encoding="utf-8")
    assert '"EntityTriggerPlayModeTests.cs")' in runner
    assert "ReloadProviderPlayModeTests;EntityTriggerPlayModeTests" in runner
    markers = ("ENTITY_TRIGGER_UNTOUCHED_PASS", "ENTITY_TRIGGER_RIGIDBODY_CONTACT_PASS",
               "ENTITY_TRIGGER_CHARACTER_CONTACT_PASS", "ENTITY_TRIGGER_UNTAGGED_PASS")
    for marker in markers:
        assert f'"{marker}"' in runner
    assert "$expectedPassed = 6 + $reloadShooters.Count + 1 + 4" in runner
    test = (root / "tests/unity_firing_provider/EntityTriggerPlayModeTests.cs"
            ).read_text(encoding="utf-8")
    assert "coin.AddComponent<CoinBehavior>();" in test
    # One [UnityTest] per marker, so the runner's expected count stays honest.
    assert test.count("[UnityTest]") == len(markers)
    assert all(marker in test for marker in markers)
    reload_test = (root / "tests/unity_firing_provider/ReloadProviderPlayModeTests.cs"
                   ).read_text(encoding="utf-8")
    assert 'Assert.IsTrue(host == null, "ArmoryBehavior did not despawn its own host.");' in reload_test


@pytest.mark.parametrize("game_type, behavior", [
    ("3D", "collect"), ("2D", "collect"), ("3D", "die"), ("3D", "patrol"),
])
def test_each_entry_shape_compiles_against_real_unity(game_type, behavior):
    from tests.test_reload_provider import _compile

    cs = _render_behavior(_canned(behavior), game_type)
    check = _compile({"Assets/Scripts/ProbeBehavior.cs": cs})
    assert check.accepted, (game_type, behavior, check.errors)
