"""S0 engineer-authored mutation fixtures; no live LLM or runtime claims."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from src.atoms import ALL_ATOMS, get_atom
from src.atoms.backends import unity
from src.atoms.base import AtomDef
from src.atoms.support_audit import (
    BASELINE, INVENTORY, ROOT, build_inventory, duplicate_dict_keys,
    identity_errors, policy_errors, validate_inventory,
)


@pytest.fixture(scope="module")
def inventory():
    return build_inventory()


def _baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_current_inventory_is_derived_and_debt_is_explicit(inventory):
    assert validate_inventory(inventory, _baseline()) == []
    assert inventory == json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert policy_errors() == []
    assert inventory["runtime_acceptance"] == "unverified"


def test_every_definition_has_both_backend_classifications(inventory):
    public = [row for row in inventory["atoms"] if row["classification"] != "internal"]
    assert {(row["backend"], row["atom"]) for row in public} == {
        (backend, atom.name) for atom in ALL_ATOMS for backend in ("unity", "gta5")}
    assert len(public) == len(ALL_ATOMS) * 2


def test_handler_removal_breaks_gate_without_editing_a_second_api_table(monkeypatch):
    monkeypatch.delitem(unity._SNIPPETS, "MoveTo")
    errors = validate_inventory(build_inventory(), _baseline())
    assert "unreviewed missing_lowerers: unity:MoveTo" in errors


@pytest.mark.parametrize("replacement", [lambda _: "// TODO: not implemented", lambda _: "/* comment only */"])
def test_comment_handler_cannot_claim_executable(monkeypatch, replacement):
    monkeypatch.setitem(unity._SNIPPETS, "MoveTo", replacement)
    assert any("MoveTo: emission smoke failed" in error
               for error in validate_inventory(build_inventory(), _baseline()))


def test_nondeterministic_handler_fails(monkeypatch):
    calls = iter(["Move(1);", "Move(2);"])
    monkeypatch.setitem(unity._SNIPPETS, "MoveTo", lambda _: next(calls))
    assert any("MoveTo: emission smoke failed" in error
               for error in validate_inventory(build_inventory(), _baseline()))


def test_new_unregistered_handler_cannot_hide(monkeypatch):
    monkeypatch.setitem(unity._SNIPPETS, "UnregisteredOperation", lambda _: "Run();")
    assert "unreviewed internal_handlers: unity:UnregisteredOperation" in validate_inventory(
        build_inventory(), _baseline())


def test_closed_gap_must_ratchet_the_debt(monkeypatch):
    # Synthetic mutation, not a Shoot implementation or accepted new vocabulary.
    monkeypatch.setitem(unity._SNIPPETS, "Shoot", lambda _: "AuditOnly();")
    assert "stale missing_lowerers baseline (ratchet/update required): unity:Shoot" in validate_inventory(
        build_inventory(), _baseline())


def test_backend_scope_leak_fails(monkeypatch):
    from src.atoms.backends import gta5
    monkeypatch.setitem(gta5._SNIPPETS, "Jump", lambda _: "Jump();")
    assert "gta5:Jump: handler exists for unsupported backend" in validate_inventory(
        build_inventory(), _baseline())


def test_duplicate_canonical_and_cross_atom_aliases_fail():
    one = AtomDef(name="One", category="logic", description="fixture", aliases=["shared"])
    two = AtomDef(name="Two", category="logic", description="fixture", aliases=["SHARED"])
    assert any("belongs to both" in error for error in identity_errors([one, two]))
    assert "duplicate canonical atom One" in identity_errors([one, one])


def test_new_alias_collision_fails_even_with_legacy_collisions(monkeypatch):
    from src.atoms import support_audit
    changed = [atom.model_copy(deep=True) for atom in ALL_ATOMS]
    changed[0].aliases.append(changed[1].name)
    monkeypatch.setattr(support_audit, "ALL_ATOMS", changed)
    assert any("unreviewed identity conflict" in error
               for error in validate_inventory(build_inventory(), _baseline()))


def test_literal_duplicate_registration_detected_before_dict_overwrite():
    source = '_SNIPPETS = {"Move": first, "Move": second}\n'
    assert duplicate_dict_keys(source) == ["duplicate dictionary key 'Move' at line 1"]


def test_every_token_has_exactly_one_owner(inventory):
    """R1 closed the four legacy collisions with one owner per token."""
    assert inventory["identity_conflicts"] == []
    assert get_atom("fire").name == "Shoot"
    assert get_atom("pause").name == "Pause"
    assert get_atom("PAUSE").name == "Pause"
    assert get_atom("wait").name == "Wait"
    assert get_atom("roll_evade").name == "Dodge"
    assert get_atom("dash").name == "Dash"
    assert get_atom("thrust").name == "Throttle"
    assert get_atom("push").name == "ApplyForce"
    assert get_atom("kick").name == "SecondaryAttack"
    assert get_atom("knockback").name == "ApplyImpulse"


def test_unverified_gameplay_owner_claim_is_rejected(inventory):
    changed = copy.deepcopy(inventory)
    changed["micros"][0]["ownership"] = "atom_owned"
    assert any("lacks migrated runtime evidence" in error
               for error in validate_inventory(changed, _baseline()))


def test_controller_variants_and_pointer_remain_template_owned(inventory):
    controller = next(micro for micro in inventory["micros"]
                      if micro["backend"] == "unity" and micro["kind"] == "controller")
    from src.micro.controller import ControllerPipeline
    assert controller["variants"] == sorted(ControllerPipeline.variants)
    assert "pointer" in controller["variants"]
    assert controller["ownership"] == "template_owned"
    # R2 retired every Unity companion (camera's was already suppressed).
    unity = [micro for micro in inventory["micros"] if micro["backend"] == "unity"]
    assert {micro["companion_emission"] for micro in unity} == {"retired (R2)"}
    assert controller["companion_emission"] == "retired (R2)"


def test_policy_disagreement_fails(tmp_path):
    for name in ["AGENTS.md", "CLAUDE.md"]:
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    path = tmp_path / "CLAUDE.md"
    path.write_text(path.read_text().replace('Current controller', 'Migrated controller'))
    assert "AGENTS.md and CLAUDE.md atom support policies differ" in policy_errors(tmp_path)


def test_cli_replay_is_byte_identical_and_read_only():
    before = INVENTORY.read_bytes()
    command = [sys.executable, "-m", "src.atoms.support_audit", "--check"]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
    assert first.stdout == second.stdout
    assert INVENTORY.read_bytes() == before


def test_unsupported_runtime_claim_is_rejected(inventory):
    changed = copy.deepcopy(inventory)
    changed["atoms"][0]["runtime_acceptance"] = "passed"
    assert any("unsupported runtime coverage claim" in error
               for error in validate_inventory(changed, _baseline()))


def test_missing_call_path_evidence_is_rejected(inventory):
    changed = copy.deepcopy(inventory)
    changed["call_paths"]["unity"][0]["template"] = "missing_template.cs.j2"
    assert "missing call-path evidence: missing_template.cs.j2" in validate_inventory(changed, _baseline())


def test_write_cannot_approve_new_missing_implementation(monkeypatch, tmp_path):
    from src.atoms import support_audit
    output = tmp_path / "inventory.json"
    output.write_text("keep evidence")
    monkeypatch.setattr(support_audit, "INVENTORY", output)
    monkeypatch.delitem(unity._SNIPPETS, "MoveTo")
    assert support_audit.main(["--write"]) == 1
    assert output.read_text() == "keep evidence"


def test_check_rejects_stale_derived_inventory(monkeypatch, tmp_path):
    from src.atoms import support_audit
    output = tmp_path / "inventory.json"
    output.write_text("{}")
    monkeypatch.setattr(support_audit, "INVENTORY", output)
    assert support_audit.main(["--check"]) == 1


def test_capability_unknowns_are_visible_and_new_unknowns_fail(inventory):
    unknown = {row["atom"] for row in inventory["references"] if row["resolution"] == "unregistered"}
    assert unknown == set(_baseline()["unregistered_references"]) == set()
    assert "InitDialogSystem" not in unknown
    changed = copy.deepcopy(inventory)
    changed["references"].append({"atom": "InventedAction", "resolution": "unregistered", "sources": []})
    assert "unreviewed unregistered atom reference: InventedAction" in validate_inventory(changed, _baseline())


# ── R1: S0 vocabulary decisions ──────────────────────────────────────────────

def test_no_s0_followup_debt_remains():
    baseline = _baseline()
    open_entries = [
        key for section in ("identity_conflicts", "internal_handlers",
                            "unregistered_references", "missing_lowerers")
        for key, entry in baseline[section].items()
        if str(entry.get("next_slice", "")).startswith("S0-followup")
    ]
    assert open_entries == []


def test_a_vocabulary_collision_fails_at_import_not_first_registered_wins():
    from src.atoms import _build_alias_index

    one = AtomDef(name="One", category="logic", description="fixture", aliases=["shared"])
    two = AtomDef(name="Two", category="logic", description="fixture", aliases=["SHARED"])
    with pytest.raises(RuntimeError, match="'SHARED' is claimed by both One and Two"):
        _build_alias_index([one, two])
    named = AtomDef(name="Shared", category="logic", description="fixture")
    with pytest.raises(RuntimeError, match="claimed by both One and Shared"):
        _build_alias_index([one, named])


def test_dialog_helpers_are_registered_unity_atoms(inventory):
    from src.atoms.dialog import DIALOG_ATOMS

    names = {atom.name for atom in DIALOG_ATOMS}
    assert names == {"SayLine", "PlayDialogAudio", "Gesture", "LookAt", "EndDialog"}
    rows = {row["atom"]: row for row in inventory["atoms"] if row["backend"] == "unity"}
    for name in names:
        assert get_atom(name).engines == ["unity"]
        assert get_atom(name).category == "dialog"
        assert rows[name]["classification"] == "executable"
    # `look_at` stays the camera's; the NPC head-look keeps its own words.
    assert get_atom("look_at").name == "CameraLookAt"
    assert get_atom("head_look").name == "LookAt"
    assert not [row for row in inventory["atoms"] if row["classification"] == "internal"]


def test_dialog_registration_leaves_the_step3_vocabulary_unchanged():
    from src.unity_prompts import _SEQUENCE_ATOM_CATEGORIES, atom_vocabulary

    assert "dialog" not in _SEQUENCE_ATOM_CATEGORIES
    vocabulary = atom_vocabulary()
    assert "SayLine(" not in vocabulary
    assert "EndDialog(" not in vocabulary


def test_escort_seam_is_a_registered_wiring_atom():
    atom = get_atom("WireTriggerToEscortObjective")
    assert atom is not None
    assert atom.category == "wiring"
    assert atom.engines == ["unity"]
    assert {p.name for p in atom.params if p.required} == {"objective_id", "entity_go_name"}
