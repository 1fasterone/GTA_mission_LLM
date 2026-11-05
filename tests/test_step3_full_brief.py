"""Synthetic briefs + mocked fills verify transport, not Pipeline LLM fidelity."""
from unittest.mock import MagicMock

from src.unity_pipeline import UnityPipeline
from src.unity_schema import UnitySchema, UnityScript


def test_step3_sends_the_full_long_brief_to_the_model(tmp_path):
    brief = ("The garden contains paths and flowers. " * 300
             + "\nFinal requirement: preserve the lavender fountain at the exit.")
    schema = UnitySchema(
        project_name="FullBriefTransport", game_type="2D", platform="pc",
        render_pipeline="URP", scenes=["Main"],
        scripts=[UnityScript(name="PlayerController", type="player",
                             description="Player movement")],
    )
    pipeline = UnityPipeline({
        "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "test-model"},
        "unity_pipeline": {"lookups_dir": "lookups", "unity_template_dir": "templates/unity",
                           "unity_output_dir": str(tmp_path)},
        "assets": {"strict": False},
    })
    chat = MagicMock(side_effect=["A garden platform game.", schema.model_dump_json()])
    pipeline.llm.chat = chat
    pipeline.run(brief, theme="none", micro_code=False)
    schema_request = chat.call_args_list[1].kwargs["user"]
    assert brief in schema_request
