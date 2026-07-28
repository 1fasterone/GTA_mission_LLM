"""
Tests for LMStudioClient: JSON extraction, truncation repair, and chat behaviour.
No network calls — all HTTP is mocked via unittest.mock.
"""
import json
import pytest

from src.lm_studio_client import _drop_surplus_closers, LMStudioClient, _repair_truncated_json
from src.backends import active_chat_cfg, make_chat_client


# ══════════════════════════════════════════════════════════════════════════════
# Fixed-seed reproducibility (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════

class TestSeed:
    def test_payload_omits_seed_when_unset(self):
        c = LMStudioClient("http://x/v1", "m")
        p = c._build_payload("sys", "usr", None, True, None)
        assert "seed" not in p

    def test_client_seed_in_payload(self):
        c = LMStudioClient("http://x/v1", "m", seed=1234)
        p = c._build_payload("sys", "usr", 0.2, False, None)
        assert p["seed"] == 1234

    def test_per_call_seed_overrides_client(self):
        c = LMStudioClient("http://x/v1", "m", seed=1234)
        p = c._build_payload("sys", "usr", None, True, 99)
        assert p["seed"] == 99

    def test_config_seed_plumbs_to_client(self):
        cfg = {"lm_studio": {"base_url": "http://x/v1", "model": "m", "seed": 777}}
        assert active_chat_cfg(cfg)["seed"] == 777
        assert make_chat_client(cfg).seed == 777

    def test_ollama_inherits_lm_studio_seed(self):
        cfg = {"lm_studio": {"backend": "ollama", "seed": 555},
               "ollama": {"base_url": "http://y/v1", "model": "q"}}
        assert active_chat_cfg(cfg)["seed"] == 555

    def test_no_seed_by_default(self):
        assert active_chat_cfg({"lm_studio": {}}).get("seed") is None
        assert make_chat_client({"lm_studio": {}}).seed is None


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> LMStudioClient:
    return LMStudioClient(
        base_url="http://localhost:1234/v1",
        model="test-model",
        temperature=0.7,
        max_tokens=4096,
        timeout=10.0,
    )


# ══════════════════════════════════════════════════════════════════════════════
# _repair_truncated_json
# ══════════════════════════════════════════════════════════════════════════════

class TestRepairTruncatedJson:
    def test_closes_single_unclosed_brace(self):
        text = '{"key": "value"'
        repaired = _repair_truncated_json(text, 0)
        assert repaired.endswith("}")
        assert json.loads(repaired) == {"key": "value"}

    def test_closes_nested_unclosed_braces(self):
        text = '{"outer": {"inner": 1'
        repaired = _repair_truncated_json(text, 0)
        assert repaired.endswith("}}")
        parsed = json.loads(repaired)
        assert parsed["outer"]["inner"] == 1

    def test_closes_unclosed_array(self):
        text = '{"steps": [1, 2, 3'
        repaired = _repair_truncated_json(text, 0)
        parsed = json.loads(repaired)
        assert parsed["steps"] == [1, 2, 3]

    def test_closes_unclosed_string_then_brace(self):
        # String was cut mid-value
        text = '{"name": "Docks Hei'
        repaired = _repair_truncated_json(text, 0)
        # Should close the string and the object
        assert json.loads(repaired)["name"] == "Docks Hei"

    def test_already_complete_json_unchanged(self):
        text = '{"a": 1}'
        repaired = _repair_truncated_json(text, 0)
        assert json.loads(repaired) == {"a": 1}

    def test_respects_start_offset(self):
        text = 'JUNK{"key": "val"'
        repaired = _repair_truncated_json(text, 4)
        assert json.loads(repaired) == {"key": "val"}

    def test_deeply_nested_mixed_types(self):
        text = '{"a": [{"b": [1, 2'
        repaired = _repair_truncated_json(text, 0)
        parsed = json.loads(repaired)
        assert parsed["a"][0]["b"] == [1, 2]


# ══════════════════════════════════════════════════════════════════════════════
# LMStudioClient.extract_json
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractJson:
    def test_plain_json_object(self, client):
        raw = '{"mission_name": "Heist", "weather": "CLEAR"}'
        result = client.extract_json(raw)
        assert result["mission_name"] == "Heist"
        assert result["weather"] == "CLEAR"

    def test_json_inside_markdown_fence(self, client):
        raw = '```json\n{"key": "value"}\n```'
        result = client.extract_json(raw)
        assert result["key"] == "value"

    def test_json_inside_backtick_fence_no_lang(self, client):
        raw = '```\n{"key": 42}\n```'
        result = client.extract_json(raw)
        assert result["key"] == 42

    def test_strips_think_block_before_json(self, client):
        raw = '<think>Let me reason...</think>\n{"answer": "yes"}'
        result = client.extract_json(raw)
        assert result["answer"] == "yes"

    def test_strips_multiline_think_block(self, client):
        raw = '<think>\nLine 1\nLine 2\n</think>\n{"x": 1}'
        result = client.extract_json(raw)
        assert result["x"] == 1

    def test_json_with_preamble_text(self, client):
        raw = 'Here is the schema:\n{"mission_name": "Test"}'
        result = client.extract_json(raw)
        assert result["mission_name"] == "Test"

    def test_nested_json_fully_parsed(self, client):
        data = {"steps": [{"type": "go_to", "label": "Go"}], "peds": []}
        raw = json.dumps(data)
        result = client.extract_json(raw)
        assert result["steps"][0]["type"] == "go_to"

    def test_raises_value_error_when_no_json(self, client):
        with pytest.raises(ValueError, match="No JSON object"):
            client.extract_json("This response has no JSON at all.")

    def test_raises_value_error_on_empty_string(self, client):
        with pytest.raises(ValueError, match="No JSON object"):
            client.extract_json("")

    def test_truncated_json_is_repaired(self, client):
        # Simulate a response cut off mid-object
        raw = '{"mission_name": "Heist", "steps": [{"type": "go_to"'
        result = client.extract_json(raw)
        assert result["mission_name"] == "Heist"

    def test_think_block_plus_markdown_fence(self, client):
        raw = '<think>reasoning</think>\n```json\n{"val": 99}\n```'
        result = client.extract_json(raw)
        assert result["val"] == 99

    def test_csharp_float_suffix_is_stripped(self, client):
        # Llama 3.1 / deepseek bleed C# literals into JSON: "interval": 10f
        raw = '{"spawn_interval": 10f, "spawn_radius": 1.5f, "max_alive": 50}'
        result = client.extract_json(raw)
        assert result["spawn_interval"] == 10
        assert result["spawn_radius"] == 1.5
        assert result["max_alive"] == 50

    def test_csharp_suffix_in_array_stripped(self, client):
        raw = '{"weights": [1.0f, 2.5f, 3f]}'
        result = client.extract_json(raw)
        assert result["weights"] == [1.0, 2.5, 3]

    def test_trailing_comma_is_repaired(self, client):
        raw = '{"a": 1, "b": 2,}'
        result = client.extract_json(raw)
        assert result == {"a": 1, "b": 2}

    def test_suffix_inside_string_is_preserved(self, client):
        # A genuine "10f" inside a string value must NOT be mangled.
        raw = '{"description": "drop 10f of rope", "interval": 10f}'
        result = client.extract_json(raw)
        assert result["description"] == "drop 10f of rope"
        assert result["interval"] == 10

    def test_multiple_json_objects_returns_first(self, client):
        # Only the first balanced object should be returned
        raw = '{"first": 1} {"second": 2}'
        result = client.extract_json(raw)
        assert result == {"first": 1}

    def test_boolean_and_null_values(self, client):
        raw = '{"flag": true, "nothing": null, "count": 0}'
        result = client.extract_json(raw)
        assert result["flag"] is True
        assert result["nothing"] is None
        assert result["count"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# LMStudioClient.chat — temperature clamping (no HTTP needed)
# ══════════════════════════════════════════════════════════════════════════════

class TestChatTemperatureClamping:
    """Verify the 0.05 floor on temperature without making real HTTP calls."""

    def test_temperature_floor_applied(self, client):
        """Temperature 0.0 must be clamped to 0.05 in the outgoing payload."""
        from unittest.mock import MagicMock, patch

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "response text"}}]
        }

        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["temperature"] = json["temperature"]
            return mock_response

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            client.chat(system="sys", user="user", temperature=0.0)

        assert captured["temperature"] == 0.05

    def test_explicit_temperature_passed_through(self, client):
        """A temperature above the floor is forwarded unchanged."""
        from unittest.mock import MagicMock, patch

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }

        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["temperature"] = json["temperature"]
            return mock_response

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            client.chat(system="sys", user="user", temperature=0.8)

        assert captured["temperature"] == 0.8


class TestChatReasoningModels:
    """Behaviour with reasoning models (Phi-4-reasoning) and slow/CPU-offloaded models."""

    def test_empty_content_with_reasoning_gives_budget_message(self, client):
        from unittest.mock import MagicMock, patch
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning_content": "lots of thinking..."}}]
        }
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = mock_response
            with pytest.raises(ValueError, match="token budget reasoning"):
                client.chat(system="sys", user="user")

    def test_timeout_raises_timeouterror_not_valueerror(self, client):
        import httpx
        from unittest.mock import patch
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = \
                httpx.ReadTimeout("timed out")
            # TimeoutError (not ValueError) so the schema-fill retry doesn't re-wait.
            with pytest.raises(TimeoutError, match="did not respond within"):
                client.chat(system="sys", user="user")

    def test_no_think_prefix_when_thinking_false(self, client):
        """thinking=False must prepend /no_think to the user message."""
        from unittest.mock import MagicMock, patch

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }

        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["user_content"] = json["messages"][1]["content"]
            return mock_response

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            client.chat(system="sys", user="my prompt", thinking=False)

        assert captured["user_content"].startswith("/no_think")

    def test_thinking_true_sends_raw_user_message(self, client):
        """thinking=True must not add /no_think prefix."""
        from unittest.mock import MagicMock, patch

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }

        captured = {}

        def fake_post(url, json=None, **kwargs):
            captured["user_content"] = json["messages"][1]["content"]
            return mock_response

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            client.chat(system="sys", user="my prompt", thinking=True)

        assert "/no_think" not in captured["user_content"]
        assert "my prompt" in captured["user_content"]


# ══════════════════════════════════════════════════════════════════════════════
# _drop_surplus_closers  (the mirror of _repair_truncated_json)
# ══════════════════════════════════════════════════════════════════════════════

class TestDropSurplusClosers:
    def test_balanced_json_unchanged(self):
        text = '{"a": [1, 2], "b": {"c": 3}}'
        assert _drop_surplus_closers(text, 0) == text

    def test_drops_surplus_array_terminator(self):
        # The live shape: element object closes, array closes, then one closer
        # too many before the next key.
        text = '{"vfx": [{"p": {"loop": false}}], "shaders": []}'
        broken = '{"vfx": [{"p": {"loop": false}}]  ], "shaders": []}'
        assert json.loads(_drop_surplus_closers(broken, 0)) == json.loads(text)

    def test_drops_surplus_brace(self):
        broken = '{"a": {"b": 1}}, "c": 2}'
        assert json.loads(_drop_surplus_closers(broken, 0)) == {"a": {"b": 1}}

    def test_ignores_closers_inside_strings(self):
        text = '{"note": "an ] and a } live here"}'
        assert _drop_surplus_closers(text, 0) == text

    def test_stops_at_root_close_dropping_trailing_prose(self):
        text = '{"a": 1}\n\nHope that helps!'
        assert _drop_surplus_closers(text, 0) == '{"a": 1}'

    def test_respects_start_offset(self):
        text = 'preamble {"a": [1]]}'
        assert json.loads(_drop_surplus_closers(text, 9)) == {"a": [1]}


class TestExtractJsonSurplusClosers:
    def test_recovers_object_with_surplus_closers(self, client):
        """extract_json's balanced-brace scan bottoms out early on a surplus
        closer, so the slice it takes is short AND malformed. It must still
        recover every key, including those after the corruption point."""
        raw = (
            '{"project_name": "RingTargetArcade",'
            ' "vfx": [{"id": "throw", "parameters": {"loop": false}}  ],'
            ' "materials": [{"id": "ring", "parameters": {"color": "#4A90D9"}}  ],'
            ' "scripts": ["ThrowController"]}'
        )
        result = client.extract_json(raw)
        assert result["project_name"] == "RingTargetArcade"
        assert result["vfx"][0]["id"] == "throw"
        assert result["materials"][0]["id"] == "ring"
        assert result["scripts"] == ["ThrowController"]

    def test_valid_json_still_parses_unchanged(self, client):
        raw = '{"a": {"b": [1, 2, 3]}, "c": "]}"}'
        assert client.extract_json(raw) == {"a": {"b": [1, 2, 3]}, "c": "]}"}
