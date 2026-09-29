"""Deterministic dungeon greybox IR and spatial authority.

The greybox is a post-schema geometry contract.  It is deliberately separate
from ``UnitySchema``'s creative fields: a seed and a small numeric config are
enough to reproduce the same room graph, grid coordinates, corridors and
markers without another model call.

The generated Unity editor script mirrors this contract for ProBuilder.  The
Python representation is the acceptance and sidecar authority; the editor
script is the geometry emitter.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import deque
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from pydantic import (BaseModel, Field, ValidationError, field_validator,
                      model_validator)


GREYBOX_FORMAT = "game-o-matic/unity-dungeon-greybox/v1"
GREYBOX_PLUS_FORMAT = "game-o-matic/unity-greybox-plus/v1"
GREYBOX_SCRIPT_TYPES = frozenset({"level_generator", "greybox_level_generator"})
GREYBOX_HIERARCHY: dict[str, Any] = {
    "root": "Dungeon_Greybox",
    "children": {
        "Dungeon_Greybox": [
            "Geometry", "Rooms", "Corridors", "Elevation", "Markers"
        ],
        "Geometry": [],
        "Rooms": [],
        "Corridors": [],
        "Elevation": [],
        "Markers": [],
    },
}

# Greybox+ is a versioned image-to-primitives planning contract. It lives
# alongside the v1 dungeon hierarchy until Slice 2 teaches the Unity emitter to
# realize these Macro/Meso/Micro paths directly. This avoids a silent root
# rename in existing generated projects.
GREYBOX_PLUS_HIERARCHY: dict[str, Any] = {
    "root": "GreyboxPlus_Level",
    "children": {
        "GreyboxPlus_Level": ["Geometry", "Markers"],
        "Geometry": ["Macro", "Meso", "Micro"],
        "Geometry/Macro": ["Buildings", "TerrainBlocks", "Elevation"],
        "Geometry/Meso": ["Pipes", "Tanks", "Machinery", "Catwalks"],
        "Geometry/Micro": ["Cover", "Blockers", "Railings"],
        "Markers": ["Spawns", "AI_Nodes", "CameraVolumes"],
    },
}


class GenerationMode(str, Enum):
    GEOMETRY_ONLY = "geometry_only"
    FULL = "full"
    # C#-style aliases keep the Python IR pleasant to use beside the generated
    # editor API without changing the serialized values.
    GeometryOnly = "geometry_only"
    Full = "full"


class GreyboxPlusTier(str, Enum):
    MACRO = "macro"
    MESO = "meso"
    MICRO = "micro"


class GreyboxPlusCategory(str, Enum):
    BUILDINGS = "buildings"
    TERRAIN_BLOCKS = "terrain_blocks"
    ELEVATION = "elevation"
    PIPES = "pipes"
    TANKS = "tanks"
    MACHINERY = "machinery"
    CATWALKS = "catwalks"
    COVER = "cover"
    BLOCKERS = "blockers"
    RAILINGS = "railings"


GREYBOX_PLUS_ROLE_PATHS: dict[GreyboxPlusCategory, tuple[GreyboxPlusTier, str]] = {
    GreyboxPlusCategory.BUILDINGS: (GreyboxPlusTier.MACRO, "Geometry/Macro/Buildings"),
    GreyboxPlusCategory.TERRAIN_BLOCKS: (GreyboxPlusTier.MACRO, "Geometry/Macro/TerrainBlocks"),
    GreyboxPlusCategory.ELEVATION: (GreyboxPlusTier.MACRO, "Geometry/Macro/Elevation"),
    GreyboxPlusCategory.PIPES: (GreyboxPlusTier.MESO, "Geometry/Meso/Pipes"),
    GreyboxPlusCategory.TANKS: (GreyboxPlusTier.MESO, "Geometry/Meso/Tanks"),
    GreyboxPlusCategory.MACHINERY: (GreyboxPlusTier.MESO, "Geometry/Meso/Machinery"),
    GreyboxPlusCategory.CATWALKS: (GreyboxPlusTier.MESO, "Geometry/Meso/Catwalks"),
    GreyboxPlusCategory.COVER: (GreyboxPlusTier.MICRO, "Geometry/Micro/Cover"),
    GreyboxPlusCategory.BLOCKERS: (GreyboxPlusTier.MICRO, "Geometry/Micro/Blockers"),
    GreyboxPlusCategory.RAILINGS: (GreyboxPlusTier.MICRO, "Geometry/Micro/Railings"),
}


class GridPoint(BaseModel):
    """An integer grid coordinate.  World coordinates are never authored."""

    x: int
    y: int = 0
    z: int

    @model_validator(mode="before")
    @classmethod
    def _from_sequence(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) != 3:
                raise ValueError("grid point must have exactly three coordinates")
            return {"x": value[0], "y": value[1], "z": value[2]}
        return value


class GridBounds(BaseModel):
    """Half-open integer bounds: [min, max) on each axis."""

    min_x: int
    min_y: int
    min_z: int
    max_x: int
    max_y: int
    max_z: int

    @model_validator(mode="after")
    def _positive(self) -> "GridBounds":
        if self.max_x <= self.min_x or self.max_y <= self.min_y or self.max_z <= self.min_z:
            raise ValueError("greybox bounds must have positive size")
        return self

    @property
    def width(self) -> int:
        return self.max_x - self.min_x

    @property
    def height(self) -> int:
        return self.max_y - self.min_y

    @property
    def depth(self) -> int:
        return self.max_z - self.min_z

    def overlaps(self, other: "GridBounds") -> bool:
        return (
            self.min_x < other.max_x and self.max_x > other.min_x
            and self.min_y < other.max_y and self.max_y > other.min_y
            and self.min_z < other.max_z and self.max_z > other.min_z
        )

    def contains(self, point: GridPoint) -> bool:
        return (
            self.min_x <= point.x < self.max_x
            and self.min_y <= point.y < self.max_y
            and self.min_z <= point.z < self.max_z
        )


class GridSegmentIR(BaseModel):
    start: GridPoint
    end: GridPoint


class ConnectorIR(BaseModel):
    id: str
    grid_position: GridPoint
    direction: str = "north"
    corridor_id: str = ""


class RoomIR(BaseModel):
    id: str
    type: str = "mid"
    bounds: GridBounds
    grid_position: GridPoint
    world_position: list[float]
    width: int
    height: int
    depth: int
    elevation: int = 0
    connectors: list[ConnectorIR] = Field(default_factory=list)
    optional: bool = False
    is_branch: bool = False
    is_loop: bool = False

    @field_validator("world_position", mode="before")
    @classmethod
    def _world_vector(cls, value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("room world_position must be [x, y, z]")
        return [float(value[0]), float(value[1]), float(value[2])]


class CorridorIR(BaseModel):
    id: str
    from_room_id: str
    to_room_id: str
    path: list[GridSegmentIR]
    width: int
    elevation: int = 0
    elevation_deltas: list[int] = Field(default_factory=list)
    is_branch: bool = False
    is_loop: bool = False


class ElevationChangeIR(BaseModel):
    id: str
    type: str = "stairs"
    path: list[GridSegmentIR]
    width: int
    from_elevation: int
    to_elevation: int


class MarkerIR(BaseModel):
    id: str
    type: str
    room_id: str = ""
    grid_position: GridPoint
    world_position: list[float]


class UnityPrimitiveIR(BaseModel):
    id: str
    primitive_type: str = "cube"
    parent: str = "Geometry"
    grid_position: GridPoint
    world_position: list[float]
    size: list[float]
    rotation: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class HierarchyIR(BaseModel):
    root: str = "Dungeon_Greybox"
    children: dict[str, list[str]] = Field(
        default_factory=lambda: json.loads(json.dumps(GREYBOX_HIERARCHY["children"]))
    )

    @model_validator(mode="after")
    def _required_children(self) -> "HierarchyIR":
        required = set(GREYBOX_HIERARCHY["children"]["Dungeon_Greybox"])
        actual = set(self.children.get(self.root, []))
        missing = sorted(required - actual)
        if missing:
            raise ValueError("greybox hierarchy is missing: " + ", ".join(missing))
        return self


class GreyboxConstraints(BaseModel):
    min_rooms: int = 4
    max_rooms: int = 8
    min_loops: int = 0
    max_loops: int = 1
    max_branching: int = 3
    min_corridor_width: int = 1
    max_corridor_width: int = 4
    min_room_width: int = 4
    max_room_width: int = 12
    min_room_depth: int = 4
    max_room_depth: int = 12
    min_boss_depth: int = 3
    max_boss_depth: int = 8
    loop_frequency: float = 0.35
    elevation_frequency: float = 0.0

    @field_validator(
        "min_rooms", "max_rooms", "min_loops", "max_loops", "max_branching",
        "min_corridor_width", "max_corridor_width", "min_room_width",
        "max_room_width", "min_room_depth", "max_room_depth", "min_boss_depth",
        "max_boss_depth", mode="before"
    )
    @classmethod
    def _integer(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @field_validator("loop_frequency", "elevation_frequency", mode="before")
    @classmethod
    def _frequency(cls, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @model_validator(mode="after")
    def _coherent_ranges(self) -> "GreyboxConstraints":
        if self.max_rooms < self.min_rooms:
            raise ValueError("greybox max_rooms must be >= min_rooms")
        if self.max_loops < self.min_loops:
            raise ValueError("greybox max_loops must be >= min_loops")
        if self.max_room_width < self.min_room_width:
            raise ValueError("greybox max_room_width must be >= min_room_width")
        if self.max_room_depth < self.min_room_depth:
            raise ValueError("greybox max_room_depth must be >= min_room_depth")
        if self.max_corridor_width < self.min_corridor_width:
            raise ValueError("greybox max_corridor_width must be >= min_corridor_width")
        if self.max_branching < 2:
            raise ValueError("greybox max_branching must be at least 2")
        return self


class DungeonGreyboxIR(BaseModel):
    format: str = GREYBOX_FORMAT
    version: int = 1
    seed: int
    mode: GenerationMode = GenerationMode.GEOMETRY_ONLY
    cell_size: float = 2.0
    theme: str = "none"
    twist_tags: list[str] = Field(default_factory=list)
    biome_id: str = ""
    rooms: list[RoomIR]
    corridors: list[CorridorIR]
    elevation_changes: list[ElevationChangeIR] = Field(default_factory=list)
    unity_primitives: list[UnityPrimitiveIR] = Field(default_factory=list)
    markers: list[MarkerIR] = Field(default_factory=list)
    hierarchy: HierarchyIR = Field(default_factory=HierarchyIR)
    adjacency: dict[str, list[str]] = Field(default_factory=dict)
    start_room_id: str
    boss_room_id: str
    boss_depth: int
    loop_count: int = 0
    branching_factors: dict[str, int] = Field(default_factory=dict)
    constraints: GreyboxConstraints = Field(default_factory=GreyboxConstraints)
    accepted: bool = False

    @model_validator(mode="after")
    def _geometry_only_theme(self) -> "DungeonGreyboxIR":
        if self.mode == GenerationMode.GEOMETRY_ONLY:
            self.theme = "none"
            self.twist_tags = []
            self.biome_id = ""
        if self.cell_size <= 0.0 or not math.isfinite(self.cell_size):
            raise ValueError("greybox cell_size must be finite and positive")
        return self


class GreyboxPlusReferenceIR(BaseModel):
    """Immutable provenance for the visual evidence consumed before Step 4."""

    image_name: str
    image_sha256: str
    visual_context_sha256: str

    @field_validator("image_name")
    @classmethod
    def _image_name(cls, value: str) -> str:
        value = str(value).strip()
        if not value:
            raise ValueError("Greybox+ evidence must name its reference image")
        return value

    @field_validator("image_sha256", "visual_context_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        value = str(value).lower()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("Greybox+ evidence digests must be lowercase SHA-256 values")
        return value


class GreyboxPlusPrimitiveIR(BaseModel):
    """A role-tagged, exact-transform view of one dungeon primitive."""

    id: str
    source_primitive_id: str
    tier: GreyboxPlusTier
    category: GreyboxPlusCategory
    hierarchy_path: str
    grid_position: GridPoint
    world_position: list[float]
    size: list[float]
    rotation: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])

    @model_validator(mode="after")
    def _role_path_matches_category(self) -> "GreyboxPlusPrimitiveIR":
        tier, hierarchy_path = GREYBOX_PLUS_ROLE_PATHS[self.category]
        if self.tier != tier or self.hierarchy_path != hierarchy_path:
            raise ValueError(
                f"Greybox+ primitive {self.id!r} has an invalid role hierarchy"
            )
        return self


class GreyboxPlusHierarchyIR(BaseModel):
    root: str = "GreyboxPlus_Level"
    children: dict[str, list[str]] = Field(
        default_factory=lambda: json.loads(json.dumps(GREYBOX_PLUS_HIERARCHY["children"]))
    )

    @model_validator(mode="after")
    def _complete_hierarchy(self) -> "GreyboxPlusHierarchyIR":
        if self.root != GREYBOX_PLUS_HIERARCHY["root"]:
            raise ValueError("Greybox+ root must be GreyboxPlus_Level")
        if self.children != GREYBOX_PLUS_HIERARCHY["children"]:
            raise ValueError("Greybox+ hierarchy must match the canonical role tree")
        return self


class GreyboxPlusIR(BaseModel):
    """Versioned Greybox+ plan; its embedded dungeon IR remains spatial truth."""

    format: str = GREYBOX_PLUS_FORMAT
    version: int = 1
    mode: GenerationMode = GenerationMode.GEOMETRY_ONLY
    theme: str = "none"
    reference: GreyboxPlusReferenceIR
    source_greybox_sha256: str
    dungeon: DungeonGreyboxIR
    hierarchy: GreyboxPlusHierarchyIR = Field(default_factory=GreyboxPlusHierarchyIR)
    primitives: list[GreyboxPlusPrimitiveIR] = Field(default_factory=list)
    accepted: bool = True

    @model_validator(mode="after")
    def _geometry_only_contract(self) -> "GreyboxPlusIR":
        if self.format != GREYBOX_PLUS_FORMAT or self.version != 1:
            raise ValueError("unsupported Greybox+ IR version")
        if self.mode != GenerationMode.GEOMETRY_ONLY or self.theme != "none":
            raise ValueError("Greybox+ is geometry-only and must use theme=none")
        if (len(self.source_greybox_sha256) != 64
                or any(char not in "0123456789abcdef"
                       for char in self.source_greybox_sha256)):
            raise ValueError("Greybox+ source digest must be SHA-256")
        source = {primitive.id: primitive for primitive in self.dungeon.unity_primitives}
        mapped = {primitive.source_primitive_id: primitive for primitive in self.primitives}
        if len(source) != len(self.dungeon.unity_primitives):
            raise ValueError("canonical dungeon primitive IDs must be unique")
        if len(mapped) != len(self.primitives) or set(source) != set(mapped):
            raise ValueError("Greybox+ roles must map every canonical dungeon primitive")
        for source_id, source_primitive in source.items():
            primitive = mapped[source_id]
            if primitive.id != f"plus_{source_id}":
                raise ValueError(
                    f"Greybox+ primitive {primitive.id!r} has an unstable ID"
                )
            if primitive.category != _greybox_plus_role(source_primitive):
                raise ValueError(
                    f"Greybox+ primitive {primitive.id!r} has the wrong role"
                )
            if (primitive.grid_position != source_primitive.grid_position
                    or primitive.world_position != source_primitive.world_position
                    or primitive.size != source_primitive.size
                    or primitive.rotation != source_primitive.rotation):
                raise ValueError(
                    f"Greybox+ primitive {primitive.id!r} drifts from {source_id!r}"
                )
        # A valid model has passed every deterministic acceptance invariant.
        # Keep the serialized status aligned with that fact rather than leaving
        # a stale false value in a sidecar after a successful rebuild.
        self.accepted = True
        return self


class GreyboxValidation(BaseModel):
    accepted: bool
    topology_errors: list[str] = Field(default_factory=list)
    coordinate_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return self.topology_errors + self.coordinate_errors


class GreyboxPlusValidation(BaseModel):
    accepted: bool
    errors: list[str] = Field(default_factory=list)


class GreyboxValidationError(ValueError):
    """Raised when a greybox cannot be accepted without guessing."""


def _world_position(point: GridPoint, cell_size: float) -> list[float]:
    # Round only for JSON stability; validation below still checks that the
    # decimal representation is an exact cell multiple.
    return [round(float(point.x * cell_size), 9),
            round(float(point.y * cell_size), 9),
            round(float(point.z * cell_size), 9)]


def _bounds(center: GridPoint, width: int, height: int, depth: int) -> GridBounds:
    return GridBounds(
        min_x=center.x - width // 2,
        min_y=center.y,
        min_z=center.z - depth // 2,
        max_x=center.x - width // 2 + width,
        max_y=center.y + height,
        max_z=center.z - depth // 2 + depth,
    )


def _room_center(room: RoomIR) -> GridPoint:
    return room.grid_position


def _line(start: GridPoint, end: GridPoint) -> list[GridSegmentIR]:
    """Create a deterministic axis-aligned path, including elevation."""
    points = [start]
    current = start
    if current.x != end.x:
        current = GridPoint(x=end.x, y=current.y, z=current.z)
        points.append(current)
    if current.z != end.z:
        current = GridPoint(x=current.x, y=current.y, z=end.z)
        points.append(current)
    if current.y != end.y:
        current = GridPoint(x=current.x, y=end.y, z=current.z)
        points.append(current)
    if points[-1] != end:
        points.append(end)
    return [GridSegmentIR(start=a, end=b) for a, b in zip(points, points[1:])]


def _segment_cells(segment: GridSegmentIR) -> list[GridPoint]:
    a, b = segment.start, segment.end
    dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
    axes = [abs(dx), abs(dy), abs(dz)]
    if sum(1 for value in axes if value) > 1:
        return []
    distance = max(axes)
    if distance == 0:
        return [a]
    sx = 0 if dx == 0 else (1 if dx > 0 else -1)
    sy = 0 if dy == 0 else (1 if dy > 0 else -1)
    sz = 0 if dz == 0 else (1 if dz > 0 else -1)
    return [GridPoint(x=a.x + sx * i, y=a.y + sy * i, z=a.z + sz * i)
            for i in range(distance + 1)]


def _all_corridor_cells(corridor: CorridorIR) -> list[GridPoint]:
    out: list[GridPoint] = []
    for segment in corridor.path:
        cells = _segment_cells(segment)
        out.extend(cells if not out else cells[1:])
    return out


def _room_cell_set(room: RoomIR) -> set[tuple[int, int, int]]:
    b = room.bounds
    return {
        (x, y, z)
        for x in range(b.min_x, b.max_x)
        for y in range(b.min_y, b.max_y)
        for z in range(b.min_z, b.max_z)
    }


def _edge_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _seed_from_schema(schema: Any) -> int:
    meta = getattr(schema, "meta", {}) or {}
    raw = meta.get("greybox") if isinstance(meta, Mapping) else None
    if isinstance(raw, Mapping) and raw.get("seed") is not None:
        try:
            return int(raw["seed"])
        except (TypeError, ValueError):
            pass
    if isinstance(meta, Mapping) and meta.get("seed") is not None:
        try:
            return int(meta["seed"])
        except (TypeError, ValueError):
            pass
    name = str(getattr(schema, "project_name", "greybox"))
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big")


def greybox_config_from_schema(schema: Any) -> GreyboxConstraints:
    meta = getattr(schema, "meta", {}) or {}
    raw = meta.get("greybox") if isinstance(meta, Mapping) else None
    if not isinstance(raw, Mapping):
        return GreyboxConstraints()
    config = raw.get("constraints", raw)
    if not isinstance(config, Mapping):
        return GreyboxConstraints()
    allowed = set(GreyboxConstraints.model_fields)
    return GreyboxConstraints(**{k: v for k, v in config.items() if k in allowed})


def is_greybox_enabled(schema: Any) -> bool:
    meta = getattr(schema, "meta", {}) or {}
    if isinstance(meta, Mapping) and meta.get("greybox_mode") in {
        GenerationMode.GEOMETRY_ONLY.value, GenerationMode.FULL.value,
    }:
        return True
    if isinstance(meta, Mapping) and isinstance(meta.get("greybox"), Mapping):
        return True
    return any(getattr(script, "type", "") in GREYBOX_SCRIPT_TYPES
               for script in getattr(schema, "scripts", []) or [])


def is_geometry_only_greybox(schema: Any) -> bool:
    """Whether this request selected the isolated, geometry-only project path."""
    meta = getattr(schema, "meta", {}) or {}
    if not isinstance(meta, Mapping):
        return False
    mode = meta.get("greybox_mode")
    if mode is None and isinstance(meta.get("greybox"), Mapping):
        mode = meta["greybox"].get("mode")
    if mode is None and isinstance(meta.get("greybox_ir"), Mapping):
        mode = meta["greybox_ir"].get("mode")
    if mode is None and any(
            getattr(script, "type", "") in GREYBOX_SCRIPT_TYPES
            for script in getattr(schema, "scripts", []) or []):
        return True
    return mode == GenerationMode.GEOMETRY_ONLY.value


def _make_rooms(seed: int, cell_size: float,
                constraints: GreyboxConstraints) -> tuple[list[RoomIR], list[tuple[str, str]]]:
    rng = random.Random(seed)
    room_count = max(constraints.min_rooms, min(constraints.max_rooms, 6))
    room_count = max(4, room_count, constraints.min_boss_depth + 1)
    if room_count > constraints.max_rooms:
        raise GreyboxValidationError(
            "greybox constraints cannot satisfy min_boss_depth within max_rooms"
        )
    main_count = max(
        4,
        constraints.min_boss_depth + 1,
        room_count - max(1, room_count // 3),
    )
    if main_count > room_count:
        room_count = main_count
        if room_count > constraints.max_rooms:
            raise GreyboxValidationError(
                "greybox constraints cannot satisfy min_boss_depth within max_rooms"
            )
    if constraints.max_branching <= 2:
        # A pure chain is the only graph that cannot exceed degree two.
        main_count, room_count = room_count, room_count
    branch_count = room_count - main_count

    rooms: list[RoomIR] = []
    room_sizes: dict[str, tuple[int, int, int]] = {}
    for index in range(main_count):
        room_id = "start" if index == 0 else "boss" if index == main_count - 1 else f"room_{index:02d}"
        width = rng.randrange(constraints.min_room_width,
                              max(constraints.min_room_width + 1,
                                  constraints.max_room_width + 1))
        depth = rng.randrange(constraints.min_room_depth,
                              max(constraints.min_room_depth + 1,
                                  constraints.max_room_depth + 1))
        # Even dimensions keep every connector on an integer cell.
        width += width % 2
        depth += depth % 2
        height = 3 + rng.randrange(0, 3)
        room_sizes[room_id] = (width, height, depth)

    x_positions: list[int] = [0]
    for index in range(1, main_count):
        previous = "start" if index == 1 else f"room_{index - 1:02d}"
        current = "boss" if index == main_count - 1 else f"room_{index:02d}"
        gap = 2
        x_positions.append(
            x_positions[-1] + room_sizes[previous][0] // 2 + gap
            + room_sizes[current][0] // 2
        )
    main_by_index: list[RoomIR] = []
    for index in range(main_count):
        room_id = "start" if index == 0 else "boss" if index == main_count - 1 else f"room_{index:02d}"
        width, height, depth = room_sizes[room_id]
        center = GridPoint(x=x_positions[index], y=0, z=0)
        room = RoomIR(
            id=room_id,
            type="start" if index == 0 else "boss" if index == main_count - 1 else "mid",
            bounds=_bounds(center, width, height, depth),
            grid_position=center,
            world_position=_world_position(center, cell_size),
            width=width,
            height=height,
            depth=depth,
        )
        rooms.append(room)
        main_by_index.append(room)

    for index in range(branch_count):
        attach_index = min(1 + index, max(1, main_count - 2))
        attach = main_by_index[attach_index]
        room_id = f"branch_{index + 1:02d}"
        width = rng.randrange(constraints.min_room_width,
                              max(constraints.min_room_width + 1,
                                  constraints.max_room_width + 1))
        depth = rng.randrange(constraints.min_room_depth,
                              max(constraints.min_room_depth + 1,
                                  constraints.max_room_depth + 1))
        width += width % 2
        depth += depth % 2
        height = 3 + rng.randrange(0, 3)
        z = (attach.bounds.max_z + 2 + depth // 2
             + (index // max(1, main_count - 2)) * (depth + 2))
        # Branch rooms share the main corridor's X lane, so a wide branch can
        # reach the next main room.  Resolve that geometric fact by moving the
        # branch north in whole-cell increments; no random retries or floating
        # point nudges are allowed.
        while True:
            center = GridPoint(x=attach.grid_position.x, y=0, z=z)
            candidate_bounds = _bounds(center, width, height, depth)
            if not any(candidate_bounds.overlaps(room.bounds) for room in rooms):
                break
            z += depth + 2
        rooms.append(RoomIR(
            id=room_id,
            type="branch",
            bounds=_bounds(center, width, height, depth),
            grid_position=center,
            world_position=_world_position(center, cell_size),
            width=width,
            height=height,
            depth=depth,
            is_branch=True,
        ))

    edges: list[tuple[str, str]] = []
    for index in range(1, main_count):
        left = main_by_index[index - 1].id
        right = main_by_index[index].id
        edges.append((left, right))
    for index in range(branch_count):
        attach_index = min(1 + index, max(1, main_count - 2))
        edges.append((main_by_index[attach_index].id, f"branch_{index + 1:02d}"))

    # The loop is an edge decision, never a layout side effect.  It closes the
    # last branch back to the adjacent main room without shortening the boss
    # path below the minimum depth.
    should_loop = (
        branch_count > 0
        and constraints.max_loops > 0
        and (constraints.min_loops > 0 or rng.random() < constraints.loop_frequency)
    )
    if should_loop:
        branch_id = f"branch_{branch_count:02d}"
        target_index = min(1 + branch_count - 1, max(1, main_count - 2))
        target = main_by_index[target_index].id
        # Join the last branch to its preceding branch when there is one.  It
        # keeps the loop's degree within the default branching limit and keeps
        # the closing corridor in the branch lane rather than across a main
        # room.  With one branch, use the preceding main room instead.
        if branch_count > 1:
            target = f"branch_{branch_count - 1:02d}"
        else:
            target = main_by_index[min(main_count - 2, target_index + 1)].id
        candidate = (branch_id, target)
        if _edge_key(*candidate) not in {_edge_key(*edge) for edge in edges}:
            edges.append(candidate)
            next(room for room in rooms if room.id == branch_id).is_loop = True
    return rooms, edges


def generate_dungeon_greybox(
    seed: int,
    *,
    mode: GenerationMode | str = GenerationMode.GEOMETRY_ONLY,
    cell_size: float = 2.0,
    constraints: GreyboxConstraints | Mapping[str, Any] | None = None,
    theme: str = "none",
    twist_tags: Optional[Iterable[str]] = None,
    biome_id: str = "",
) -> DungeonGreyboxIR:
    """Generate, validate and return one deterministic dungeon IR."""
    selected_mode = GenerationMode(mode)
    config = (constraints if isinstance(constraints, GreyboxConstraints)
              else GreyboxConstraints(**dict(constraints or {})))
    if selected_mode == GenerationMode.GEOMETRY_ONLY:
        theme, twist_tags, biome_id = "none", [], ""
    rooms, edges = _make_rooms(int(seed), float(cell_size), config)
    room_by_id = {room.id: room for room in rooms}

    rng = random.Random(int(seed) ^ 0x47524258)
    # Elevation is opt-in.  Geometry-only defaults to a planar dungeon, while
    # explicit elevation_frequency still produces first-class stair IR.
    if config.elevation_frequency > 0.0:
        candidates = [room for room in rooms if room.type == "branch"]
        if candidates and rng.random() < config.elevation_frequency:
            elevated = candidates[0]
            elevated.grid_position = GridPoint(
                x=elevated.grid_position.x, y=1, z=elevated.grid_position.z)
            elevated.elevation = 1
            elevated.bounds = _bounds(elevated.grid_position,
                                       elevated.width, elevated.height,
                                       elevated.depth)
            elevated.world_position = _world_position(elevated.grid_position, cell_size)

    corridors: list[CorridorIR] = []
    elevation_changes: list[ElevationChangeIR] = []
    for index, (from_id, to_id) in enumerate(edges):
        source, target = room_by_id[from_id], room_by_id[to_id]
        prior_adjacency: dict[str, set[str]] = {room.id: set() for room in rooms}
        for prior in corridors:
            prior_adjacency[prior.from_room_id].add(prior.to_room_id)
            prior_adjacency[prior.to_room_id].add(prior.from_room_id)
        # A loop is identified by graph order: an edge joining rooms that are
        # already connected by the preceding spanning-tree edges.
        is_loop = _shortest_path(prior_adjacency, from_id, to_id) >= 0

        if is_loop:
            # Close branch loops outside every room footprint.  A direct
            # horizontal segment between two branch rooms can pass through the
            # source room because the two branch lanes overlap in X; the
            # elevated north lane is a deterministic, room-disjoint detour.
            clear_z = max(room.bounds.max_z for room in rooms) + 1
            start = GridPoint(x=source.grid_position.x, y=source.grid_position.y,
                              z=source.bounds.max_z)
            via_source = GridPoint(x=start.x, y=start.y, z=clear_z)
            via_target = GridPoint(x=target.grid_position.x, y=target.grid_position.y,
                                   z=clear_z)
            end = GridPoint(x=target.grid_position.x, y=target.grid_position.y,
                            z=target.bounds.max_z)
            path = (_line(start, via_source) + _line(via_source, via_target)
                    + _line(via_target, end))
        elif target.grid_position.x > source.grid_position.x:
            start = GridPoint(x=source.bounds.max_x, y=source.grid_position.y,
                              z=source.grid_position.z)
            end = GridPoint(x=target.bounds.min_x - 1, y=target.grid_position.y,
                            z=target.grid_position.z)
        elif target.grid_position.x < source.grid_position.x:
            start = GridPoint(x=source.bounds.min_x - 1, y=source.grid_position.y,
                              z=source.grid_position.z)
            end = GridPoint(x=target.bounds.max_x, y=target.grid_position.y,
                            z=target.grid_position.z)
        elif target.grid_position.z >= source.grid_position.z:
            start = GridPoint(x=source.grid_position.x, y=source.grid_position.y,
                              z=source.bounds.max_z)
            end = GridPoint(x=target.grid_position.x, y=target.grid_position.y,
                            z=target.bounds.min_z - 1)
        else:
            start = GridPoint(x=source.grid_position.x, y=source.grid_position.y,
                              z=source.bounds.min_z - 1)
            end = GridPoint(x=target.grid_position.x, y=target.grid_position.y,
                            z=target.bounds.max_z)
        if not is_loop:
            path = _line(start, end)
        deltas = [segment.end.y - segment.start.y for segment in path]
        corridor_id = f"corridor_{index + 1:02d}"
        corridor = CorridorIR(
            id=corridor_id,
            from_room_id=from_id,
            to_room_id=to_id,
            path=path,
            width=max(config.min_corridor_width,
                      min(config.max_corridor_width, 2)),
            elevation=source.elevation,
            elevation_deltas=deltas,
            is_branch=source.is_branch or target.is_branch,
            is_loop=is_loop,
        )
        corridors.append(corridor)
        for room, point in ((source, start), (target, end)):
            direction = "east" if point.x >= room.grid_position.x else "west"
            if point.z != room.grid_position.z:
                direction = "north" if point.z >= room.grid_position.z else "south"
            room.connectors.append(ConnectorIR(
                id=f"{room.id}_door_{len(room.connectors) + 1:02d}",
                grid_position=point,
                direction=direction,
                corridor_id=corridor_id,
            ))
        vertical = [segment for segment in path if segment.start.y != segment.end.y]
        if vertical:
            segment = vertical[0]
            elevation_changes.append(ElevationChangeIR(
                id=f"elevation_{len(elevation_changes) + 1:02d}",
                path=[segment],
                width=corridor.width,
                from_elevation=segment.start.y,
                to_elevation=segment.end.y,
            ))

    adjacency: dict[str, list[str]] = {room.id: [] for room in rooms}
    for corridor in corridors:
        adjacency[corridor.from_room_id].append(corridor.to_room_id)
        adjacency[corridor.to_room_id].append(corridor.from_room_id)
    adjacency = {key: sorted(value) for key, value in sorted(adjacency.items())}
    branching = {room_id: len(neighbors) for room_id, neighbors in adjacency.items()}

    markers: list[MarkerIR] = []
    start_room, boss_room = room_by_id["start"], room_by_id["boss"]
    for marker_id, marker_type, room in (
        ("spawn_start", "spawn", start_room),
        ("boss_target", "boss", boss_room),
    ):
        markers.append(MarkerIR(
            id=marker_id,
            type=marker_type,
            room_id=room.id,
            grid_position=room.grid_position,
            world_position=_world_position(room.grid_position, cell_size),
        ))
    for room in rooms:
        for connector in room.connectors:
            markers.append(MarkerIR(
                id=connector.id,
                type="door",
                room_id=room.id,
                grid_position=connector.grid_position,
                world_position=_world_position(connector.grid_position, cell_size),
            ))

    # `unity_primitives` is the complete, schema-driven ProBuilder recipe.
    # Do not emit one solid cuboid per logical room: that proves an object was
    # made while producing a non-navigable slab.  Floors, perimeter wall cells,
    # and corridor side walls are explicit primitives with integer grid origins,
    # so Unity does not have to infer a different spatial layout from the room
    # graph.  Their world coordinates remain exact `grid * cell_size` values.
    primitives: list[UnityPrimitiveIR] = []
    floor_height = round(max(0.05, cell_size * 0.125), 9)
    wall_height_cells = 2

    def add_primitive(
        primitive_id: str,
        primitive_type: str,
        parent: str,
        point: GridPoint,
        size: list[float],
    ) -> None:
        primitives.append(UnityPrimitiveIR(
            id=primitive_id,
            primitive_type=primitive_type,
            parent=parent,
            grid_position=point,
            world_position=_world_position(point, cell_size),
            size=[round(value, 9) for value in size],
        ))

    def doorway_cell(room: RoomIR, connector: ConnectorIR) -> tuple[int, int]:
        """Map the graph connector to its actual perimeter wall grid cell."""
        direction = connector.direction.lower()
        if direction == "west":
            return room.bounds.min_x, connector.grid_position.z
        if direction == "east":
            return room.bounds.max_x, connector.grid_position.z
        if direction == "south":
            return connector.grid_position.x, room.bounds.min_z
        return connector.grid_position.x, room.bounds.max_z

    def corridor_doorways(corridor: CorridorIR) -> tuple[GridPoint, GridPoint]:
        source = room_by_id[corridor.from_room_id]
        target = room_by_id[corridor.to_room_id]
        source_connector = next(
            connector for connector in source.connectors
            if connector.corridor_id == corridor.id
        )
        target_connector = next(
            connector for connector in target.connectors
            if connector.corridor_id == corridor.id
        )
        source_x, source_z = doorway_cell(source, source_connector)
        target_x, target_z = doorway_cell(target, target_connector)
        return (
            GridPoint(x=source_x, y=source_connector.grid_position.y, z=source_z),
            GridPoint(x=target_x, y=target_connector.grid_position.y, z=target_z),
        )

    for room in rooms:
        add_primitive(
            f"{room.id}_floor", "floor", "Geometry", room.grid_position,
            [room.width * cell_size, floor_height, room.depth * cell_size],
        )
        doorway_cells = {doorway_cell(room, connector) for connector in room.connectors}
        y = room.grid_position.y
        # West/east include the corners. South/north omit them, avoiding
        # duplicate ProBuilder meshes while keeping the perimeter closed.
        for z in range(room.bounds.min_z, room.bounds.max_z + 1):
            for side, x in (("west", room.bounds.min_x), ("east", room.bounds.max_x)):
                if (x, z) in doorway_cells:
                    continue
                add_primitive(
                    f"{room.id}_wall_{side}_{z - room.bounds.min_z:02d}",
                    "wall", "Geometry", GridPoint(x=x, y=y, z=z),
                    [cell_size, room.height * cell_size, cell_size],
                )
        for x in range(room.bounds.min_x + 1, room.bounds.max_x):
            for side, z in (("south", room.bounds.min_z), ("north", room.bounds.max_z)):
                if (x, z) in doorway_cells:
                    continue
                add_primitive(
                    f"{room.id}_wall_{side}_{x - room.bounds.min_x:02d}",
                    "wall", "Geometry", GridPoint(x=x, y=y, z=z),
                    [cell_size, room.height * cell_size, cell_size],
                )

    for corridor in corridors:
        cells = _all_corridor_cells(corridor)
        start, end = corridor_doorways(corridor)
        # Existing graph paths stop one grid cell before an opposing room's
        # perimeter. Include the authoritative doorway endpoints so a visual
        # corridor cannot appear disconnected while topology claims it is live.
        if not cells or cells[0] != start:
            cells.insert(0, start)
        if cells[-1] != end:
            cells.append(end)

        corridor_height = max(wall_height_cells, 1) * cell_size
        side_offset = max(1, (corridor.width + 1) // 2)
        for index, point in enumerate(cells):
            neighbor = cells[index + 1] if index + 1 < len(cells) else cells[index - 1]
            axis_x = neighbor.x != point.x
            axis_z = neighbor.z != point.z
            parent = "Elevation" if neighbor.y != point.y else "Corridors"
            sides: tuple[tuple[str, GridPoint], ...]
            if axis_x:
                floor_size = [cell_size, floor_height, corridor.width * cell_size]
            elif axis_z:
                floor_size = [corridor.width * cell_size, floor_height, cell_size]
            else:
                floor_size = [corridor.width * cell_size, floor_height,
                              corridor.width * cell_size]
            add_primitive(
                f"{corridor.id}_floor_{index + 1:02d}", "floor", parent,
                point, floor_size,
            )
            if parent != "Corridors":
                continue
            if axis_x:
                sides = (("north", GridPoint(
                    x=point.x, y=point.y, z=point.z + side_offset)),
                         ("south", GridPoint(
                    x=point.x, y=point.y, z=point.z - side_offset)))
            elif axis_z:
                sides = (("east", GridPoint(
                    x=point.x + side_offset, y=point.y, z=point.z)),
                         ("west", GridPoint(
                    x=point.x - side_offset, y=point.y, z=point.z)))
            else:
                sides = ()
            for side, wall_point in sides:
                add_primitive(
                    f"{corridor.id}_wall_{side}_{index + 1:02d}",
                    "wall", "Corridors", wall_point,
                    [cell_size, corridor_height, cell_size],
                )

    ir = DungeonGreyboxIR(
        seed=int(seed),
        mode=selected_mode,
        cell_size=float(cell_size),
        theme=theme,
        twist_tags=list(twist_tags or []),
        biome_id=biome_id,
        rooms=rooms,
        corridors=corridors,
        elevation_changes=elevation_changes,
        unity_primitives=primitives,
        markers=markers,
        adjacency=adjacency,
        start_room_id="start",
        boss_room_id="boss",
        boss_depth=_shortest_path(adjacency, "start", "boss"),
        loop_count=max(0, len(corridors) - len(rooms) + 1),
        branching_factors=branching,
        constraints=config,
    )
    report = validate_dungeon_greybox(ir)
    if not report.accepted:
        raise GreyboxValidationError("; ".join(report.errors))
    ir.accepted = True
    return ir


def _shortest_path(adjacency: Mapping[str, Iterable[str]], start: str, target: str) -> int:
    if start == target:
        return 0
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        node, depth = queue.popleft()
        for neighbor in adjacency.get(node, []):
            if neighbor == target:
                return depth + 1
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
    return -1


def validate_grid_alignment(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    cell = Decimal(str(ir.cell_size))
    if cell <= 0:
        return ["cell_size must be positive"]

    def check(label: str, point: GridPoint, world: list[float]) -> None:
        if len(world) != 3:
            errors.append(f"{label} world_position must have three coordinates")
            return
        for axis, grid, value in zip("xyz", (point.x, point.y, point.z), world):
            try:
                actual = Decimal(str(value))
                expected = Decimal(grid) * cell
            except (InvalidOperation, ValueError):
                errors.append(f"{label}.{axis} is not numeric")
                continue
            if actual != expected:
                errors.append(
                    f"{label}.{axis}={value!r} is not the exact grid coordinate "
                    f"{grid}*cell_size ({expected})"
                )

    for room in ir.rooms:
        check(f"room {room.id}", room.grid_position, room.world_position)
    for primitive in ir.unity_primitives:
        check(f"primitive {primitive.id}", primitive.grid_position,
              primitive.world_position)
    for marker in ir.markers:
        check(f"marker {marker.id}", marker.grid_position, marker.world_position)
    return errors


def validate_connectivity(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    room_ids = {room.id for room in ir.rooms}
    if ir.start_room_id not in room_ids:
        errors.append(f"start room {ir.start_room_id!r} does not exist")
    if ir.boss_room_id not in room_ids:
        errors.append(f"boss room {ir.boss_room_id!r} does not exist")
    adjacency: dict[str, set[str]] = {room_id: set() for room_id in room_ids}
    for corridor in ir.corridors:
        if corridor.from_room_id not in room_ids or corridor.to_room_id not in room_ids:
            errors.append(f"corridor {corridor.id} references an unknown room")
            continue
        adjacency[corridor.from_room_id].add(corridor.to_room_id)
        adjacency[corridor.to_room_id].add(corridor.from_room_id)
    for room_id in sorted(room_ids):
        declared = set(ir.adjacency.get(room_id, []))
        if declared != adjacency[room_id]:
            errors.append(f"adjacency metadata for room {room_id!r} disagrees with corridors")
    reachable: set[str] = set()
    if ir.start_room_id in room_ids:
        queue = deque([ir.start_room_id])
        while queue:
            node = queue.popleft()
            if node in reachable:
                continue
            reachable.add(node)
            queue.extend(sorted(adjacency[node] - reachable))
    if ir.boss_room_id not in reachable:
        errors.append("boss room is not reachable from start room")
    for room in ir.rooms:
        if not room.optional and room.id not in reachable:
            errors.append(f"required room {room.id!r} is unreachable")
    return errors


def validate_loops_and_branching(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    room_ids = {room.id for room in ir.rooms}
    edges = {
        _edge_key(corridor.from_room_id, corridor.to_room_id)
        for corridor in ir.corridors
        if corridor.from_room_id in room_ids and corridor.to_room_id in room_ids
    }
    cycles = max(0, len(edges) - len(room_ids) + 1) if room_ids else 0
    if cycles != ir.loop_count:
        errors.append(f"loop_count={ir.loop_count} but graph has {cycles} cycle(s)")
    if cycles < ir.constraints.min_loops or cycles > ir.constraints.max_loops:
        errors.append(
            f"loop count {cycles} outside [{ir.constraints.min_loops}, "
            f"{ir.constraints.max_loops}]"
        )
    for room_id, degree in ir.branching_factors.items():
        if degree > ir.constraints.max_branching:
            errors.append(
                f"room {room_id!r} branching factor {degree} exceeds "
                f"{ir.constraints.max_branching}"
            )
    return errors


def validate_widths(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    for room in ir.rooms:
        if not (ir.constraints.min_room_width <= room.width <= ir.constraints.max_room_width):
            errors.append(f"room {room.id} width {room.width} is outside configured range")
        if not (ir.constraints.min_room_depth <= room.depth <= ir.constraints.max_room_depth):
            errors.append(f"room {room.id} depth {room.depth} is outside configured range")
    for corridor in ir.corridors:
        if not (ir.constraints.min_corridor_width <= corridor.width <= ir.constraints.max_corridor_width):
            errors.append(f"corridor {corridor.id} width {corridor.width} is outside configured range")
        if len(corridor.elevation_deltas) != len(corridor.path):
            errors.append(f"corridor {corridor.id} has inconsistent elevation deltas")
        elif any(delta != corridor.elevation_deltas[index]
                 for index, delta in enumerate(
                     segment.end.y - segment.start.y for segment in corridor.path)):
            errors.append(f"corridor {corridor.id} has inconsistent elevation deltas")
    return errors


def validate_overlaps(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    for index, first in enumerate(ir.rooms):
        for second in ir.rooms[index + 1:]:
            if first.bounds.overlaps(second.bounds):
                errors.append(f"rooms {first.id!r} and {second.id!r} overlap")
    room_cells = {
        room.id: _room_cell_set(room) for room in ir.rooms
    }
    connector_cells = {
        (connector.grid_position.x, connector.grid_position.y, connector.grid_position.z)
        for room in ir.rooms for connector in room.connectors
    }
    for corridor in ir.corridors:
        for point in _all_corridor_cells(corridor):
            cell = (point.x, point.y, point.z)
            if any(cell in cells and cell not in connector_cells
                   for cells in room_cells.values()):
                errors.append(
                    f"corridor {corridor.id} intersects a room interior at {cell}"
                )
                break
    return errors


def validate_boss_depth(ir: DungeonGreyboxIR) -> list[str]:
    adjacency: dict[str, set[str]] = {room.id: set() for room in ir.rooms}
    for corridor in ir.corridors:
        if corridor.from_room_id in adjacency and corridor.to_room_id in adjacency:
            adjacency[corridor.from_room_id].add(corridor.to_room_id)
            adjacency[corridor.to_room_id].add(corridor.from_room_id)
    depth = _shortest_path(adjacency, ir.start_room_id, ir.boss_room_id)
    errors: list[str] = []
    if depth != ir.boss_depth:
        errors.append(f"boss_depth={ir.boss_depth} but shortest path is {depth}")
    if not (ir.constraints.min_boss_depth <= depth <= ir.constraints.max_boss_depth):
        errors.append(
            f"boss depth {depth} outside [{ir.constraints.min_boss_depth}, "
            f"{ir.constraints.max_boss_depth}]"
        )
    return errors


def validate_topology(ir: DungeonGreyboxIR) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_connectivity(ir))
    errors.extend(validate_loops_and_branching(ir))
    errors.extend(validate_widths(ir))
    errors.extend(validate_overlaps(ir))
    errors.extend(validate_boss_depth(ir))
    return errors


def validate_coordinates(ir: DungeonGreyboxIR) -> list[str]:
    return validate_grid_alignment(ir)


def validate_dungeon_greybox(ir: DungeonGreyboxIR) -> GreyboxValidation:
    """Run the complete Step 7 greybox acceptance contract."""
    topology = validate_topology(ir)
    coordinates = validate_coordinates(ir)
    return GreyboxValidation(
        accepted=not topology and not coordinates,
        topology_errors=topology,
        coordinate_errors=coordinates,
    )


def _canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_greybox_plus_enabled(schema: Any) -> bool:
    """Whether this schema selected the image-aware Greybox+ contract."""
    meta = getattr(schema, "meta", {}) or {}
    return isinstance(meta, Mapping) and bool(
        meta.get("greybox_plus_mode") == "greybox_plus"
        or isinstance(meta.get("greybox_plus"), Mapping)
        or isinstance(meta.get("greybox_plus_ir"), Mapping)
    )


def stamp_greybox_plus_reference(schema: Any, reference_image: Path,
                                 visual_context: str) -> None:
    """Record immutable visual provenance while the image is still in scope.

    The source image is not copied into the geometry-only Unity output: it is
    evidence, not a generated texture asset. The filename plus digests are
    enough to bind an accepted IR to the exact image/context that informed
    Steps 2–3.
    """
    if not reference_image.is_file():
        raise GreyboxValidationError(
            f"Greybox+ reference image is missing: {reference_image}"
        )
    if not visual_context or not visual_context.strip():
        raise GreyboxValidationError("Greybox+ requires non-empty viz context")
    meta = getattr(schema, "meta", None)
    if not isinstance(meta, dict):
        meta = {}
        schema.meta = meta
    meta["greybox_plus_mode"] = "greybox_plus"
    meta["greybox_plus"] = {
        "reference": {
            "image_name": reference_image.name,
            "image_sha256": hashlib.sha256(reference_image.read_bytes()).hexdigest(),
            "visual_context_sha256": hashlib.sha256(
                visual_context.encode("utf-8")
            ).hexdigest(),
        },
    }


def _greybox_plus_role(primitive: UnityPrimitiveIR) -> GreyboxPlusCategory:
    """Classify the existing deterministic dungeon recipe without guessing art."""
    primitive_id = primitive.id.lower()
    if primitive.parent == "Elevation" or "elevation" in primitive_id:
        return GreyboxPlusCategory.ELEVATION
    if primitive.parent == "Corridors":
        return (GreyboxPlusCategory.RAILINGS if "_wall_" in primitive_id
                else GreyboxPlusCategory.CATWALKS)
    return GreyboxPlusCategory.BUILDINGS


def greybox_plus_ir_for_schema(
    schema: Any, dungeon: DungeonGreyboxIR | None = None,
) -> GreyboxPlusIR:
    """Build or validate the versioned Greybox+ view of the canonical dungeon.

    Slice 1 intentionally derives role records from the accepted dungeon recipe
    rather than inventing image geometry in a post-schema pass. Slice 2 will
    extend this same IR with pipes, tanks, machinery and exported editor edits.
    """
    meta = getattr(schema, "meta", {}) or {}
    if not isinstance(meta, Mapping):
        raise GreyboxValidationError("Greybox+ metadata is missing")
    raw = meta.get("greybox_plus_ir")
    if isinstance(raw, Mapping):
        ir = GreyboxPlusIR.model_validate(raw)
        report = validate_greybox_plus_ir(ir)
        if not report.accepted:
            raise GreyboxValidationError("; ".join(report.errors))
        if isinstance(meta, dict):
            meta["greybox_plus_ir"] = ir.model_dump(mode="json")
            meta["greybox_plus_acceptance"] = report.model_dump(mode="json")
        return ir
    raw_request = meta.get("greybox_plus")
    if not isinstance(raw_request, Mapping):
        raise GreyboxValidationError("Greybox+ reference provenance is missing")
    raw_reference = raw_request.get("reference")
    if not isinstance(raw_reference, Mapping):
        raise GreyboxValidationError("Greybox+ reference binding is missing")
    dungeon = dungeon or greybox_ir_for_schema(schema)
    primitives: list[GreyboxPlusPrimitiveIR] = []
    for source in dungeon.unity_primitives:
        category = _greybox_plus_role(source)
        tier, hierarchy_path = GREYBOX_PLUS_ROLE_PATHS[category]
        primitives.append(GreyboxPlusPrimitiveIR(
            id=f"plus_{source.id}",
            source_primitive_id=source.id,
            tier=tier,
            category=category,
            hierarchy_path=hierarchy_path,
            grid_position=source.grid_position,
            world_position=list(source.world_position),
            size=list(source.size),
            rotation=list(source.rotation),
        ))
    ir = GreyboxPlusIR(
        reference=GreyboxPlusReferenceIR.model_validate(raw_reference),
        source_greybox_sha256=_canonical_sha256(dungeon),
        dungeon=dungeon,
        primitives=primitives,
    )
    report = validate_greybox_plus_ir(ir)
    if not report.accepted:
        raise GreyboxValidationError("; ".join(report.errors))
    if isinstance(meta, dict):
        meta["greybox_plus_ir"] = ir.model_dump(mode="json")
        meta["greybox_plus_acceptance"] = report.model_dump(mode="json")
    return ir


def validate_greybox_plus_ir(ir: GreyboxPlusIR) -> GreyboxPlusValidation:
    errors: list[str] = []
    # Callers may have adjusted a model in memory after Pydantic constructed
    # it. Re-validating its serialized form makes Step 7 reject those changes
    # just as it rejects a malformed sidecar read from disk.
    try:
        validated = GreyboxPlusIR.model_validate(ir.model_dump(mode="json"))
    except ValidationError as exc:
        return GreyboxPlusValidation(
            accepted=False,
            errors=[f"Greybox+ IR structural validation failed: {exc}"],
        )
    errors.extend(validate_dungeon_greybox(validated.dungeon).errors)
    if validated.source_greybox_sha256 != _canonical_sha256(validated.dungeon):
        errors.append("Greybox+ source digest disagrees with embedded dungeon IR")
    return GreyboxPlusValidation(accepted=not errors, errors=errors)


def greybox_ir_for_schema(schema: Any) -> DungeonGreyboxIR:
    """Return the schema's deterministic greybox, stamping no creative data."""
    meta = getattr(schema, "meta", {}) or {}
    raw = meta.get("greybox_ir") if isinstance(meta, Mapping) else None
    if isinstance(raw, Mapping):
        ir = DungeonGreyboxIR.model_validate(raw)
        report = validate_dungeon_greybox(ir)
        if not report.accepted:
            raise GreyboxValidationError("; ".join(report.errors))
        return ir
    config = greybox_config_from_schema(schema)
    raw_greybox = meta.get("greybox") if isinstance(meta, Mapping) else None
    cell_size = 2.0
    if isinstance(raw_greybox, Mapping):
        try:
            cell_size = float(raw_greybox.get("cell_size", cell_size))
        except (TypeError, ValueError):
            pass
    ir = generate_dungeon_greybox(
        _seed_from_schema(schema),
        mode=GenerationMode.GEOMETRY_ONLY,
        cell_size=cell_size,
        constraints=config,
    )
    if isinstance(meta, dict):
        meta["greybox_ir"] = ir.model_dump(mode="json")
        meta["greybox_acceptance"] = validate_dungeon_greybox(ir).model_dump(mode="json")
    return ir


def apply_greybox_spatial_layout(schema: Any, ir: DungeonGreyboxIR) -> list[str]:
    """Place declared entity groups inside the greybox rooms.

    This is intentionally a deterministic role-to-room bridge, not a theme or
    twist interpreter.  Authored Scene Compiler transforms remain authoritative
    and are not rewritten; the greybox is still the world bounds/coordinate
    authority for every generated position.
    """
    entities = list(getattr(schema, "entities", []) or [])
    if not entities:
        return []
    authored = set()
    meta = getattr(schema, "meta", {}) or {}
    if isinstance(meta, Mapping):
        authored = {str(name) for name in (meta.get("authored_models") or {})}
    rooms = [room for room in ir.rooms if room.type not in {"start", "boss"}]
    if not rooms:
        rooms = list(ir.rooms)
    messages: list[str] = []
    room_bindings: dict[str, str] = {}
    cursor = 0
    for entity in sorted(entities, key=lambda item: str(getattr(item, "name", ""))):
        name = str(getattr(entity, "name", ""))
        if name in authored and getattr(entity, "instance_positions", None):
            continue
        kind = str(getattr(entity, "kind", "")).lower()
        tag = str(getattr(entity, "tag", "")).lower()
        if tag == "player" or kind in {"player", "avatar"}:
            room = next(room for room in ir.rooms if room.id == ir.start_room_id)
        elif kind in {"boss", "goal", "objective"} or tag == "enemy" and "boss" in name.lower():
            room = next(room for room in ir.rooms if room.id == ir.boss_room_id)
        else:
            room = rooms[cursor % len(rooms)]
            cursor += 1
        center = room.grid_position
        x, y, z = _world_position(center, ir.cell_size)
        count = max(1, int(getattr(entity, "count", 1) or 1))
        spacing = max(1.0, float(getattr(entity, "spacing", 2.0) or 2.0))
        step_cells = max(1, int(round(spacing / ir.cell_size)))
        max_offset = max(0, room.width // 2 - 1)
        positions = [
            [
                (center.x + max(-max_offset, min(
                    max_offset,
                    int(round((index - (count - 1) / 2.0) * step_cells)),
                ))) * ir.cell_size,
                y,
                z,
            ]
            for index in range(count)
        ]
        entity.instance_positions = positions
        entity.instance_yaws = []
        entity.origin_offset = [round(x, 3), round(z, 3)]
        entity.elevation = y
        room_bindings[name] = room.id
        messages.append(f"Greybox placed {name!r} in room {room.id!r}")
    if isinstance(meta, dict):
        meta["greybox_entity_rooms"] = room_bindings
        meta["greybox_world_bounds"] = list(greybox_world_bounds(ir))
    return messages


def greybox_world_bounds(ir: DungeonGreyboxIR) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for room in ir.rooms:
        points.extend([
            (room.bounds.min_x * ir.cell_size, room.bounds.min_z * ir.cell_size),
            (room.bounds.max_x * ir.cell_size, room.bounds.max_z * ir.cell_size),
        ])
    for corridor in ir.corridors:
        for point in _all_corridor_cells(corridor):
            points.append((point.x * ir.cell_size, point.z * ir.cell_size))
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs, zs = zip(*points)
    return (min(xs), min(zs), max(xs), max(zs))


# Public vocabulary aliases: the longer names remain the implementation names
# while these match the validator nouns used in the greybox acceptance plan.
validate_loop_branching = validate_loops_and_branching
validate_width = validate_widths
validate_overlap = validate_overlaps
generate_greybox_ir = generate_dungeon_greybox
validate_greybox_ir = validate_dungeon_greybox


def unity_greybox_asset_payload(ir: DungeonGreyboxIR) -> dict[str, Any]:
    """Return the camelCase/Vector3 JSON consumed by Unity JsonUtility.

    The canonical sidecar remains the Pydantic IR above.  This compact sibling
    keeps Unity's deliberately limited JsonUtility format out of the canonical
    contract while allowing the editor emitter to consume exactly the same
    rooms, corridors and marker coordinates rather than re-inventing them.
    """
    raw = ir.model_dump(mode="json")

    def point(value: Mapping[str, Any]) -> dict[str, Any]:
        return {"x": int(value["x"]), "y": int(value["y"]), "z": int(value["z"])}

    def vector(value: Iterable[Any]) -> dict[str, float]:
        values = list(value)
        return {"x": float(values[0]), "y": float(values[1]), "z": float(values[2])}

    def bounds(value: Mapping[str, Any]) -> dict[str, int]:
        return {
            "minX": int(value["min_x"]), "minY": int(value["min_y"]),
            "minZ": int(value["min_z"]), "maxX": int(value["max_x"]),
            "maxY": int(value["max_y"]), "maxZ": int(value["max_z"]),
        }

    rooms = []
    for room in raw["rooms"]:
        rooms.append({
            "id": room["id"], "type": room["type"],
            "bounds": bounds(room["bounds"]),
            "gridPosition": point(room["grid_position"]),
            "worldPosition": vector(room["world_position"]),
            "width": room["width"], "height": room["height"],
            "depth": room["depth"], "elevation": room["elevation"],
            "connectors": [
                {
                    "id": connector["id"],
                    "gridPosition": point(connector["grid_position"]),
                    "direction": connector["direction"],
                    "corridorId": connector["corridor_id"],
                }
                for connector in room["connectors"]
            ],
            "optional": room["optional"], "isBranch": room["is_branch"],
            "isLoop": room["is_loop"],
        })
    corridors = []
    for corridor in raw["corridors"]:
        corridors.append({
            "id": corridor["id"],
            "fromRoomId": corridor["from_room_id"],
            "toRoomId": corridor["to_room_id"],
            "path": [
                {"start": point(segment["start"]), "end": point(segment["end"])}
                for segment in corridor["path"]
            ],
            "width": corridor["width"], "elevation": corridor["elevation"],
            "elevationDeltas": corridor["elevation_deltas"],
            "isBranch": corridor["is_branch"], "isLoop": corridor["is_loop"],
        })
    elevation = [
        {
            "id": change["id"], "type": change["type"],
            "path": [
                {"start": point(segment["start"]), "end": point(segment["end"])}
                for segment in change["path"]
            ],
            "width": change["width"],
            "fromElevation": change["from_elevation"],
            "toElevation": change["to_elevation"],
        }
        for change in raw["elevation_changes"]
    ]
    primitives = [
        {
            "id": primitive["id"], "primitiveType": primitive["primitive_type"],
            "parent": primitive["parent"],
            "gridPosition": point(primitive["grid_position"]),
            "worldPosition": vector(primitive["world_position"]),
            "size": vector(primitive["size"]),
        }
        for primitive in raw["unity_primitives"]
    ]
    markers = [
        {
            "id": marker["id"], "type": marker["type"],
            "roomId": marker["room_id"],
            "gridPosition": point(marker["grid_position"]),
            "worldPosition": vector(marker["world_position"]),
        }
        for marker in raw["markers"]
    ]
    return {
        "seed": raw["seed"],
        "mode": 0 if raw["mode"] == GenerationMode.GEOMETRY_ONLY.value else 1,
        "cellSize": raw["cell_size"], "theme": "none",
        "twistTags": [], "biomeId": "",
        "rooms": rooms, "corridors": corridors,
        "elevationChanges": elevation, "unityPrimitives": primitives,
        "markers": markers, "hierarchyRoot": raw["hierarchy"]["root"],
        "hierarchyChildren": raw["hierarchy"]["children"]["Dungeon_Greybox"],
        "startRoomId": raw["start_room_id"], "bossRoomId": raw["boss_room_id"],
        "bossDepth": raw["boss_depth"], "loopCount": raw["loop_count"],
        "constraints": {
            "minRooms": raw["constraints"]["min_rooms"],
            "maxRooms": raw["constraints"]["max_rooms"],
            "minLoops": raw["constraints"]["min_loops"],
            "maxLoops": raw["constraints"]["max_loops"],
            "maxBranching": raw["constraints"]["max_branching"],
            "minCorridorWidth": raw["constraints"]["min_corridor_width"],
            "maxCorridorWidth": raw["constraints"]["max_corridor_width"],
            "minRoomWidth": raw["constraints"]["min_room_width"],
            "maxRoomWidth": raw["constraints"]["max_room_width"],
            "minRoomDepth": raw["constraints"]["min_room_depth"],
            "maxRoomDepth": raw["constraints"]["max_room_depth"],
            "minBossDepth": raw["constraints"]["min_boss_depth"],
            "maxBossDepth": raw["constraints"]["max_boss_depth"],
        },
    }


__all__ = [
    "GREYBOX_FORMAT", "GREYBOX_PLUS_FORMAT", "GREYBOX_SCRIPT_TYPES",
    "GREYBOX_HIERARCHY", "GREYBOX_PLUS_HIERARCHY", "GenerationMode",
    "GreyboxPlusTier", "GreyboxPlusCategory", "GREYBOX_PLUS_ROLE_PATHS",
    "GridPoint",
    "GridBounds", "GridSegmentIR", "ConnectorIR", "RoomIR", "CorridorIR",
    "ElevationChangeIR", "MarkerIR", "UnityPrimitiveIR", "HierarchyIR",
    "GreyboxConstraints", "DungeonGreyboxIR", "GreyboxPlusReferenceIR",
    "GreyboxPlusPrimitiveIR", "GreyboxPlusHierarchyIR", "GreyboxPlusIR",
    "GreyboxValidation", "GreyboxPlusValidation",
    "GreyboxValidationError", "generate_dungeon_greybox", "validate_grid_alignment",
    "validate_connectivity", "validate_loops_and_branching", "validate_widths",
    "validate_overlaps", "validate_boss_depth", "validate_topology",
    "validate_coordinates", "validate_dungeon_greybox", "is_greybox_enabled",
    "is_geometry_only_greybox", "is_greybox_plus_enabled",
    "greybox_config_from_schema", "greybox_ir_for_schema",
    "stamp_greybox_plus_reference", "greybox_plus_ir_for_schema",
    "validate_greybox_plus_ir",
    "apply_greybox_spatial_layout", "greybox_world_bounds",
    "unity_greybox_asset_payload", "validate_loop_branching", "validate_width",
    "validate_overlap", "generate_greybox_ir", "validate_greybox_ir",
]
