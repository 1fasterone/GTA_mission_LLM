"""The Unity build manifest must bind a project to deterministic inputs."""

import json

import pytest

from src.unity_build_provenance import (FORMAT, MANIFEST_NAME,
                                        write_build_provenance)
from src.unity_schema import UnitySchema


def _schema() -> UnitySchema:
    return UnitySchema.model_validate({
        "project_name": "ProvenanceCheck",
        "game_type": "3D",
        "platform": "pc",
        "render_pipeline": "URP",
    })


def test_provenance_is_stable_redacts_secrets_and_tracks_input_changes(tmp_path):
    output = tmp_path / "output"
    root = output / "ProvenanceCheck"
    root.mkdir(parents=True)
    (root / ".generated_files.json").write_text(
        json.dumps(["ProvenanceCheck/Assets/Script.cs"]), encoding="utf-8")
    generated = root / "Assets"
    generated.mkdir(parents=True)
    (generated / "Script.cs").write_text("class Script {}\n", encoding="utf-8")
    (root / "ProvenanceCheck.schema.json").write_text(
        _schema().model_dump_json(indent=2), encoding="utf-8")

    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "Script.cs.j2"
    template.write_text("class {{ name }} {}\n", encoding="utf-8")
    lookups = tmp_path / "lookups"
    lookups.mkdir()
    (lookups / "packages.json").write_text('{"packages": []}\n', encoding="utf-8")

    kwargs = {
        "project_root": root,
        "manifest_deps": {"com.unity.inputsystem": "1.8.2"},
        "config": {"api_token": "must-not-appear", "nested": {"secret": "nope"}},
        "template_dir": templates,
        "lookups_dir": lookups,
    }
    first = write_build_provenance(_schema(), **kwargs)
    first_raw = first.read_text(encoding="utf-8")
    second = write_build_provenance(_schema(), **kwargs)

    assert first == second == root / MANIFEST_NAME
    assert second.read_text(encoding="utf-8") == first_raw
    assert "must-not-appear" not in first_raw and "nope" not in first_raw
    payload = json.loads(first_raw)
    assert payload["format"] == FORMAT
    assert payload["status"] == "unverified"
    assert payload["outputs"]["generated_files"]["declared_file_count"] == 1
    [generated_record] = payload["outputs"]["generated_files"]["files"]
    assert generated_record["path"] == "ProvenanceCheck/Assets/Script.cs"
    assert generated_record["identity"] is not None
    first_generated_identity = payload["outputs"]["generated_files"]["identity"]

    (generated / "Script.cs").write_text("class Script { int Value; }\n", encoding="utf-8")
    changed_output = write_build_provenance(_schema(), **kwargs)
    payload_output_changed = json.loads(changed_output.read_text(encoding="utf-8"))
    assert (payload_output_changed["outputs"]["generated_files"]["identity"]
            != first_generated_identity)

    template.write_text("class {{ changed }} {}\n", encoding="utf-8")
    changed = write_build_provenance(_schema(), **kwargs)
    payload_changed = json.loads(changed.read_text(encoding="utf-8"))
    assert (payload_changed["compiler_inputs"]["templates"]["identity"]
            != payload["compiler_inputs"]["templates"]["identity"])


def test_provenance_rejects_unknown_status(tmp_path):
    with pytest.raises(ValueError, match="unsupported build provenance status"):
        write_build_provenance(
            _schema(),
            project_root=tmp_path / "Project",
            manifest_deps={}, config={}, template_dir=tmp_path,
            lookups_dir=tmp_path, status="definitely-accepted",
        )
