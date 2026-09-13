"""
Pydantic schema models for mission data.
Two stages:
  ConceptSchema  — after Step 3 (plain English descriptions, no model IDs)
  ResolvedSchema — after Step 4 (model IDs filled by RAG)
"""

from __future__ import annotations
import re
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

# ── Keybind-gated mid-mission actions (fire once per keypress, any time the
# mission is active — unlike MissionEvent's one-shot progress-fraction firing,
# and unlike the Mission Editor's proximity-volume `editor_triggers`, which
# fire on player distance, not a keystroke). ─────────────────────────────────
KeybindAction = Literal["teleport_player", "spawn_ped_group", "spawn_vehicle"]

# ── Minigame skeleton (menu-primary replayable arcade loop) ──────────────────
SkeletonKind = Literal["mission", "minigame"]
SettingKind  = Literal["cycle", "toggle", "int"]
ScoreMetric  = Literal["points", "kills", "seconds"]
RoundType    = Literal["timed", "survive_waves", "pursuit_survive", "drift_score", "theft_streak"]
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


_SETTING_KEY_RE = re.compile(r"[^a-z0-9_]+")


class MinigameSetting(BaseModel):
    """One row in a minigame's config menu — also becomes its .ini [Settings] key."""
    key:       str
    label:     str
    kind:      SettingKind = "int"
    options:   list[str] = Field(default_factory=list, max_length=8)   # cycle only
    min_value: int = 0                                                 # int only
    max_value: int = 100
    step:      int = 1
    default:   str = ""   # normalized by the model validator per kind

    @field_validator("kind", mode="before")
    @classmethod
    def _clamp_kind(cls, v: str) -> str:
        return clamp_enum(v, get_args(SettingKind), "int")

    @field_validator("key", mode="before")
    @classmethod
    def _sanitize_key(cls, v) -> str:
        cleaned = _SETTING_KEY_RE.sub("_", str(v).strip().lower()).strip("_")
        return cleaned or "setting"

    @model_validator(mode="after")
    def _normalize(self) -> "MinigameSetting":
        # A cycle row without at least 2 options can't cycle — degrade to toggle.
        if self.kind == "cycle" and len(self.options) < 2:
            self.kind = "toggle"
        if self.min_value > self.max_value:
            self.min_value, self.max_value = self.max_value, self.min_value
        self.step = max(1, int(self.step))
        if self.kind == "cycle":
            if self.default not in self.options:
                self.default = self.options[0]
        elif self.kind == "toggle":
            if str(self.default).lower() not in ("true", "false"):
                self.default = "true"
            else:
                self.default = str(self.default).lower()
        else:  # int
            self.default = str(_clamp_int(self.default, self.min_value,
                                          self.max_value, self.min_value))
        return self


class WaveRoundParams(BaseModel):
    """survive_waves round: arena horde escalation. Enemy model + weapon come
    from schema.peds (RAG-resolved via the normal Step-4 path), not from here."""
    wave_count:           int   = 5
    enemies_per_wave:     int   = 3
    wave_growth:          float = 1.5
    arena_radius:         float = 35.0
    intermission_seconds: int   = 8

    @field_validator("wave_count", "enemies_per_wave", "intermission_seconds", mode="before")
    @classmethod
    def _clamp_ints(cls, v, info):
        bounds = {"wave_count": (3, 10, 5), "enemies_per_wave": (2, 8, 3),
                  "intermission_seconds": (3, 30, 8)}
        lo, hi, default = bounds[info.field_name]
        return _clamp_int(v, lo, hi, default)

    @field_validator("wave_growth", mode="before")
    @classmethod
    def _clamp_growth(cls, v):
        return _clamp_float(v, 1.0, 3.0, 1.5)

    @field_validator("arena_radius", mode="before")
    @classmethod
    def _clamp_radius(cls, v):
        return _clamp_float(v, 20.0, 100.0, 35.0)


class PursuitRoundParams(BaseModel):
    """pursuit_survive round: outlast escalating police heat; score = seconds."""
    start_wanted:          int = 2
    escalation_interval_s: int = 45
    max_wanted:            int = 5
    spawn_heli_at_level:   int = 4    # 0 = never spawn the attack heli

    @field_validator("start_wanted", "escalation_interval_s", "max_wanted",
                     "spawn_heli_at_level", mode="before")
    @classmethod
    def _clamp_ints(cls, v, info):
        bounds = {"start_wanted": (1, 3, 2), "escalation_interval_s": (15, 120, 45),
                  "max_wanted": (3, 5, 5), "spawn_heli_at_level": (0, 5, 4)}
        lo, hi, default = bounds[info.field_name]
        return _clamp_int(v, lo, hi, default)

    @model_validator(mode="after")
    def _order(self) -> "PursuitRoundParams":
        if self.start_wanted > self.max_wanted:
            self.start_wanted = self.max_wanted
        return self


class DriftRoundParams(BaseModel):
    """drift_score round: chain drifts for points against the clock. Drift
    state (angle/speed check, combo decay) is computed live from the player's
    current vehicle — no extra peds/vehicles required."""
    time_limit_s:    int   = 90
    min_drift_angle: float = 15.0   # degrees between heading and velocity
    min_drift_speed: float = 8.0    # m/s
    combo_decay_s:   float = 1.5    # grace window before a combo resets

    @field_validator("time_limit_s", mode="before")
    @classmethod
    def _clamp_time(cls, v):
        return _clamp_int(v, 30, 300, 90)

    @field_validator("min_drift_angle", mode="before")
    @classmethod
    def _clamp_angle(cls, v):
        return _clamp_float(v, 5.0, 60.0, 15.0)

    @field_validator("min_drift_speed", mode="before")
    @classmethod
    def _clamp_speed(cls, v):
        return _clamp_float(v, 3.0, 25.0, 8.0)

    @field_validator("combo_decay_s", mode="before")
    @classmethod
    def _clamp_decay(cls, v):
        return _clamp_float(v, 0.3, 5.0, 1.5)


class TheftRoundParams(BaseModel):
    """theft_streak round: jack any vehicle and drive it to the personal
    garage (a fixed point near the mission location) to secure it. One point
    per car secured; a secured vehicle can't be re-scored. No peds/vehicles
    required — the target is always whatever the player is currently driving."""
    time_limit_s:   int   = 120
    garage_radius:  float = 8.0     # metres from the garage point to count as delivered

    @field_validator("time_limit_s", mode="before")
    @classmethod
    def _clamp_time(cls, v):
        return _clamp_int(v, 60, 300, 120)

    @field_validator("garage_radius", mode="before")
    @classmethod
    def _clamp_radius(cls, v):
        return _clamp_float(v, 4.0, 20.0, 8.0)


class MinigameSpec(BaseModel):
    """Replayable-arcade configuration: menu rows, scoring, and the round body."""
    settings:     list[MinigameSetting] = Field(default_factory=list, max_length=8)
    score_metric: ScoreMetric = "points"
    score_label:  str = "SCORE"
    round_type:   RoundType = "timed"
    round_params: dict = Field(default_factory=dict)

    @field_validator("score_metric", mode="before")
    @classmethod
    def _clamp_metric(cls, v: str) -> str:
        return clamp_enum(v, get_args(ScoreMetric), "points")

    @field_validator("round_type", mode="before")
    @classmethod
    def _clamp_round(cls, v: str) -> str:
        return clamp_enum(v, get_args(RoundType), "timed")

    @model_validator(mode="after")
    def _normalize(self) -> "MinigameSpec":
        # Dedupe setting keys (keep first) — keys become ini entries and must be unique.
        seen: set[str] = set()
        unique: list[MinigameSetting] = []
        for s in self.settings:
            if s.key not in seen:
                seen.add(s.key)
                unique.append(s)
        self.settings = unique
        if self.round_type == "timed":
            secs = self.round_params.get("seconds", 60)
            self.round_params["seconds"] = _clamp_int(secs, 10, 600, 60)
        elif self.round_type == "survive_waves":
            self.round_params = WaveRoundParams.model_validate(
                self.round_params or {}).model_dump()
        elif self.round_type == "pursuit_survive":
            self.round_params = PursuitRoundParams.model_validate(
                self.round_params or {}).model_dump()
        elif self.round_type == "drift_score":
            self.round_params = DriftRoundParams.model_validate(
                self.round_params or {}).model_dump()
        elif self.round_type == "theft_streak":
            self.round_params = TheftRoundParams.model_validate(
                self.round_params or {}).model_dump()
        return self


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


class TeleportPoint(BaseModel):
    """Named world position a keybind can move the player to.

    Never carries raw world coordinates — the LLM has no way to know real
    GTA5 map coordinates and would hallucinate them. Instead it names one of
    the same closed location vocabulary already used for the mission's own
    location_name, plus a small offset — the exact convention
    code_generator.py already uses for waypoints/checkpoints (anchor + a
    bounded compass offset), never an invented absolute position.
    """
    name:          str
    location_name: str   = "docks"
    offset_x:      float = 0.0
    offset_y:      float = 0.0

    @field_validator("location_name", mode="before")
    @classmethod
    def _clamp_location(cls, v: str) -> str:
        normalised = str(v).strip().lower().replace(" ", "_")
        return normalised if normalised in _VALID_LOCATIONS else "docks"

    @field_validator("offset_x", "offset_y", mode="before")
    @classmethod
    def _clamp_offset(cls, v) -> float:
        return _clamp_float(v, -300.0, 300.0, 0.0)


_KEYBIND_KEY_RE: re.Pattern = re.compile(r"^F([1-9]|1[0-2])$")
# F5 starts the mission, F10 toggles the config menu — a keybind claiming
# either would silently steal an already-meaningful key.
_RESERVED_KEYBIND_KEYS: frozenset[str] = frozenset({"F5", "F10"})


class MissionKeybind(BaseModel):
    """Keystroke-gated action, active any time the mission is running.

    `target` names the entity the action acts on, matched by exact
    description text against the schema's own peds[]/vehicles[]
    (spawn_ped_group/spawn_vehicle) or teleport_points[] (teleport_player) —
    reuses those declarations rather than duplicating spawn config here.
    Resolving `target` and rejecting duplicate/reserved keys happens in
    MissionSchema._validate_keybinds (loud abstain, never a silent guess).
    """
    key:    str = "F6"
    action: KeybindAction
    target: str = ""
    label:  str = ""   # short player-facing text (e.g. "Call backup")

    @field_validator("key", mode="before")
    @classmethod
    def _clamp_key(cls, v: str) -> str:
        key = str(v).strip().upper()
        return key if _KEYBIND_KEY_RE.match(key) else "F6"

    @field_validator("action", mode="before")
    @classmethod
    def _clamp_action(cls, v: str) -> str:
        return clamp_enum(v, get_args(KeybindAction), "spawn_ped_group")


_VALID_WEAPONS: frozenset[str] = frozenset(get_args(WeaponEnum))


class MissionPed(BaseModel):
    description: str
    model:       Optional[str] = None   # filled by RAG
    role:        PedRole
    count:       int = Field(default=1, ge=1, le=8)
    weapon:      WeaponEnum = "none"
    ammo:        int = 100   # matches GIVE_WEAPON_TO_PED's historical hardcoded value
    health:      float = 100.0
    armor:       float = 0.0

    @field_validator("weapon", mode="before")
    @classmethod
    def normalise_weapon(cls, v: str) -> str:
        """Clamp any unrecognised weapon string to 'none' instead of raising."""
        w = str(v).lower().strip()
        return w if w in _VALID_WEAPONS else "none"

    @field_validator("ammo", mode="before")
    @classmethod
    def _clamp_ammo(cls, v) -> int:
        return _clamp_int(v, 1, 9999, 100)

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

    @field_validator("health", mode="before")
    @classmethod
    def _clamp_health(cls, v) -> float:
        return _clamp_float(v, 1.0, 1000.0, 100.0)

    @field_validator("armor", mode="before")
    @classmethod
    def _clamp_armor(cls, v) -> float:
        return _clamp_float(v, 0.0, 100.0, 0.0)


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

    # ── Keybind-gated actions (teleport / on-demand spawn) ──
    #   teleport_points -> named locations a keybind can move the player to
    #   keybinds        -> F1-F12-gated actions, active any time the mission
    #                      is running; each references an existing peds[]/
    #                      vehicles[]/teleport_points[] entry by description
    teleport_points: list[TeleportPoint]  = Field(default_factory=list, max_length=4)
    keybinds:        list[MissionKeybind] = Field(default_factory=list, max_length=4)

    # ── Minigame skeleton (menu-primary replayable arcade loop) ──
    #   skeleton   -> "mission" = classic blip→F5→steps; "minigame" = menu→round→results loop
    #   minigame   -> config-menu rows + scoring + round body (only when skeleton="minigame")
    skeleton:      str = "mission"
    minigame:      Optional[MinigameSpec] = None

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

    @field_validator("steps", "peds", "vehicles", "objects", "events",
                     "teleport_points", "keybinds", mode="before")
    @classmethod
    def _truncate_lists(cls, v, info):
        # Models sometimes return more entries than the schema caps allow; truncate
        # (keep the first N) rather than raising a 'too_long' error. Caps mirror the
        # Field(max_length=...) declarations below.
        caps = {"steps": 7, "peds": 6, "vehicles": 4, "objects": 6, "events": 3,
                "teleport_points": 4, "keybinds": 4}
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

    @field_validator("skeleton", mode="before")
    @classmethod
    def _clamp_skeleton(cls, v) -> str:
        return clamp_enum(str(v), get_args(SkeletonKind), "mission")

    @model_validator(mode="after")
    def _normalize_skeleton(self) -> "MissionSchema":
        # Minigame skeleton always has a spec (defaults if the LLM omitted it);
        # mission skeleton never carries one — keeps the classic path byte-identical.
        if self.skeleton == "minigame" and self.minigame is None:
            self.minigame = MinigameSpec()
        elif self.skeleton == "mission":
            self.minigame = None
        return self

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

    @model_validator(mode="after")
    def _validate_keybinds(self) -> "MissionSchema":
        """Abstain loudly on a broken keybind rather than guessing.

        A keybind is dropped (never silently rewritten) when its key is
        reserved/already claimed by another keybind, or its target doesn't
        match a declared ped/vehicle/teleport_point description — the rest
        of the schema stays valid either way. Every drop is named in
        meta['keybind_warnings'], mirroring how unity_scenebind.py logs
        uncovered entities instead of silently falling through.
        """
        if not self.keybinds:
            return self

        ped_names        = {p.description.strip().lower() for p in self.peds}
        vehicle_names     = {v.description.strip().lower() for v in self.vehicles}
        teleport_names    = {t.name.strip().lower() for t in self.teleport_points}
        used_keys         = set(_RESERVED_KEYBIND_KEYS)
        warnings: list[str] = []
        kept: list[MissionKeybind] = []

        for kb in self.keybinds:
            if kb.key in used_keys:
                warnings.append(
                    f"keybind {kb.key} ({kb.action}, target={kb.target!r}) dropped — "
                    f"key already bound (or reserved for start/config-menu)")
                continue
            target = kb.target.strip().lower()
            if kb.action == "teleport_player":
                if target not in teleport_names:
                    warnings.append(
                        f"keybind {kb.key} (teleport_player) dropped — no teleport_points "
                        f"entry named {kb.target!r}")
                    continue
            elif kb.action == "spawn_ped_group":
                if target not in ped_names:
                    warnings.append(
                        f"keybind {kb.key} (spawn_ped_group) dropped — no peds[] entry "
                        f"described {kb.target!r}")
                    continue
            elif kb.action == "spawn_vehicle":
                if target not in vehicle_names:
                    warnings.append(
                        f"keybind {kb.key} (spawn_vehicle) dropped — no vehicles[] entry "
                        f"described {kb.target!r}")
                    continue
            used_keys.add(kb.key)
            kept.append(kb)

        self.keybinds = kept
        if warnings:
            self.meta.setdefault("keybind_warnings", []).extend(warnings)
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
