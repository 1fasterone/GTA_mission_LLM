"""
Terrain atoms — Unity-only runtime primitive for scattering content across a
generated Terrain.

Heightmap/splatmap generation (src/unity_terraingen.py) is an EDITOR-TIME
asset-baking step, not a runtime coroutine, so it is deliberately NOT modeled
as an atom here — every other atom in this registry is a yielding coroutine
body (`unity_atom_snippet`'s docstring: "self-contained... composes cleanly
inside a StartCoroutine"), and baking a heightmap into TerrainData has no
runtime-coroutine shape to give it. Only the one genuinely runtime action —
scattering detail props across the baked terrain at Start — belongs here.

Domain: Unity ONLY (engines=["unity"]). GTA5 has no Terrain/TerrainData API;
its equivalent (if ever needed) belongs in backends/gta5.py, not here.
"""
from .base import AtomDef, AtomParam

TERRAIN_ATOMS: list[AtomDef] = [
    AtomDef(
        name="ScatterTerrainDetails",
        category="terrain",
        description=(
            "Instantiate a prefab at N seeded-random positions within a "
            "rectangular bounds, each snapped to Terrain.activeTerrain's "
            "sampled height. Runs once at Start — the terrain is already "
            "baked (TerrainBuilder has already run) by the time this fires."
        ),
        engines=["unity"],
        duration="instant",
        side_effects=["terrain_populated_with_props"],
        params=[
            AtomParam(name="prefab", type="string", default="Rock",
                      description="Resources/Terrain/<prefab> to instantiate"),
            AtomParam(name="count",  type="int", default=20,
                      description="How many instances to scatter"),
            AtomParam(name="xmin", type="float", default=0.0),
            AtomParam(name="zmin", type="float", default=0.0),
            AtomParam(name="xmax", type="float", default=100.0),
            AtomParam(name="zmax", type="float", default=100.0),
            AtomParam(name="seed", type="int", default=0,
                      description="Deterministic scatter seed — same seed, "
                                  "same layout, every build"),
            AtomParam(name="terrain_name", type="string", default="",
                      description="GameObject name of the Terrain to scatter "
                                  "onto; empty string falls back to "
                                  "Terrain.activeTerrain. Named lookup exists "
                                  "because a multi-region world can compose "
                                  "more than one Terrain GameObject in the "
                                  "same scene, and 'the active one' is "
                                  "ambiguous once there's more than one."),
        ],
    ),
]
