"""S3 structural mechanic ownership audit tests; no runtime claims."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections import Counter

import pytest

from src.atoms.ownership_audit import (
    AMBIGUOUS_OWNER,
    CONNECTED_AND_OWNED,
    CONNECTED_BUT_NOT_ATTACHED,
    MISSING_PROVIDER,
    REPORT,
    RETIRED,
    ROOT,
    OwnershipEvidenceError,
    _has_hosted_script_binding,
    _source_ref,
    _unity_companion_emitters,
    build_report,
    classify_connection,
    validate_report,
)


@pytest.fixture(scope="module")
def report():
    return build_report()


def _representative(report, descriptor: str):
    return next(
        row for row in report["representative_connections"]
        if row["descriptor"] == descriptor
    )


def test_current_report_is_derived_and_structural_only(report):
    assert validate_report(report) == []
    assert report == json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["runtime_acceptance"] == "unverified"
    assert report["evidence_level"] == "structural_source_only"


def test_representatives_cross_all_required_ownership_modes(report):
    modes = {row["mode"] for row in report["representative_connections"]}
    assert {
        "schema_level_atom",
        "entity_attachment",
        "unity_wiring_seam",
        "standalone_companion",
        "template_owned_mechanic",
    } <= modes


def test_emission_alone_does_not_establish_attachment(report):
    assert classify_connection(
        provider="src/provider.py:1",
        owner_candidates=["GeneratedCompanion"],
        lifecycle_seam=None,
    ) == CONNECTED_BUT_NOT_ATTACHED
    # R2 attached the sequencer; losing that seam must fail, not downgrade quietly.
    changed = copy.deepcopy(report)
    sequencer = _representative(changed, "unity:UnitySchema.atoms:Wait")
    sequencer["classification"] = CONNECTED_BUT_NOT_ATTACHED
    sequencer["lifecycle_seam"] = None
    sequencer["missing_seam"] = "scene or prefab component attachment"
    changed["summary"] = dict(sorted(Counter(
        row["classification"] for row in changed["representative_connections"]).items()))
    assert any("emitted but unattached" in error for error in validate_report(changed))


def test_attachment_requires_a_hosted_specific_guid_binding():
    mentions_only = """{# GameSequencer #}
# ControllerDefaultBehavior and AnotherDefaultBehavior
m_Script: {fileID: 11500000, guid: {{ unrelated_guid }}, type: 3}
"""
    unhosted = """--- !u!114 &100
MonoBehaviour:
  m_GameObject: {fileID: 0}
  m_Script: {fileID: 11500000, guid: {{ game_sequencer_guid }}, type: 3}
"""
    hosted = """--- !u!114 &100
MonoBehaviour:
  m_GameObject: {fileID: 200}
  m_Script: {fileID: 11500000, guid: {{ game_sequencer_guid }}, type: 3}
"""
    assert not _has_hosted_script_binding(mentions_only, "game_sequencer_guid")
    assert not _has_hosted_script_binding(unhosted, "game_sequencer_guid")
    assert _has_hosted_script_binding(hosted, "game_sequencer_guid")


def test_entity_and_wiring_routes_have_real_hosts(report):
    entity = _representative(report, "unity:behavior_map:patrol:MoveTo")
    wiring = _representative(report, "unity:wiring:WireInputToController")
    assert entity["classification"] == CONNECTED_AND_OWNED
    assert entity["generated_host"] == "spawned entity GameObject"
    assert wiring["classification"] == CONNECTED_AND_OWNED
    assert wiring["generated_host"] == "_Systems"


def test_shoot_is_owned_by_the_one_firing_provider(report):
    """R3 (S5) closed Shoot's missing provider; it shares the input's gate."""
    from src.atoms.backends import unity

    shoot = _representative(report, "unity:AtomDef:Shoot")
    assert shoot["classification"] == CONNECTED_AND_OWNED
    assert shoot["mode"] == "shared_firing_provider"
    assert shoot["provider"] == _source_ref(unity._SNIPPETS["Shoot"])
    assert shoot["owner_candidates"] == ["ControllerPipeline"]
    assert shoot["generated_host"] == "Player"
    assert "IFiringProvider.TryFire" in shoot["lifecycle_seam"]
    assert shoot["next_slice"] is None
    assert shoot["template_owner_preserved"] == "ControllerPipeline"


def test_ambiguous_owner_names_candidates_and_missing_seam():
    assert classify_connection(
        provider="src/provider.py:1",
        owner_candidates=["FirstOwner", "SecondOwner"],
        lifecycle_seam=None,
    ) == AMBIGUOUS_OWNER


def test_unknown_owner_route_fails_loudly():
    with pytest.raises(OwnershipEvidenceError, match=r"candidates=\[\].*missing_seam"):
        classify_connection(
            provider="src/provider.py:1",
            owner_candidates=[],
            lifecycle_seam=None,
        )


def test_ambiguous_report_must_name_every_candidate_and_missing_seam(report):
    changed = copy.deepcopy(report)
    row = _representative(changed, "unity:MICROGEN_ATOM_MAP:controller:Move")
    row["classification"] = AMBIGUOUS_OWNER
    row["owner_candidates"] = ["FirstOwner", "SecondOwner"]
    row["diagnostic"] = "ambiguous owner candidates: FirstOwner"
    row["missing_seam"] = None
    errors = validate_report(changed)
    assert any("ambiguity diagnostic omits SecondOwner" in error for error in errors)
    assert any("ambiguous route must name the missing seam" in error for error in errors)


def test_validator_rejects_emission_upgraded_to_attachment(report):
    changed = copy.deepcopy(report)
    companion = _representative(changed, "unity:MICROGEN_ATOM_MAP:controller:Move")
    companion["classification"] = CONNECTED_AND_OWNED
    assert any(
        "connected ownership lacks host or lifecycle seam" in error
        for error in validate_report(changed)
    )


def test_template_owners_remain_unmigrated(report):
    owners = {row["mechanic"]: row for row in report["protected_template_owners"]}
    assert set(owners) == {"controller", "board", "rules", "projectile"}
    assert all(row["ownership"] == "template_owned" for row in owners.values())
    assert all(row["migrated"] is False for row in owners.values())


def test_validator_rejects_template_owner_replacement(report):
    changed = copy.deepcopy(report)
    changed["protected_template_owners"][0]["ownership"] = "atom_owned"
    assert any("S3 cannot replace template ownership" in error
               for error in validate_report(changed))


def test_cli_replay_is_byte_identical_and_read_only():
    before = REPORT.read_bytes()
    command = [sys.executable, "-m", "src.atoms.ownership_audit", "--check"]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
    assert first.stdout == second.stdout
    assert REPORT.read_bytes() == before


# ── R2 (S4): every representative route attached or retired ──────────────────

def test_r2_leaves_no_route_emitted_but_unattached(report):
    assert CONNECTED_BUT_NOT_ATTACHED not in report["summary"]
    sequencer = _representative(report, "unity:UnitySchema.atoms:Wait")
    assert sequencer["classification"] == CONNECTED_AND_OWNED
    assert sequencer["generated_host"] == "_GameSequencer"
    assert sequencer["lifecycle_seam"].startswith("templates/unity/scene.unity.j2:")
    assert any(ref.startswith("src/atom_bindings.py:") for ref in sequencer["provenance"])


def test_unity_companions_are_retired_with_source_evidence(report):
    from src.micro.unity_base import UnityMicroPipeline

    companion = _representative(report, "unity:MICROGEN_ATOM_MAP:controller:Move")
    assert companion["classification"] == RETIRED
    assert companion["provider"] is None and companion["owner_candidates"] == []
    assert companion["retired_by"] == _source_ref(UnityMicroPipeline._after_write)
    assert companion["diagnostic"].startswith("retired by ")
    assert _unity_companion_emitters() == []


def test_retired_unity_hook_writes_nothing(tmp_path):
    from src.micro.unity_base import UnityMicroPipeline

    UnityMicroPipeline._after_write(None, None, "default", tmp_path, "Proof")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def _emitting_hook(self, schema, variant, out_dir, name):
    (out_dir / "ControllerDefaultBehavior.cs").write_text("// companion", encoding="utf-8")


def test_a_hook_that_emits_again_reopens_the_route_and_fails(monkeypatch):
    from src.micro.unity_base import UnityMicroPipeline

    monkeypatch.setattr(UnityMicroPipeline, "_after_write", _emitting_hook)
    reopened = build_report()
    companion = _representative(reopened, "unity:MICROGEN_ATOM_MAP:controller:Move")
    assert companion["classification"] == CONNECTED_BUT_NOT_ATTACHED
    assert companion["provider"] == _source_ref(_emitting_hook)
    assert any("emitted but unattached" in error for error in validate_report(reopened))


def test_a_retired_route_cannot_keep_a_provider(report):
    with pytest.raises(OwnershipEvidenceError, match="retired by"):
        classify_connection(provider="src/provider.py:1", owner_candidates=[],
                            lifecycle_seam=None, retired_by="src/retirer.py:1")
    changed = copy.deepcopy(report)
    companion = _representative(changed, "unity:MICROGEN_ATOM_MAP:controller:Move")
    companion["retired_by"] = "src/no_such_retirer.py:1"
    assert any("retired route needs retired_by source evidence" in error
               for error in validate_report(changed))


# ── R3 (S5): Shoot and fire input share one firing provider ──────────────────

def test_r3_leaves_no_route_without_a_provider(report):
    assert MISSING_PROVIDER not in report["summary"]
    changed = copy.deepcopy(report)
    shoot = _representative(changed, "unity:AtomDef:Shoot")
    shoot.update(classification=MISSING_PROVIDER, provider=None, owner_candidates=[],
                 generated_host=None, lifecycle_seam=None, next_slice="S5")
    changed["summary"] = dict(sorted(Counter(
        row["classification"] for row in changed["representative_connections"]).items()))
    assert any("no provider; supply a lowerer and its runtime owner (R3)" in error
               for error in validate_report(changed))


def test_a_shoot_lowerer_that_bypasses_the_provider_loses_its_seam(monkeypatch):
    from src.atoms.backends import unity

    monkeypatch.setitem(unity._SNIPPETS, "Shoot", lambda _args: "GetComponent<Gun>().Fire();")
    reopened = build_report()
    shoot = _representative(reopened, "unity:AtomDef:Shoot")
    assert shoot["classification"] == CONNECTED_BUT_NOT_ATTACHED
    assert "does not call FiringProviders.FirePlayer()" in shoot["missing_seam"]
    assert any("emitted but unattached" in error for error in validate_report(reopened))


def test_a_second_firing_implementer_is_ambiguous_not_picked(monkeypatch):
    from src.atoms import ownership_audit

    monkeypatch.setattr(ownership_audit, "_firing_owner_candidates", lambda: {
        "templates/unity/micro/controller/Controller.cs.j2:28": "ControllerPipeline",
        "templates/unity/micro/turret/Turret.cs.j2:9": "TurretPipeline",
    })
    shoot = _representative(build_report(), "unity:AtomDef:Shoot")
    assert shoot["classification"] == AMBIGUOUS_OWNER
    assert shoot["owner_candidates"] == ["ControllerPipeline", "TurretPipeline"]


def test_firing_owners_are_read_from_class_declarations_only():
    from src.atoms.ownership_audit import _IMPLEMENTS_FIRING, _firing_owner_candidates

    assert list(_firing_owner_candidates().values()) == ["ControllerPipeline"]
    assert _IMPLEMENTS_FIRING.search(
        'public class {{ s.name }} : MonoBehaviour{% if x %}, IFiringProvider{% endif %}')
    assert _IMPLEMENTS_FIRING.search("public sealed class Turret : MonoBehaviour, IFiringProvider")
    assert not _IMPLEMENTS_FIRING.search("public interface IFiringProvider")
    assert not _IMPLEMENTS_FIRING.search("// A shooting controller implements IFiringProvider")
