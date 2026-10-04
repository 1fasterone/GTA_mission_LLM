"""Hermetic acceptance for a captured Step-3.5 schema replay."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.scene_graph import build_scene_graph
from src.unity_schema import UnitySchema
from src.unity_spatial_fusion import SpatialFusionError, replay_frozen_spatial_fusion
from src.unity_spatial_realization import realize_spatial_layout
from src.unity_spatial_roles import derive_spatial_roles
from src.unity_spatial_validation import validate_spatial_layout


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
MANIFEST = FIXTURES / "skyline_strike_step35.acceptance.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _command(*args: str) -> list[str]:
    return [sys.executable, str(REPO_ROOT / "main.py"), *args]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(*args),
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _replay_config(tmp_path: Path) -> Path:
    """A deliberately offline config for the no-LLM replay subprocess."""
    path = tmp_path / "replay.config.json"
    path.write_text(json.dumps({
        "lm_studio": {
            "backend": "lm_studio",
            "base_url": "http://127.0.0.1:9/v1",
            "model": "must-not-be-called",
        },
        "basic_mode": True,
        "unity_pipeline": {
            "lookups_dir": str(REPO_ROOT / "lookups"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
            "unity_output_dir": str(tmp_path / "project-output"),
            "inject_mcp_bridge": False,
            "syntax_gate": False,
            "package_cache": {"enabled": False},
        },
        "assets": {"strict": False},
        "sketchfab": {
            "enabled": False,
            "collect": False,
            "auto_fetch": False,
            "vision_select": False,
            "semantic_select": False,
        },
        "audio": {"backend": "dsp"},
        "acceptance": {"reject_lossy_normalization": True},
    }, indent=2) + "\n", encoding="utf-8")
    return path


def test_captured_step35_fixture_replays_without_llm_and_passes_strict_gates(
    tmp_path: Path,
) -> None:
    """A captured Step-3.5 payload must survive the deterministic tail intact.

    This is intentionally an acceptance test, not an engineer-authored schema
    test: it uses a live-captured schema containing LLM-authored micro inputs,
    replays the frozen visual fusion without its original image services, then
    runs the supported ``unity-validate --compose`` no-LLM command.
    """
    manifest = _load_json(MANIFEST)
    schema_path = FIXTURES / manifest["schema"]
    schema = UnitySchema.model_validate(_load_json(schema_path))

    enrichment = schema.meta.get("llm_enrichment")
    assert isinstance(enrichment, dict)
    assert enrichment.get("format") == manifest["step_3_5"]["enrichment_format"]
    assert enrichment.get("stage") == manifest["step_3_5"]["stage"]
    authored = [micro for micro in schema.microgens if micro.input]
    assert manifest["step_3_5"]["requires_nonempty_micro_inputs"] is True
    assert len(authored) >= manifest["step_3_5"]["minimum_authored_microgens"]

    frozen_fusion = _load_json(FIXTURES / manifest["spatial"]["spatial_fusion"])
    replayed_fusion = replay_frozen_spatial_fusion(schema, frozen_fusion)
    assert replayed_fusion is not frozen_fusion
    assert replayed_fusion["authority"]["llm_called_here"] is False
    assert replayed_fusion["authority"]["replayed_from_frozen_fusion"] is True
    assert replayed_fusion["inputs"]["captured_schema_semantic_sha256"]

    roles = derive_spatial_roles(schema)
    world_layout = realize_spatial_layout(
        schema.model_copy(deep=True),
        relation_graph=build_scene_graph(schema, roles),
        spatial_fusion=replayed_fusion,
    )
    spatial = validate_spatial_layout(
        schema,
        world_layout=world_layout,
        spatial_roles=roles,
        spatial_fusion=replayed_fusion,
    )
    assert spatial["status"] == manifest["spatial"]["required_status"]
    assert not spatial["warnings"]
    assert not spatial["errors"]
    assert {check["status"] for check in spatial["checks"]} == {"pass"}

    config = _replay_config(tmp_path)
    replay = _run(
        "unity-validate", str(schema_path), "--config", str(config), "--compose",
    )
    assert replay.returncode == 0, replay.stdout + "\n" + replay.stderr

    project = tmp_path / "project-output" / schema.project_name
    generated_schema = project / f"{schema.project_name}.schema.json"
    assert generated_schema.is_file()
    emitted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((project / "Assets" / "Micros").rglob("*.cs"))
    )
    assert "LaunchMissile" in emitted
    assert "OnMissileLaunch" in emitted

    playability = _run(
        "unity-playability", str(generated_schema), "--strict", "--min-score",
        str(manifest["playability"]["minimum_score"]),
    )
    assert playability.returncode == 0, playability.stdout + "\n" + playability.stderr


def test_frozen_fusion_replay_refuses_to_drop_a_captured_binding() -> None:
    manifest = _load_json(MANIFEST)
    schema = UnitySchema.model_validate(_load_json(FIXTURES / manifest["schema"]))
    incomplete = copy.deepcopy(
        _load_json(FIXTURES / manifest["spatial"]["spatial_fusion"])
    )
    incomplete["entity_regions"][0]["binding_status"] = "unbound"

    with pytest.raises(SpatialFusionError, match="one bound visual reference"):
        replay_frozen_spatial_fusion(schema, incomplete)
