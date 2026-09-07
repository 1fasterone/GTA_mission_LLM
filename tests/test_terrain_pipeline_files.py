"""src/micro/terrain.py — the write-to-disk layer over unity_terraingen.py's
pure functions. Verifies the on-disk shape (Resources/Scripts/README, .meta
files) and that the generated C# actually parses, via the same Roslyn-backed
gate the real --compose pipeline runs before handing a project back
(src/csharp_syntax_gate.py) — the strongest check available without a live
Unity Editor."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from src.csharp_syntax_gate import enforce
from src.micro.terrain import write_terrain_files, write_world_terrain
from src.unity_schema import WorldRegion, WorldSpec
from src.unity_terraingen import TerrainSpec

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("dotnet"),
    reason="C# syntax gate needs the .NET SDK — falls back to a no-op without it, "
           "so skip rather than pass on a check that silently didn't run",
)


def _spec(**kw) -> TerrainSpec:
    base = dict(name="AlphaRegion", bounds=(0.0, 0.0, 100.0, 100.0), biome="forest", seed=11)
    base.update(kw)
    return TerrainSpec(**base)


class TestWriteTerrainFiles:
    def test_writes_the_expected_file_set(self, tmp_path: Path):
        n = write_terrain_files(_spec(), tmp_path)
        region_dir = tmp_path / "Terrain" / "AlphaRegion"
        expected = {
            region_dir / "README.md",
            region_dir / "Resources" / "Terrain" / "AlphaRegion" / "heightmap.png",
            region_dir / "Resources" / "Terrain" / "AlphaRegion" / "heightmap.png.meta",
            region_dir / "Resources" / "Terrain" / "AlphaRegion" / "splatmap.png",
            region_dir / "Resources" / "Terrain" / "AlphaRegion" / "splatmap.png.meta",
            region_dir / "Scripts" / "AlphaRegionTerrainBuilder.cs",
            region_dir / "Scripts" / "AlphaRegionTerrainBuilder.cs.meta",
        }
        for path in expected:
            assert path.is_file(), f"missing {path}"
        assert n == len(expected)

    def test_heightmap_png_is_a_readable_rgba_image_of_the_right_size(self, tmp_path: Path):
        write_terrain_files(_spec(resolution=65), tmp_path)
        png = tmp_path / "Terrain" / "AlphaRegion" / "Resources" / "Terrain" / "AlphaRegion" / "heightmap.png"
        img = Image.open(png)
        assert img.mode == "RGBA"
        assert img.size == (65, 65)

    def test_meta_files_force_readable_uncompressed_import(self, tmp_path: Path):
        write_terrain_files(_spec(), tmp_path)
        meta = (tmp_path / "Terrain" / "AlphaRegion" / "Resources" / "Terrain"
                / "AlphaRegion" / "heightmap.png.meta").read_text(encoding="utf-8")
        assert "isReadable: 1" in meta
        assert "sRGBTexture: 0" in meta
        assert "textureCompression: 0" in meta

    def test_generated_terrain_builder_parses_as_valid_csharp(self, tmp_path: Path):
        write_terrain_files(_spec(), tmp_path)
        cs = tmp_path / "Terrain" / "AlphaRegion" / "Scripts" / "AlphaRegionTerrainBuilder.cs"
        enforce([cs], enabled=True, label="terrain test (no scatter)")

    def test_generated_terrain_builder_with_scatter_details_parses(self, tmp_path: Path):
        write_terrain_files(_spec(), tmp_path, scatter_details=True,
                            scatter_prefab="Boulder", scatter_count=12)
        cs = tmp_path / "Terrain" / "AlphaRegion" / "Scripts" / "AlphaRegionTerrainBuilder.cs"
        text = cs.read_text(encoding="utf-8")
        assert "RunDetailScatter" in text
        assert "Boulder" in text
        enforce([cs], enabled=True, label="terrain test (scatter)")

    def test_two_layer_biome_parses_too(self, tmp_path: Path):
        write_terrain_files(_spec(biome="desert"), tmp_path)   # sand, rock = 2 layers
        cs = tmp_path / "Terrain" / "AlphaRegion" / "Scripts" / "AlphaRegionTerrainBuilder.cs"
        enforce([cs], enabled=True, label="terrain test (2-layer biome)")

    def test_class_name_is_a_valid_csharp_identifier_for_a_snake_case_region(self, tmp_path: Path):
        write_terrain_files(_spec(name="north_ridge"), tmp_path)
        cs_files = list((tmp_path / "Terrain" / "north_ridge" / "Scripts").glob("*.cs"))
        assert len(cs_files) == 1
        assert cs_files[0].stem == "NorthRidgeTerrainBuilder"
        enforce(cs_files, enabled=True, label="terrain test (snake_case name)")


class TestInPlaceDetection:
    """Regression coverage for the `in_place` flag (fix/d721f59): whether the
    README tells the reader to copy the Terrain/ folder into their project or
    warns them it is already there. Getting this wrong in the "already in
    place" direction is exactly how a second copy of <class_name>.cs lands
    under Assets/Scripts/ and Unity throws CS0101 on the duplicate class."""

    def _readme(self, tmp_path: Path, region: str = "AlphaRegion") -> str:
        # Collapse whitespace so a phrase that happens to wrap across a
        # template line break still matches a single-line assertion.
        text = (tmp_path / "Terrain" / region / "README.md").read_text(encoding="utf-8")
        return " ".join(text.split())

    def test_standalone_output_dir_is_not_in_place(self, tmp_path: Path):
        # e.g. `unity-terrain-gen`'s default -o output/unity/terrain — no
        # "Assets" component anywhere in the path.
        out = tmp_path / "output" / "unity" / "terrain"
        write_terrain_files(_spec(), out)
        text = self._readme(out)
        assert "already in place" not in text
        assert "Copy the whole" in text

    def test_assets_root_named_assets_is_in_place(self, tmp_path: Path):
        # The --compose pipeline's call shape: project_root / "Assets".
        assets_root = tmp_path / "MyGame" / "Assets"
        write_terrain_files(_spec(), assets_root)
        text = self._readme(assets_root)
        assert "already in place" in text
        assert "Do not copy" in text

    def test_subfolder_already_inside_assets_is_also_in_place(self, tmp_path: Path):
        # A caller pointed at a location already nested inside Assets/ (not
        # Assets/ itself) must still be treated as in place — a basename-only
        # check would miss this and tell the reader to copy it again,
        # landing a second copy right back inside the same Assets/ tree.
        assets_root = tmp_path / "MyGame" / "Assets" / "Generated"
        write_terrain_files(_spec(), assets_root)
        text = self._readme(assets_root)
        assert "already in place" in text

    def test_folder_merely_containing_the_word_assets_is_not_in_place(self, tmp_path: Path):
        # "AssetsBackup" is not the literal Unity "Assets" folder — must not
        # false-positive on a substring match.
        out = tmp_path / "AssetsBackup" / "terrain"
        write_terrain_files(_spec(), out)
        text = self._readme(out)
        assert "already in place" not in text


class TestWriteWorldTerrain:
    def test_writes_one_region_per_job_and_skips_no_ground_ones(self, tmp_path: Path):
        world = WorldSpec(regions=[
            WorldRegion(name="Interior", biome="interior", bounds=[0, 0, 10, 10]),
            WorldRegion(name="Outside", biome="forest", bounds=[0, 0, 50, 50]),
        ])
        results = write_world_terrain(world, tmp_path, project_name="TestProj")
        assert list(results) == ["Outside"]
        assert (tmp_path / "Terrain" / "Outside" / "README.md").is_file()
        assert not (tmp_path / "Terrain" / "Interior").exists()

    def test_empty_world_writes_nothing(self, tmp_path: Path):
        assert write_world_terrain(WorldSpec(regions=[]), tmp_path) == {}
        assert not (tmp_path / "Terrain").exists()

    def test_same_project_name_reproduces_identical_bytes(self, tmp_path: Path):
        world = WorldSpec(regions=[WorldRegion(name="R", biome="forest", bounds=[0, 0, 40, 40])])
        from src.unity_terraingen import project_seed_from_name
        seed = project_seed_from_name("SameGame")

        d1, d2 = tmp_path / "a", tmp_path / "b"
        write_world_terrain(world, d1, project_name="SameGame", project_seed=seed)
        write_world_terrain(world, d2, project_name="SameGame", project_seed=seed)
        h1 = (d1 / "Terrain" / "R" / "Resources" / "Terrain" / "R" / "heightmap.png").read_bytes()
        h2 = (d2 / "Terrain" / "R" / "Resources" / "Terrain" / "R" / "heightmap.png").read_bytes()
        assert h1 == h2
