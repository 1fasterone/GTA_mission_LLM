import json

from src.audio import stable3_batch
from src.audio.batch import write_stable3_batch
from src.micro.audio import AudioPipeline


def test_write_stable3_batch_emits_prompts_and_pending_manifest(tmp_path):
    batch = tmp_path / "Assets" / "Micros" / "audio" / "audio_batch.json"
    write_stable3_batch(
        [
            {
                "id": "wind",
                "category": "ambient",
                "kind": "gen",
                "descriptor": "cold basement wind",
                "gen_prompt": "low industrial wind through concrete corridors",
                "duration_s": 8,
            },
            {
                "id": "robot_shot",
                "category": "sfx",
                "kind": "gen",
                "descriptor": "robot laser shot",
                "duration_s": 4,
            },
        ],
        batch,
        seed=7,
        project_name="BasementFps",
    )

    payload = json.loads(batch.read_text(encoding="utf-8"))
    assert payload["status"] == "pending"
    assert payload["cues"][0]["prompt"].startswith("low industrial")
    assert payload["cues"][0]["stable3_model"] == "music"
    assert payload["cues"][1]["stable3_model"] == "sfx"
    manifest = json.loads(
        (batch.parent / "Resources" / "audio_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "pending_stable3_batch"
    assert manifest["cue_count"] == 2


def test_standalone_batch_renders_and_overlays_without_gpu(tmp_path, monkeypatch):
    project = tmp_path / "Game"
    batch = project / "Assets" / "Micros" / "audio" / "audio_batch.json"
    write_stable3_batch(
        [{"id": "wind", "category": "ambient", "descriptor": "wind", "duration_s": 4}],
        batch,
    )
    staging = batch.parent / "Resources"

    def fake_generate(cues, resources_dir, config, seed=0):
        assert config["audio"]["backend"] == "stable3"
        assert seed == 0
        clip = resources_dir / "Audio" / "wind" / "wind_0.wav"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"wav")
        manifest = {
            "backend": "stable3",
            "cue_count": len(cues),
            "cues": [{"id": "wind", "clips": ["Audio/wind/wind_0.wav"]}],
            "warnings": [],
        }
        (resources_dir / "audio_manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return manifest

    monkeypatch.setattr(stable3_batch, "generate_bank", fake_generate)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"batch_path": str(batch), "config": {"audio": {}}, "unity": None}),
        encoding="utf-8",
    )
    result = stable3_batch.run_request(request)

    assert result["status"] == "complete"
    assert (project / "Assets" / "Audio" / "wind" / "wind_0.wav").read_bytes() == b"wav"
    assert (project / "Assets" / "Resources" / "audio_manifest.json").is_file()
    assert not staging.exists()
    assert (batch.parent / "audio_batch.result.json").is_file()


def test_audio_micro_defers_stable3_without_loading_gpu_model(tmp_path):
    pipe = AudioPipeline(
        {
            "lm_studio": {"backend": "lm_studio", "model": "test"},
            "unity_pipeline": {"unity_output_dir": str(tmp_path / "unity")},
            "audio": {
                "backend": "stable3",
                "defer_stable3": True,
                "generate_clips": True,
            },
        }
    )
    out = pipe.run(
        "a robot shooter with warning sirens",
        use_llm=False,
        out=str(tmp_path / "bank"),
    )

    assert (out / "audio_batch.json").is_file()
    assert (out / "Resources" / "audio_manifest.json").is_file()
    assert not list((out / "Resources").rglob("*.wav"))
