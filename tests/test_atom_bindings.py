"""R2 (S4): typed atom-route bindings, per-build reference checks, replay.

Every schema below is engineer-authored test data, never Pipeline LLM output.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.atom_bindings import (
    BINDINGS_KEY,
    ENTITY_SPAWNER_HOST,
    GAME_SEQUENCER_HOST,
    AtomBindings,
    bind_atom,
    derive_atom_bindings,
    saved_atom_bindings,
    stamp_atom_bindings,
)
from src.atoms import ATOM_REGISTRY
from src.atoms.base import AtomDef, AtomInvocation
from src.unity_eval import replay
from src.unity_generator import UnityProjectGenerator, _make_guid
from src.unity_schema import EntitySpec, SceneSpec, UnitySchema, UnityScript

FIXTURE = Path("tests/fixtures/dropzone_arcade_llm.schema.json")


def _schema(**overrides) -> UnitySchema:
    fields = dict(project_name="BindProof", game_type="3D", platform="PC",
                  render_pipeline="URP", scenes=["MainMenu", "Main"])
    fields.update(overrides)
    return UnitySchema(**fields)


def _generate(schema: UnitySchema) -> tuple[UnityProjectGenerator, dict[str, str]]:
    generator = UnityProjectGenerator("templates/unity")
    return generator, generator.generate(schema, {})


def _wait(seconds: float = 0.5) -> AtomInvocation:
    return AtomInvocation(atom="Wait", args={"seconds": seconds})


# ── per-build validation of dynamic atom references ──────────────────────────

def test_each_dynamic_reference_names_the_lowerer_it_reaches(monkeypatch):
    lowered = bind_atom(0, _wait())
    assert lowered.status == "lowered"
    assert lowered.provider == "src/atoms/backends/unity.py:_wait"

    alias = bind_atom(1, AtomInvocation(atom="delay"))
    assert (alias.status, alias.canonical, alias.provider) == ("alias", "Wait", None)

    assert bind_atom(2, AtomInvocation(atom="Shoot")).status == "descriptive"
    assert bind_atom(3, AtomInvocation(atom="InventedThing")).status == "unregistered"

    monkeypatch.setitem(ATOM_REGISTRY, "GtaOnlyFixture", AtomDef(
        name="GtaOnlyFixture", category="logic", description="fixture", engines=["gta5"]))
    assert bind_atom(4, AtomInvocation(atom="GtaOnlyFixture")).status == "unsupported"


def test_unexecutable_references_are_reported_not_silently_lowered():
    schema = _schema(atoms=[_wait(), AtomInvocation(atom="delay"),
                            AtomInvocation(atom="InventedThing")])
    generator, files = _generate(schema)
    warnings = generator.last_atom_binding_report.warnings
    assert any("atoms[1] 'delay' is an alias" in w and "'Wait'" in w for w in warnings)
    assert any("atoms[2] 'InventedThing' is not a registered atom" in w for w in warnings)
    assert not any("atoms[0]" in w for w in warnings)
    sequencer = files["BindProof/Assets/Scripts/GameSequencer.cs"]
    assert "// TODO: atom 'InventedThing'" in sequencer


# ── GameSequencer: attached, not merely emitted ──────────────────────────────

def test_game_sequencer_is_hosted_in_every_gameplay_scene_and_no_menu():
    schema = _schema(scenes=["MainMenu", "Main", "Level2"], atoms=[_wait()])
    generator, files = _generate(schema)
    guid = _make_guid("BindProof/Scripts/GameSequencer")
    assert "BindProof/Assets/Scripts/GameSequencer.cs" in files
    for scene in ("Main", "Level2"):
        text = files[f"BindProof/Assets/Scenes/{scene}.unity"]
        assert f"m_Name: {GAME_SEQUENCER_HOST}" in text
        assert f"m_Script: {{fileID: 11500000, guid: {guid}, type: 3}}" in text
    menu = files["BindProof/Assets/Scenes/MainMenu.unity"]
    assert GAME_SEQUENCER_HOST not in menu and guid not in menu

    route = generator.last_atom_bindings.route("atoms")
    assert route is not None and route.attached
    assert (route.host_class, route.host) == ("GameSequencer", GAME_SEQUENCER_HOST)
    assert (route.phase, route.trigger) == ("start_coroutine_once", "scene_load")
    assert route.scenes == ["Main", "Level2"]


def test_no_atoms_means_no_sequencer_and_no_host():
    schema = _schema()
    generator, files = _generate(schema)
    assert "BindProof/Assets/Scripts/GameSequencer.cs" not in files
    assert GAME_SEQUENCER_HOST not in files["BindProof/Assets/Scenes/Main.unity"]
    assert generator.last_atom_bindings.routes == []


def test_a_menu_only_build_emits_no_unhosted_sequencer():
    schema = _schema(scenes=["MainMenu"], atoms=[_wait()])
    generator, files = _generate(schema)
    assert "BindProof/Assets/Scripts/GameSequencer.cs" not in files
    route = generator.last_atom_bindings.route("atoms")
    assert route is not None and not route.attached
    assert "every scene is a menu" in (route.finding or "")
    assert any("atoms does not run" in w for w in generator.last_atom_binding_report.warnings)


# ── entity and custom-script routes ──────────────────────────────────────────

def test_entity_atoms_with_a_blank_behavior_reach_their_generated_class():
    crate = EntitySpec(name="Crate", kind="prop", count=2, placement="scatter",
                       behavior="", atoms=[_wait()])
    schema = _schema(entities=[crate])
    generator, files = _generate(schema)
    assert schema.entities[0].behavior == "CrateBehavior"
    assert "BindProof/Assets/Scripts/CrateBehavior.cs" in files
    route = generator.last_atom_bindings.route("entities[Crate].atoms")
    assert route is not None and route.attached
    assert (route.host_class, route.host) == ("CrateBehavior", ENTITY_SPAWNER_HOST)
    assert (route.phase, route.trigger) == ("start_coroutine_loop", "entity_spawn")
    assert route.scenes == ["Main"]


def test_an_entity_route_no_scene_hosts_is_named():
    crate = EntitySpec(name="Crate", kind="prop", count=1, atoms=[_wait()])
    schema = _schema(entities=[crate], scene_specs=[SceneSpec(name="Main", entities=[])])
    generator, _ = _generate(schema)
    route = generator.last_atom_bindings.route("entities[Crate].atoms")
    assert route is not None and not route.attached
    assert route.finding == "no gameplay scene hosts this entity group"


def test_custom_script_atoms_run_on_their_attach_to_host():
    script = UnityScript(name="Pulse", type="custom", description="pulse",
                         attach_to="systems", atoms=[_wait()])
    schema = _schema(scripts=[script])
    generator, _ = _generate(schema)
    route = generator.last_atom_bindings.route("scripts[Pulse].atoms")
    assert route is not None and route.attached
    assert (route.host_class, route.host) == ("Pulse", "_CustomScripts")


# ── persistence and versioned replay ─────────────────────────────────────────

def _replay_schema() -> UnitySchema:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["atoms"] = [{"atom": "Wait", "args": {"seconds": 0.25}},
                     {"atom": "delay", "args": {}}]
    for entity in data["entities"]:
        if entity["name"] == "Collectible":
            entity["atoms"] = [{"atom": "Wait", "args": {"seconds": 1.0}}]
    return UnitySchema.model_validate(data)


def test_saved_schema_replay_reproduces_bindings_twice():
    first_schema, _, first_log = replay(_replay_schema())
    saved = json.loads(first_schema.model_dump_json())
    record = AtomBindings.model_validate(saved["meta"][BINDINGS_KEY])
    assert {route.source for route in record.routes} >= {
        "atoms", "entities[Collectible].atoms", "scripts[CameraManager].atoms"}

    for _ in range(2):
        replayed, _, log = replay(UnitySchema.model_validate(saved))
        assert replayed.meta[BINDINGS_KEY] == saved["meta"][BINDINGS_KEY]
        assert any("saved record replayed identically" in line for line in log)
        assert not any("replay drift" in line for line in log)
        saved = json.loads(replayed.model_dump_json())
    assert not any("saved record replayed" in line for line in first_log)


def test_an_unreadable_record_version_is_announced_and_replaced():
    schema = _schema(atoms=[_wait()])
    schema.meta[BINDINGS_KEY] = {"version": 2, "backend": "unity", "routes": []}
    report = stamp_atom_bindings(schema, derive_atom_bindings(schema))
    assert any("not replayable by this compiler (version 2" in w for w in report.warnings)
    assert saved_atom_bindings(schema) == derive_atom_bindings(schema)


def test_replay_drift_names_the_changed_routes():
    schema = _schema(atoms=[_wait()])
    stamp_atom_bindings(schema, derive_atom_bindings(schema))
    schema.atoms.append(_wait(2.0))
    report = stamp_atom_bindings(schema, derive_atom_bindings(schema))
    assert any("replay drift" in w and "for atoms;" in w for w in report.warnings)
    assert saved_atom_bindings(schema) == derive_atom_bindings(schema)


def test_derivation_is_deterministic():
    crate = EntitySpec(name="Crate", kind="prop", count=1, atoms=[_wait()])
    schema = _schema(atoms=[_wait()], entities=[crate])
    assert derive_atom_bindings(schema) == derive_atom_bindings(schema)
    assert (derive_atom_bindings(schema).model_dump_json()
            == derive_atom_bindings(schema).model_dump_json())
