"""
Deterministic Unity Terrain writer — no LLM, no MicroPipeline base class.

Terrain is world/region-scale, not per-entity, so it does not fit the
CodeExtensibleSchema + MicroPipeline + micro.REGISTRY shape every other
generator in this package uses — see src/unity_terraingen.py's module
docstring for the full reasoning. This module is the thin write-to-disk
layer over that generator's pure functions. It lives in src/micro/ (next to
the rest of the micro-generator library, for discovery) but is NOT
registered in REGISTRY and never goes through enrich_subsystems: there is no
LLM-authorable surface here (heightmap/splatmap bytes, not C# fields) to
enrich.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..atoms.backends.unity import unity_atom_snippet
from ..micro_pipeline import to_pascal
from ..unity_generator import _make_guid, _meta_file
from ..unity_terraingen import (
    TerrainSpec,
    bake_terrain,
    readable_texture_meta,
    terrain_jobs_for_world,
    write_heightmap_png,
    write_splatmap_png,
)

_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "templates" / "unity" / "micro" / "terrain"
)


def _jinja() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def write_terrain_files(
    spec: TerrainSpec,
    assets_root: Path,
    *,
    project_name: str = "Project",
    scatter_details: bool = False,
    scatter_prefab: str = "Rock",
    scatter_count: int = 20,
) -> int:
    """Bake + write one region's terrain under `assets_root/Terrain/<name>/`:
    heightmap.png + splatmap.png (each with a Read/Write-enabled .meta, since
    the default Unity import — compressed, non-readable, sRGB — would
    silently corrupt the packed height/weight bytes), a runtime
    TerrainBuilder.cs, and a README. Returns the file count written.
    """
    bake = bake_terrain(spec)
    region_dir = Path(assets_root) / "Terrain" / spec.name

    res_dir = region_dir / "Resources" / "Terrain" / spec.name
    res_dir.mkdir(parents=True, exist_ok=True)
    heightmap_path = res_dir / "heightmap.png"
    splatmap_path = res_dir / "splatmap.png"
    write_heightmap_png(bake.heightmap, heightmap_path)
    write_splatmap_png(bake.splatmap, splatmap_path)

    count = 0
    for png_path in (heightmap_path, splatmap_path):
        guid = _make_guid(f"{project_name}/Terrain/{spec.name}/{png_path.stem}")
        (png_path.parent / (png_path.name + ".meta")).write_text(
            readable_texture_meta(guid), encoding="utf-8"
        )
        count += 2  # .png + its .meta

    class_name = to_pascal(spec.name, "Region") + "TerrainBuilder"
    scatter_snippet = ""
    if scatter_details:
        scatter_snippet = unity_atom_snippet(
            "ScatterTerrainDetails",
            {
                "prefab": scatter_prefab,
                "count": scatter_count,
                "xmin": spec.bounds[0], "zmin": spec.bounds[1],
                "xmax": spec.bounds[2], "zmax": spec.bounds[3],
                "seed": spec.seed,
                "terrain_name": spec.name,
            },
        )

    ctx: dict[str, Any] = {
        "name": spec.name,
        "class_name": class_name,
        "biome": spec.biome,
        "bounds": spec.bounds,
        "width": spec.width,
        "length": spec.length,
        "height_scale": spec.height_scale,
        "resolution": spec.resolution,
        "seed": spec.seed,
        "layers": bake.layers,
        "scatter_snippet": scatter_snippet,
    }

    env = _jinja()
    scripts_dir = region_dir / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cs_path = scripts_dir / f"{class_name}.cs"
    cs_path.write_text(env.get_template("TerrainBuilder.cs.j2").render(**ctx), encoding="utf-8")
    guid = _make_guid(f"{project_name}/Terrain/{spec.name}/{class_name}")
    (scripts_dir / f"{class_name}.cs.meta").write_text(_meta_file(guid), encoding="utf-8")
    count += 2  # .cs + its .meta

    (region_dir / "README.md").write_text(
        env.get_template("README.md.j2").render(**ctx), encoding="utf-8"
    )
    count += 1

    return count


def write_world_terrain(
    world: Any,
    assets_root: Path,
    *,
    project_name: str = "Project",
    project_seed: int = 0,
    scatter_details: bool = False,
) -> dict[str, int]:
    """One write_terrain_files() call per region terrain_jobs_for_world(world)
    returns. Returns {region_name: file_count} — empty (never a partial
    write) when the world declares no regions with ground, per
    terrain_jobs_for_world's abstain-loudly rules."""
    results: dict[str, int] = {}
    for spec in terrain_jobs_for_world(world, project_seed=project_seed):
        results[spec.name] = write_terrain_files(
            spec, assets_root, project_name=project_name, scatter_details=scatter_details
        )
    return results
