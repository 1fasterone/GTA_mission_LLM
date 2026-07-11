"""
Tests for MissionSchema, MissionPed, MissionVehicle, MissionObject, MissionStep.
Covers field validation, normalisation validators, and unresolved_models().
"""
import pytest
from pydantic import ValidationError

from src.mission_schema import (
    MissionAudio,
    MissionObject,
    MissionPed,
    MissionSchema,
    MissionStep,
    MissionVehicle,
)


# ══════════════════════════════════════════════════════════════════════════════
# MissionStep
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionStep:
    def test_valid_step_types(self):
        valid = ["go_to", "kill_all", "pickup", "deliver", "get_in_vehicle", "escape", "wait"]
        for t in valid:
            s = MissionStep(type=t, label="test")
            assert s.type == t

    def test_invalid_step_type_clamps_to_wait(self):
        # Clamp-don't-crash (matches location/weather/weapon handling): an unknown
        # step type degrades to a harmless 'wait' instead of failing the mission.
        s = MissionStep(type="fly_away", label="test")
        assert s.type == "wait"

    def test_label_stored(self):
        s = MissionStep(type="go_to", label="Head to the docks")
        assert s.label == "Head to the docks"


class TestRoleClamping:
    """Models confuse enum roles across entity types; schema clamps instead of crashing."""

    def test_ped_role_race_opponent_clamps_to_civilian(self):
        # The exact Llama failure: 'race_opponent' (a vehicle role) on a ped.
        p = MissionPed(description="rival driver", role="race_opponent")
        assert p.role == "civilian"

    def test_vehicle_role_unknown_clamps_to_parked(self):
        from src.mission_schema import MissionVehicle
        v = MissionVehicle(description="car", role="nonsense")
        assert v.role == "parked"

    def test_object_role_unknown_clamps_to_decoration(self):
        from src.mission_schema import MissionObject
        o = MissionObject(description="thing", role="weapon")
        assert o.role == "decoration"

    def test_audio_type_unknown_clamps_to_ambient(self):
        from src.mission_schema import MissionAudio
        a = MissionAudio(type="victory_fanfare", context="finish")
        assert a.type == "ambient"

    def test_valid_roles_pass_through(self):
        assert MissionPed(description="x", role="guard").role == "guard"


# ══════════════════════════════════════════════════════════════════════════════
# MissionPed
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionPed:
    def test_valid_ped(self):
        p = MissionPed(description="cop", role="guard", count=2, weapon="pistol")
        assert p.role == "guard"
        assert p.weapon == "pistol"

    def test_model_optional_defaults_none(self):
        p = MissionPed(description="cop", role="guard")
        assert p.model is None

    def test_count_defaults_to_one(self):
        p = MissionPed(description="cop", role="guard")
        assert p.count == 1

    def test_count_below_minimum_clamps_to_one(self):
        # Clamp-don't-crash: out-of-range counts are clamped into [1, 8].
        assert MissionPed(description="cop", role="guard", count=0).count == 1

    def test_count_above_maximum_clamps_to_max(self):
        assert MissionPed(description="cop", role="guard", count=9).count == 8

    def test_unknown_weapon_normalised_to_none(self):
        """Pydantic validator: unrecognised weapon clamps to 'none', not a raise."""
        p = MissionPed(description="cop", role="guard", weapon="bazooka")
        assert p.weapon == "none"

    def test_weapon_case_normalised(self):
        p = MissionPed(description="cop", role="guard", weapon="PISTOL")
        assert p.weapon == "pistol"

    def test_all_valid_weapons_accepted(self):
        for w in ("none", "pistol", "smg", "rifle", "shotgun", "sniper"):
            p = MissionPed(description="cop", role="guard", weapon=w)
            assert p.weapon == w

    def test_invalid_ped_role_clamps(self):
        # Clamp-don't-crash: unknown ped role → civilian.
        assert MissionPed(description="cop", role="sniper_class").role == "civilian"

    def test_all_valid_ped_roles_accepted(self):
        for role in ("guard", "patrol", "target", "civilian", "ally"):
            p = MissionPed(description="cop", role=role)
            assert p.role == role


# ══════════════════════════════════════════════════════════════════════════════
# MissionVehicle
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionVehicle:
    def test_valid_vehicle(self):
        v = MissionVehicle(description="getaway car", role="escape", count=1)
        assert v.role == "escape"

    def test_model_optional_defaults_none(self):
        v = MissionVehicle(description="van", role="escape")
        assert v.model is None

    def test_count_below_minimum_clamps_to_one(self):
        assert MissionVehicle(description="van", role="escape", count=0).count == 1

    def test_count_above_maximum_clamps_to_max(self):
        assert MissionVehicle(description="van", role="escape", count=5).count == 4

    def test_invalid_vehicle_role_clamps(self):
        assert MissionVehicle(description="van", role="getaway").role == "parked"

    def test_all_valid_vehicle_roles_accepted(self):
        for role in ("patrol", "escape", "delivery", "parked", "chase"):
            v = MissionVehicle(description="car", role=role)
            assert v.role == role


# ══════════════════════════════════════════════════════════════════════════════
# MissionObject
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionObject:
    def test_valid_object(self):
        o = MissionObject(description="drug package", role="pickup")
        assert o.role == "pickup"

    def test_model_optional_defaults_none(self):
        o = MissionObject(description="crate", role="obstacle")
        assert o.model is None

    def test_invalid_object_role_clamps(self):
        assert MissionObject(description="crate", role="weapon").role == "decoration"

    def test_all_valid_object_roles_accepted(self):
        for role in ("pickup", "obstacle", "decoration"):
            o = MissionObject(description="thing", role=role)
            assert o.role == role


# ══════════════════════════════════════════════════════════════════════════════
# MissionSchema — field validators
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionSchemaLocationValidator:
    def test_valid_location_accepted(self):
        s = MissionSchema(mission_name="X", location_name="airport", steps=[], peds=[], vehicles=[], objects=[])
        assert s.location_name == "airport"

    def test_location_uppercased_input_normalised(self):
        s = MissionSchema(mission_name="X", location_name="DOCKS", steps=[], peds=[], vehicles=[], objects=[])
        assert s.location_name == "docks"

    def test_location_spaces_replaced_with_underscores(self):
        s = MissionSchema(mission_name="X", location_name="sandy shores", steps=[], peds=[], vehicles=[], objects=[])
        assert s.location_name == "sandy_shores"

    def test_unknown_location_clamps_to_docks(self):
        s = MissionSchema(mission_name="X", location_name="moon_base", steps=[], peds=[], vehicles=[], objects=[])
        assert s.location_name == "docks"

    def test_all_valid_locations_accepted(self):
        valid = [
            "docks", "airport", "downtown", "vinewood", "sandy_shores",
            "paleto_bay", "alamo_sea", "military_base", "prison", "port", "highway",
        ]
        for loc in valid:
            s = MissionSchema(mission_name="X", location_name=loc, steps=[], peds=[], vehicles=[], objects=[])
            assert s.location_name == loc


class TestMissionSchemaWeatherValidator:
    def test_valid_weather_accepted(self):
        s = MissionSchema(mission_name="X", weather="RAIN", steps=[], peds=[], vehicles=[], objects=[])
        assert s.weather == "RAIN"

    def test_lowercase_weather_uppercased(self):
        s = MissionSchema(mission_name="X", weather="rain", steps=[], peds=[], vehicles=[], objects=[])
        assert s.weather == "RAIN"

    def test_unknown_weather_clamps_to_clear(self):
        s = MissionSchema(mission_name="X", weather="BLIZZARD", steps=[], peds=[], vehicles=[], objects=[])
        assert s.weather == "CLEAR"

    def test_all_valid_weathers_accepted(self):
        for w in ("CLEAR", "OVERCAST", "RAIN", "THUNDER", "FOGGY", "SMOG", "EXTRASUNNY"):
            s = MissionSchema(mission_name="X", weather=w, steps=[], peds=[], vehicles=[], objects=[])
            assert s.weather == w


class TestMissionSchemaAiDifficultyAndConfigMenu:
    """HeliRace-style opt-in polish: AI rival difficulty curve + F10 config menu."""

    def test_ai_difficulty_defaults_to_medium(self):
        s = MissionSchema(mission_name="X", steps=[], peds=[], vehicles=[], objects=[])
        assert s.ai_difficulty == "medium"

    def test_config_menu_defaults_to_false(self):
        s = MissionSchema(mission_name="X", steps=[], peds=[], vehicles=[], objects=[])
        assert s.config_menu is False

    def test_all_valid_ai_difficulties_accepted(self):
        for d in ("easy", "medium", "hard"):
            s = MissionSchema(mission_name="X", ai_difficulty=d, steps=[], peds=[], vehicles=[], objects=[])
            assert s.ai_difficulty == d

    def test_ai_difficulty_case_normalised(self):
        s = MissionSchema(mission_name="X", ai_difficulty="HARD", steps=[], peds=[], vehicles=[], objects=[])
        assert s.ai_difficulty == "hard"

    def test_unknown_ai_difficulty_clamps_to_medium(self):
        s = MissionSchema(mission_name="X", ai_difficulty="nightmare", steps=[], peds=[], vehicles=[], objects=[])
        assert s.ai_difficulty == "medium"

    def test_config_menu_true_accepted(self):
        s = MissionSchema(mission_name="X", config_menu=True, steps=[], peds=[], vehicles=[], objects=[])
        assert s.config_menu is True


class TestMissionSchemaTimeOfDay:
    def test_time_of_day_defaults_to_20(self):
        s = MissionSchema(mission_name="X", steps=[], peds=[], vehicles=[], objects=[])
        assert s.time_of_day == 20

    def test_time_of_day_below_min_clamps_to_zero(self):
        s = MissionSchema(mission_name="X", time_of_day=-1, steps=[], peds=[], vehicles=[], objects=[])
        assert s.time_of_day == 0

    def test_time_of_day_above_max_clamps_to_23(self):
        s = MissionSchema(mission_name="X", time_of_day=24, steps=[], peds=[], vehicles=[], objects=[])
        assert s.time_of_day == 23

    def test_boundary_values_accepted(self):
        for t in (0, 12, 23):
            s = MissionSchema(mission_name="X", time_of_day=t, steps=[], peds=[], vehicles=[], objects=[])
            assert s.time_of_day == t


class TestMissionSchemaListLimits:
    """Over-long lists are truncated (clamp-don't-crash), not rejected — so a chatty
    model that returns 8 steps still yields a valid 7-step mission."""

    def test_steps_truncated_to_7(self):
        steps = [MissionStep(type="go_to", label=f"Step {i}") for i in range(9)]
        s = MissionSchema(mission_name="X", steps=steps, peds=[], vehicles=[], objects=[])
        assert len(s.steps) == 7

    def test_peds_truncated_to_6(self):
        peds = [MissionPed(description="cop", role="guard") for _ in range(8)]
        s = MissionSchema(mission_name="X", steps=[], peds=peds, vehicles=[], objects=[])
        assert len(s.peds) == 6

    def test_vehicles_truncated_to_4(self):
        vehicles = [MissionVehicle(description="car", role="escape") for _ in range(6)]
        s = MissionSchema(mission_name="X", steps=[], peds=[], vehicles=vehicles, objects=[])
        assert len(s.vehicles) == 4

    def test_objects_truncated_to_6(self):
        objects = [MissionObject(description="box", role="pickup") for _ in range(9)]
        s = MissionSchema(mission_name="X", steps=[], peds=[], vehicles=[], objects=objects)
        assert len(s.objects) == 6


# ══════════════════════════════════════════════════════════════════════════════
# MissionSchema.unresolved_models()
# ══════════════════════════════════════════════════════════════════════════════

class TestUnresolvedModels:
    def test_all_resolved_returns_empty_list(self, gta_full_schema):
        assert gta_full_schema.unresolved_models() == []

    def test_ped_with_no_model_detected(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[MissionPed(description="armed guy", role="guard")],   # model=None
            vehicles=[],
            objects=[],
        )
        unresolved = s.unresolved_models()
        assert len(unresolved) == 1
        assert "ped:armed guy" in unresolved

    def test_ped_with_unresolved_string_detected(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[MissionPed(description="alien", model="UNRESOLVED", role="guard")],
            vehicles=[],
            objects=[],
        )
        assert "ped:alien" in s.unresolved_models()

    def test_vehicle_with_no_model_detected(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[],
            vehicles=[MissionVehicle(description="hovercraft", role="escape")],
            objects=[],
        )
        assert "vehicle:hovercraft" in s.unresolved_models()

    def test_object_with_no_model_detected(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[],
            vehicles=[],
            objects=[MissionObject(description="mystery box", role="pickup")],
        )
        assert "object:mystery box" in s.unresolved_models()

    def test_multiple_unresolved_all_reported(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[MissionPed(description="a", role="guard")],
            vehicles=[MissionVehicle(description="b", role="escape")],
            objects=[MissionObject(description="c", role="pickup")],
        )
        u = s.unresolved_models()
        assert len(u) == 3

    def test_mixed_resolved_and_unresolved(self):
        s = MissionSchema(
            mission_name="X",
            steps=[],
            peds=[
                MissionPed(description="cop", model="s_m_y_cop_01", role="guard"),
                MissionPed(description="alien", role="guard"),   # unresolved
            ],
            vehicles=[],
            objects=[],
        )
        u = s.unresolved_models()
        assert len(u) == 1
        assert "ped:alien" in u
