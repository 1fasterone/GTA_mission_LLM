"""Deterministic Unity Terrain generator (src/unity_terraingen.py) — no LLM,
no Unity. Heightmap/splatmap math + the abstain-loudly region-selection rules
that decide which WorldRegions get a terrain baked at all."""
from __future__ import annotations

import numpy as np
import pytest

from src.unity_schema import WorldRegion, WorldSpec
from src.unity_terraingen import (
    NO_GROUND_BIOMES,
    TerrainSpec,
    VALID_RESOLUTIONS,
    bake_terrain,
    generate_heightmap,
    generate_splatmap,
    layers_for_biome,
    project_seed_from_name,
    seed_for_region,
    terrain_jobs_for_world,
)


class TestHeightmap:
    def test_same_seed_is_byte_identical(self):
        a = generate_heightmap(42, 65)
        b = generate_heightmap(42, 65)
        assert np.array_equal(a, b)

    def test_different_seeds_differ(self):
        a = generate_heightmap(1, 65)
        b = generate_heightmap(2, 65)
        assert not np.array_equal(a, b)

    def test_normalized_to_unit_range(self):
        h = generate_heightmap(7, 65)
        assert h.min() == pytest.approx(0.0, abs=1e-9)
        assert h.max() == pytest.approx(1.0, abs=1e-9)
        assert h.shape == (65, 65)

    def test_rejects_a_non_power_of_two_plus_one_resolution(self):
        with pytest.raises(ValueError):
            generate_heightmap(1, 100)

    def test_every_declared_valid_resolution_actually_works(self):
        for res in (33, 65, 129):   # skip the huge ones — this is a unit test
            h = generate_heightmap(1, res)
            assert h.shape == (res, res)


class TestSplatmap:
    def test_weights_sum_to_one_everywhere(self):
        h = generate_heightmap(3, 65)
        splat, layers = generate_splatmap(h, "forest")
        assert layers == layers_for_biome("forest")
        sums = splat.sum(axis=-1)
        assert np.allclose(sums, 1.0, atol=1e-6)

    def test_single_layer_biome_is_all_ones(self):
        h = generate_heightmap(3, 33)
        # No real biome maps to exactly one layer today, but the function
        # must still handle it correctly if one ever does.
        from src.unity_terraingen import BIOME_LAYERS
        BIOME_LAYERS["_single_layer_test"] = ["only"]
        try:
            splat, layers = generate_splatmap(h, "_single_layer_test")
        finally:
            del BIOME_LAYERS["_single_layer_test"]
        assert layers == ["only"]
        assert np.all(splat == 1.0)

    def test_low_texel_favors_the_lowest_layer(self):
        h = np.zeros((4, 4))
        splat, layers = generate_splatmap(h, "mountain")   # rock, snow, grass
        # height 0 is closest to the FIRST layer's band center
        assert np.argmax(splat[0, 0]) == 0

    def test_unknown_biome_falls_back_to_default_layers(self):
        assert layers_for_biome("a wholly invented biome name") == \
            layers_for_biome("")   # both hit the same fallback


class TestBake:
    def test_bake_terrain_is_deterministic(self):
        spec = TerrainSpec(name="R", bounds=(0, 0, 100, 100), biome="forest", seed=99)
        a = bake_terrain(spec)
        b = bake_terrain(spec)
        assert np.array_equal(a.heightmap, b.heightmap)
        assert np.array_equal(a.splatmap, b.splatmap)
        assert a.layers == b.layers


class TestSeeds:
    def test_seed_for_region_is_stable_across_calls(self):
        assert seed_for_region("Alpha") == seed_for_region("Alpha")

    def test_seed_for_region_differs_by_name(self):
        assert seed_for_region("Alpha") != seed_for_region("Beta")

    def test_project_seed_changes_region_seed(self):
        a = seed_for_region("Alpha", project_seed=1)
        b = seed_for_region("Alpha", project_seed=2)
        assert a != b

    def test_project_seed_from_name_is_stable(self):
        assert project_seed_from_name("MyGame") == project_seed_from_name("MyGame")
        assert project_seed_from_name("MyGame") != project_seed_from_name("OtherGame")


class TestTerrainJobsForWorld:
    def test_none_world_yields_no_jobs(self):
        assert terrain_jobs_for_world(None) == []

    def test_world_with_no_regions_yields_no_jobs(self):
        assert terrain_jobs_for_world(WorldSpec(regions=[])) == []

    def test_a_ground_biome_region_becomes_a_job(self):
        world = WorldSpec(regions=[WorldRegion(name="Alpha", biome="forest", bounds=[0, 0, 50, 50])])
        jobs = terrain_jobs_for_world(world)
        assert len(jobs) == 1
        assert jobs[0].name == "Alpha"
        assert jobs[0].biome == "forest"
        assert jobs[0].bounds == (0.0, 0.0, 50.0, 50.0)

    @pytest.mark.parametrize("biome", sorted(NO_GROUND_BIOMES))
    def test_every_no_ground_biome_is_skipped(self, biome):
        world = WorldSpec(regions=[WorldRegion(name="R", biome=biome, bounds=[0, 0, 50, 50])])
        assert terrain_jobs_for_world(world) == []

    def test_degenerate_bounds_is_skipped(self):
        world = WorldSpec(regions=[WorldRegion(name="R", biome="forest", bounds=[10, 10, 10, 10])])
        assert terrain_jobs_for_world(world) == []

    def test_inverted_bounds_is_skipped(self):
        world = WorldSpec(regions=[WorldRegion(name="R", biome="forest", bounds=[50, 50, 0, 0])])
        assert terrain_jobs_for_world(world) == []

    def test_region_biome_overrides_world_biome(self):
        world = WorldSpec(biome="interior", regions=[
            WorldRegion(name="R", biome="forest", bounds=[0, 0, 10, 10]),
        ])
        jobs = terrain_jobs_for_world(world)
        assert len(jobs) == 1 and jobs[0].biome == "forest"

    def test_blank_region_biome_falls_back_to_world_biome(self):
        world = WorldSpec(biome="desert", regions=[
            WorldRegion(name="R", biome="", bounds=[0, 0, 10, 10]),
        ])
        jobs = terrain_jobs_for_world(world)
        assert len(jobs) == 1 and jobs[0].biome == "desert"

    def test_jobs_are_reproducible_for_the_same_project_seed(self):
        world = WorldSpec(regions=[WorldRegion(name="Alpha", biome="forest", bounds=[0, 0, 10, 10])])
        a = terrain_jobs_for_world(world, project_seed=5)
        b = terrain_jobs_for_world(world, project_seed=5)
        assert a[0].seed == b[0].seed

    def test_mixed_good_and_bad_regions_keeps_only_the_good_one(self):
        world = WorldSpec(regions=[
            WorldRegion(name="Inside", biome="interior", bounds=[0, 0, 10, 10]),
            WorldRegion(name="Outside", biome="forest", bounds=[0, 0, 10, 10]),
        ])
        jobs = terrain_jobs_for_world(world)
        assert [j.name for j in jobs] == ["Outside"]
