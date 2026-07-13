"""
Pydantic schema models for mission data.
Two stages:
  ConceptSchema  — after Step 3 (plain English descriptions, no model IDs)
  ResolvedSchema — after Step 4 (model IDs filled by RAG)
"""

from __future__ import annotations
from typing import Literal, Optional, get_args
from pydantic import BaseModel, Field, field_validator, model_validator

# Shared code-fragment primitives — the same mechanism the Unity micro
# generators use, reused here so the LLM can author mission-specific C#.
from .micro.code_fields import CodeField, CodeMethod, clamp_enum


StepType    = Literal[
    # existing — keep order for backward-compat with saved .schema.json files
    "go_to", "kill_all", "pickup", "deliver", "get_in_vehicle", "escape", "wait",
    # racing / timed
    "checkpoint", "race_lap", "timer",
    # any-order collection (HeliRace-derived pattern)
    "scavenger",
]
EventType   = Literal["thunderstorm", "police_pursuit"]
PedRole     = Literal["guard", "patrol", "target", "civilian", "ally"]
VehicleRole = Literal["patrol", "escape", "delivery", "parked", "chase", "race_opponent"]
ObjectRole  = Literal["pickup", "obstacle", "decoration"]
AudioType   = Literal["ambient", "mission_passed", "mission_failed", "sting"]
WeaponEnum  = Literal["none", "pistol", "smg", "rifle", "shotgun", "sniper"]

LocationName = Literal[
    "docks", "airport", "downtown", "vinewood", "sandy_shores",
    "paleto_bay", "alamo_sea", "military_base", "prison", "port", "highway"
]
WeatherName = Literal["CLEAR", "OVERCAST", "RAIN", "THUNDER", "FOGGY", "SMOG", "EXTRASUNNY"]
AiDifficulty = Literal["easy", "medium", "hard"]


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    """Coerce to int and clamp into [lo, hi]; fall back to default on garbage.
    Clamp-don't-crash for the schema's bounded counts (matches the enum clamps)."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_float(v, lo: float, hi: float, default: float) -> float:
    """Float twin of _clamp_int — same clamp-don't-crash contract."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


class MissionStep(BaseModel):
    type:   StepType
    label:  str
    params: dict = Field(default_factory=dict)   # type-specific config; backward-compat default

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v: str) -> str:
        # Loose models emit unknown/aliased step types; clamp to a harmless
        # 'wait' rather than crashing the whole mission (mirrors the micro layer).
        return clamp_enum(v, get_args(StepType), "wait")


class MissionEvent(BaseModel):
    """One-shot mid-mission event fired when mission progress crosses at_progress."""
    event_type:  EventType
    at_progress: float = Field(default=0.5, ge=0.0, le=1.0)
    params:      dict  = Field(default_factory=dict)   # type-specific config

    @field_validator("event_type", mode="before")
    @classmethod
    def _clamp_event_type(cls, v: str) -> str:
        return clamp_enum(v, get_args(EventType), "thunderstorm")

    @field_validator("at_progress", mode="before")
    @classmethod
    def _clamp_at_progress(cls, v) -> float:
        return _clamp_float(v, 0.0, 1.0, 0.5)


_VALID_WEAPONS: frozenset[str] = frozenset(get_args(WeaponEnum))


class MissionPed(BaseModel):
    description: str
    model:       Optional[str] = None   # filled by RAG
    role:        PedRole
    count:       int = Field(default=1, ge=1, le=8)
    weapon:      WeaponEnum = "none"

    @field_validator("weapon", mode="before")
    @classmethod
    def normalise_weapon(cls, v: str) -> str:
        """Clamp any unrecognised weapon string to 'none' instead of raising."""
        w = str(v).lower().strip()
        return w if w in _VALID_WEAPONS else "none"

    @field_validator("role", mode="before")
    @classmethod
    def _clamp_role(cls, v: str) -> str:
        # Models confuse ped/vehicle roles (e.g. 'race_opponent' on a ped) — clamp
        # to a harmless bystander rather than crashing the whole generation.
        return clamp_enum(v, get_args(PedRole), "civilian")

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        return _clamp_int(v, 1, 8, 1)


class MissionVehicle(BaseModel):
    description: str
    model:       Optional[str] = None   # filled by RAG
    role:        VehicleRole
    count:       int = Field(default=1, ge=1, le=4)

    @field_validator("role", mode="before")
    @classmethod
    def _clamp_role(cls, v: str) -> str:
        return clamp_enum(v, get_args(VehicleRole), "parked")

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        return _clamp_int(v, 1, 4, 1)


class MissionObject(BaseModel):
    description: str
    model:       Optional[str] = None   # filled by RAG
    role:        ObjectRole

    @field_validator("role", mode="before")
    @classmethod
    def _clamp_role(cls, v: str) -> str:
        return clamp_enum(v, get_args(ObjectRole), "decoration")


class MissionAudio(BaseModel):
    type:    AudioType
    context: str
    name:    Optional[str] = None       # filled by RAG/lookup

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v: str) -> str:
        return clamp_enum(v, get_args(AudioType), "ambient")


_VALID_LOCATIONS: frozenset[str] = frozenset(get_args(LocationName))
_VALID_WEATHER:   frozenset[str] = frozenset(get_args(WeatherName))


class MissionSchema(BaseModel):
    mission_name:  str
    location_name: str = "docks"
    time_of_day:   int = Field(default=20, ge=0, le=23)
    weather:       str = "CLEAR"
    steps:         list[MissionStep]    = Field(default_factory=list, max_length=7)
    peds:          list[MissionPed]     = Field(default_factory=list, max_length=6)
    vehicles:      list[MissionVehicle] = Field(default_factory=list, max_length=4)
    objects:       list[MissionObject]  = Field(default_factory=list, max_length=6)
    audio:         list[MissionAudio]   = Field(default_factory=list)
    meta:          dict                  = Field(default_factory=dict)   # theme/seed bookkeeping

    # ── HeliRace-style polish (opt-in, deterministic — see race_opponent vehicles) ──
    #   ai_difficulty  -> speed curve for any race_opponent driver (SpawnVehicles/UpdateRivals)
    #   config_menu    -> emit an F10 native DrawRect/DrawText settings panel
    #                     (weather + AI difficulty, only shown before the mission starts)
    #   events         -> one-shot mid-mission twists fired at N% progress
    #   hud            -> native DrawRect/DrawText mission HUD (timer + progress bar)
    #   reward_cash    -> cash paid on Complete() (SP total-cash stat, money fallback)
    ai_difficulty: str  = "medium"
    config_menu:   bool = False
    events:        list[MissionEvent] = Field(default_factory=list, max_length=3)
    hud:           bool = False
    reward_cash:   int  = 0

    # ── LLM-authored mission-specific C# (insertion points) ──
    # Frozen into the schema, so Step 6 stays a pure function — determinism intact.
    #   extra_init  -> end of Init()   (runs once when the mission starts)
    #   extra_tick  -> end of OnTick() (runs every frame)
    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_init:    str              = ""
    extra_tick:    str              = ""
    extra_methods: list[CodeMethod] = Field(default_factory=list)

    @field_validator("location_name", mode="before")
    @classmethod
    def normalise_location(cls, v: str) -> str:
        """Accept any casing/spacing; clamp unknown values to 'docks'."""
        normalised = str(v).strip().lower().replace(" ", "_")
        return normalised if normalised in _VALID_LOCATIONS else "docks"

    @field_validator("time_of_day", mode="before")
    @classmethod
    def _clamp_time(cls, v) -> int:
        return _clamp_int(v, 0, 23, 20)

    @field_validator("steps", "peds", "vehicles", "objects", "events", mode="before")
    @classmethod
    def _truncate_lists(cls, v, info):
        # Models sometimes return more entries than the schema caps allow; truncate
        # (keep the first N) rather than raising a 'too_long' error. Caps mirror the
        # Field(max_length=...) declarations below.
        caps = {"steps": 7, "peds": 6, "vehicles": 4, "objects": 6, "events": 3}
        cap = caps.get(info.field_name)
        if cap is not None and isinstance(v, list) and len(v) > cap:
            return v[:cap]
        return v

    @field_validator("weather", mode="before")
    @classmethod
    def normalise_weather(cls, v: str) -> str:
        """Upper-case and clamp unknown weather to CLEAR."""
        up = str(v).strip().upper()
        return up if up in _VALID_WEATHER else "CLEAR"

    @field_validator("ai_difficulty", mode="before")
    @classmethod
    def _clamp_ai_difficulty(cls, v) -> str:
        return clamp_enum(str(v), get_args(AiDifficulty), "medium")

    @field_validator("reward_cash", mode="before")
    @classmethod
    def _clamp_reward_cash(cls, v) -> int:
        return _clamp_int(v, 0, 1_000_000, 0)

    @model_validator(mode="after")
    def _default_step_params(self) -> "MissionSchema":
        """Silently apply sensible defaults for new step type params.
        Existing step types are untouched (their params remain {})."""
        for step in self.steps:
            if step.type == "race_lap":
                step.params.setdefault("lap_count", 3)
                step.params.setdefault("checkpoint_count", 6)
                step.params.setdefault("track", "")
                step.params.setdefault("require_vehicle", True)
            elif step.type == "checkpoint":
                step.params.setdefault("require_vehicle", False)
                step.params.setdefault("track", "")
            elif step.type == "timer":
                step.params.setdefault("seconds", 120)
            elif step.type == "scavenger":
                step.params.setdefault("target_count", 5)
                step.params.setdefault("radius", 3.0)
        for evt in self.events:
            if evt.event_type == "thunderstorm":
                evt.params.setdefault("wind_speed", 40.0)
            elif evt.event_type == "police_pursuit":
                evt.params.setdefault("wanted_level", 3)
                evt.params.setdefault("spawn_heli", False)
        return self

    def unresolved_models(self) -> list[str]:
        """Return a list of descriptions still missing model IDs."""
        missing = []
        for p in self.peds:
            if not p.model or p.model == "UNRESOLVED":
                missing.append(f"ped:{p.description}")
        for v in self.vehicles:
            if not v.model or v.model == "UNRESOLVED":
                missing.append(f"vehicle:{v.description}")
        for o in self.objects:
            if not o.model or o.model == "UNRESOLVED":
                missing.append(f"object:{o.description}")
        return missing
