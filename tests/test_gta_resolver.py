"""
Tests for ModelResolver: local lookup fallback, RAG integration (mocked),
resolve_schema population, and audio resolution.
No real network calls — RagClient.resolve_model/resolve_audio are always mocked.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.resolver import ModelResolver, _local_resolve, _load_lookups
from src.mission_schema import MissionAudio, MissionObject, MissionPed, MissionSchema, MissionStep, MissionVehicle
from rag.query import RagClient


# ── Shared helpers ─────────────────────────────────────────────────────────────

LOOKUPS_DIR = "lookups"


@pytest.fixture
def lookups() -> dict:
    return _load_lookups(LOOKUPS_DIR)


@pytest.fixture
def mock_rag() -> MagicMock:
    """RagClient with resolve_model mocked to return empty (triggers local fallback)."""
    rag = MagicMock(spec=RagClient)
    rag.resolve_model.return_value = None   # forces local fallback
    rag.resolve_audio.return_value = None
    rag.is_corpus_backed = False   # default: exercise the valid_models guard like most callers
    return rag


@pytest.fixture
def resolver(mock_rag) -> ModelResolver:
    return ModelResolver(rag=mock_rag, lookups_dir=LOOKUPS_DIR, fallback=True)


# ══════════════════════════════════════════════════════════════════════════════
# _load_lookups
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadLookups:
    def test_loads_peds(self, lookups):
        assert "peds" in lookups

    def test_loads_vehicles(self, lookups):
        assert "vehicles" in lookups

    def test_loads_objects(self, lookups):
        assert "objects" in lookups

    def test_loads_weapons(self, lookups):
        assert "weapons" in lookups

    def test_peds_has_synonyms(self, lookups):
        assert "synonyms" in lookups["peds"]
        assert len(lookups["peds"]["synonyms"]) > 0

    def test_missing_dir_returns_empty_dict(self):
        result = _load_lookups("nonexistent_dir_xyz")
        assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# _local_resolve
# ══════════════════════════════════════════════════════════════════════════════

class TestLocalResolve:
    def test_exact_synonym_match_peds(self, lookups):
        """'cop' is a known synonym → resolves to 's_m_y_cop_01'."""
        result = _local_resolve("cop", "peds", lookups)
        assert result == "s_m_y_cop_01"

    def test_exact_synonym_match_vehicles(self, lookups):
        """'police car' is a known synonym → resolves to 'police'."""
        result = _local_resolve("police car", "vehicles", lookups)
        assert result == "police"

    def test_exact_synonym_match_objects(self, lookups):
        """'drug package' is a known synonym → resolves to 'prop_drug_package_01'."""
        result = _local_resolve("drug package", "objects", lookups)
        assert result == "prop_drug_package_01"

    def test_substring_match_finds_result(self, lookups):
        """A description containing a synonym key should still match."""
        # "cop" is a key; "armed cop" contains "cop"
        result = _local_resolve("armed cop", "peds", lookups)
        assert result is not None
        assert isinstance(result, str)

    def test_unknown_description_returns_none(self, lookups):
        result = _local_resolve("xyzzy_nonexistent_model_999", "peds", lookups)
        assert result is None

    def test_unknown_category_returns_none(self, lookups):
        result = _local_resolve("cop", "spaceships", lookups)
        assert result is None

    def test_case_insensitive(self, lookups):
        """Descriptions should match regardless of case."""
        lower = _local_resolve("police car", "vehicles", lookups)
        upper = _local_resolve("POLICE CAR", "vehicles", lookups)
        assert lower == upper

    def test_ped_synonyms_are_valid_models(self, lookups):
        peds = lookups["peds"]
        valid = set(peds["valid_models"])
        missing = {
            label: model
            for label, model in peds["synonyms"].items()
            if model not in valid
        }
        assert missing == {}


# ══════════════════════════════════════════════════════════════════════════════
# ModelResolver.resolve — RAG + local fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestModelResolverResolve:
    def test_local_fallback_when_rag_returns_none(self, resolver):
        """RAG returns None → local lookup resolves 'cop' → 's_m_y_cop_01'."""
        result = resolver.resolve("cop", "peds")
        assert result == "s_m_y_cop_01"

    def test_local_fallback_resolves_vehicle(self, resolver):
        result = resolver.resolve("police car", "vehicles")
        assert result == "police"

    def test_local_fallback_resolves_object(self, resolver):
        result = resolver.resolve("drug package", "objects")
        assert result == "prop_drug_package_01"

    def test_returns_unresolved_when_all_fallbacks_fail(self, resolver):
        result = resolver.resolve("martian_hovercraft_xyz", "vehicles")
        assert result == "UNRESOLVED"

    def test_rag_result_used_when_valid_and_in_valid_models(self, mock_rag):
        """When RAG returns a valid model ID that's in valid_models, use it."""
        mock_rag.resolve_model.return_value = "s_m_y_cop_01"
        res = ModelResolver(rag=mock_rag, lookups_dir=LOOKUPS_DIR, fallback=True)
        result = res.resolve("some cop", "peds")
        assert result == "s_m_y_cop_01"

    def test_rag_result_rejected_when_not_in_valid_models(self, mock_rag):
        """If RAG returns an ID that isn't in valid_models, fall back to local."""
        mock_rag.resolve_model.return_value = "definitely_not_a_real_model_xyz"
        res = ModelResolver(rag=mock_rag, lookups_dir=LOOKUPS_DIR, fallback=True)
        # Local fallback for "cop" should win
        result = res.resolve("cop", "peds")
        assert result == "s_m_y_cop_01"

    def test_rag_exception_triggers_local_fallback(self, mock_rag):
        """If RAG raises, the local lookup fallback should still fire."""
        mock_rag.resolve_model.side_effect = ConnectionError("RAG server down")
        res = ModelResolver(rag=mock_rag, lookups_dir=LOOKUPS_DIR, fallback=True)
        result = res.resolve("cop", "peds")
        assert result == "s_m_y_cop_01"

    def test_fallback_false_skips_local_lookup(self, mock_rag):
        """fallback=False: RAG fails → return UNRESOLVED without local lookup."""
        mock_rag.resolve_model.return_value = None
        res = ModelResolver(rag=mock_rag, lookups_dir=LOOKUPS_DIR, fallback=False)
        result = res.resolve("cop", "peds")
        assert result == "UNRESOLVED"


# ══════════════════════════════════════════════════════════════════════════════
# ModelResolver.resolve_audio_name
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveAudioName:
    def test_rag_returns_audio_name_used(self, resolver, mock_rag):
        mock_rag.resolve_audio.return_value = "CUSTOM_JINGLE"
        result = resolver.resolve_audio_name("mission success", "mission_passed")
        assert result == "CUSTOM_JINGLE"

    def test_rag_none_falls_back_to_default_passed(self, resolver):
        result = resolver.resolve_audio_name("done", "mission_passed")
        assert result == "MISSION_PASSED_JINGLE"

    def test_rag_none_falls_back_to_default_failed(self, resolver):
        result = resolver.resolve_audio_name("failed", "mission_failed")
        assert result == "MISSION_FAILED_JINGLE"

    def test_rag_none_falls_back_to_default_ambient(self, resolver):
        result = resolver.resolve_audio_name("beach sounds", "ambient")
        assert result == "DLC_ISLANDS_AMBIENCE_EXTERIOR"

    def test_rag_none_falls_back_to_default_sting(self, resolver):
        result = resolver.resolve_audio_name("dramatic moment", "sting")
        assert result == "STING_SUBTLE_1"

    def test_rag_exception_falls_back_to_default(self, resolver, mock_rag):
        mock_rag.resolve_audio.side_effect = RuntimeError("timeout")
        result = resolver.resolve_audio_name("done", "mission_passed")
        assert result == "MISSION_PASSED_JINGLE"


# ══════════════════════════════════════════════════════════════════════════════
# ModelResolver.resolve_schema — full schema population
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveSchema:
    def _bare_schema(self, peds=None, vehicles=None, objects=None, audio=None) -> MissionSchema:
        return MissionSchema(
            mission_name="Test",
            location_name="docks",
            steps=[MissionStep(type="go_to", label="Go")],
            peds=peds or [],
            vehicles=vehicles or [],
            objects=objects or [],
            audio=audio or [],
        )

    def test_ped_model_filled_from_local_lookup(self, resolver):
        schema = self._bare_schema(peds=[
            MissionPed(description="cop", role="guard"),
        ])
        result = resolver.resolve_schema(schema)
        assert result.peds[0].model == "s_m_y_cop_01"

    def test_vehicle_model_filled_from_local_lookup(self, resolver):
        schema = self._bare_schema(vehicles=[
            MissionVehicle(description="police car", role="patrol"),
        ])
        result = resolver.resolve_schema(schema)
        assert result.vehicles[0].model == "police"

    def test_object_model_filled_from_local_lookup(self, resolver):
        schema = self._bare_schema(objects=[
            MissionObject(description="drug package", role="pickup"),
        ])
        result = resolver.resolve_schema(schema)
        assert result.objects[0].model == "prop_drug_package_01"

    def test_already_resolved_ped_not_overwritten(self, resolver):
        schema = self._bare_schema(peds=[
            MissionPed(description="cop", model="custom_ped_model", role="guard"),
        ])
        result = resolver.resolve_schema(schema)
        assert result.peds[0].model == "custom_ped_model"

    def test_audio_name_filled_when_missing(self, resolver):
        schema = self._bare_schema(audio=[
            MissionAudio(type="mission_passed", context="success"),
        ])
        result = resolver.resolve_schema(schema)
        assert result.audio[0].name is not None
        assert len(result.audio[0].name) > 0

    def test_all_unresolved_after_schema_resolve_reported(self, resolver):
        """Unknown descriptions end up UNRESOLVED, detectable via unresolved_models()."""
        schema = self._bare_schema(peds=[
            MissionPed(description="martian_alien_xyz_9999", role="guard"),
        ])
        result = resolver.resolve_schema(schema)
        assert "UNRESOLVED" in result.unresolved_models()[0] or result.peds[0].model == "UNRESOLVED"

    def test_multiple_entities_all_resolved(self, resolver):
        schema = self._bare_schema(
            peds=[MissionPed(description="cop", role="guard")],
            vehicles=[MissionVehicle(description="police car", role="patrol")],
            objects=[MissionObject(description="drug package", role="pickup")],
        )
        result = resolver.resolve_schema(schema)
        assert result.unresolved_models() == []

    def test_distinct_common_ped_choices_resolve_to_distinct_models(self, resolver):
        schema = self._bare_schema(peds=[
            MissionPed(description="cop", role="guard"),
            MissionPed(description="security guard", role="guard"),
            MissionPed(description="armed guard", role="guard"),
            MissionPed(description="robber", role="target"),
            MissionPed(description="biker thug", role="target"),
            MissionPed(description="cartel member", role="target"),
        ])
        result = resolver.resolve_schema(schema)
        models = [ped.model for ped in result.peds]
        assert len(models) == len(set(models))
