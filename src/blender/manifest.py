"""
Asset manifest + job result models — what the manager returns to the caller.

The manifest is the contract the Unity overlay consumes: it lists every produced
file (fbx, textures, blend, lods) with enough metadata to import them into a
generated project's Assets/ folder.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    # Blender (or the diffusion backend) wasn't available — the job produced a
    # placeholder manifest instead of real geometry so the pipeline isn't blocked.
    STUBBED  = "stubbed"


class AssetManifest(BaseModel):
    """Describes the artifacts a single job produced."""
    name:        str
    asset_type:  str
    style:       str
    route:       str                 # "blender" | "diffusion"
    fbx:         Optional[str] = None
    blend:       Optional[str] = None
    alembic:     Optional[str] = None     # baked soft-body/cloth deformation (.abc)
    textures:    list[str]     = Field(default_factory=list)
    lods:        list[str]     = Field(default_factory=list)
    animations:  list[str]     = Field(default_factory=list)
    rigged:      bool          = False
    seed:        Optional[int] = None      # the seed that produced this asset (provenance)
    geo_hash:    Optional[str] = None      # vertex fingerprint (backend-agnostic; cache key)
    # Pre-rig mesh `.blend` the run saved (clean verts+UVs, no armature) — the
    # reusable geometry unit, cached by geo-fingerprint so material/colour variants
    # skip the geometry build. Internal; the Unity overlay ignores it.
    mesh_blend:  Optional[str] = None
    rig_strategy: str          = ""        # versioned rig micro that ran (e.g. RigAutoHumanoidV1)
    # Extra Unity Package Manager deps this asset needs (e.g. Alembic for .abc).
    unity_packages: list[str]  = Field(default_factory=list)
    tris:        Optional[int] = None     # poly budget reported by Blender
    # Sanitizer steps Blender actually performed (mesh_cleanup, uv_unwrap, …).
    operations:  list[str]     = Field(default_factory=list)
    warnings:    list[str]     = Field(default_factory=list)
    stub:        bool          = False    # True → placeholder, no real geometry

    @classmethod
    def from_blender_report(cls, data: dict) -> "AssetManifest":
        """Parse the manifest JSON the in-Blender script (scripts/run_job.py)
        drops next to its outputs."""
        return cls.model_validate(data)


class JobResult(BaseModel):
    """Outcome of one job: status + manifest + diagnostics."""
    name:      str
    status:    JobStatus
    route:     str
    attempts:  int = 0
    duration_s: float = 0.0
    manifest:  Optional[AssetManifest] = None
    error:     str = ""
    log_path:  Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.STUBBED)


class BatchManifest(BaseModel):
    """Aggregate manifest the manager returns from run_all()."""
    total:    int
    done:     int
    failed:   int
    stubbed:  int
    results:  list[JobResult] = Field(default_factory=list)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @property
    def assets(self) -> list[AssetManifest]:
        return [r.manifest for r in self.results if r.manifest is not None]
