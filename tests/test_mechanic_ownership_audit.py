"""S3 structural mechanic ownership audit tests; no runtime claims."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from src.atoms.ownership_audit import (
    AMBIGUOUS_OWNER,
    CONNECTED_AND_OWNED,
    CONNECTED_BUT_NOT_ATTACHED,
    MISSING_PROVIDER,
    REPORT,
    ROOT,
    OwnershipEvidenceError,
    _has_hosted_script_binding,
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
    sequencer = _representative(report, "unity:UnitySchema.atoms:Wait")
    companion = _representative(report, "unity:MICROGEN_ATOM_MAP:controller:Move")
    assert sequencer["classification"] == CONNECTED_BUT_NOT_ATTACHED
    assert companion["classification"] == CONNECTED_BUT_NOT_ATTACHED
    assert sequencer["missing_seam"]
    assert companion["missing_seam"]


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


def test_missing_provider_preserves_existing_debt_slice(report):
    shoot = _representative(report, "unity:AtomDef:Shoot")
    assert shoot["classification"] == MISSING_PROVIDER
    assert shoot["provider"] is None
    assert shoot["next_slice"] == "S5"
    assert shoot["template_owner_preserved"] == "controller/projectile templates"


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
