"""src/atoms/terrain.py — the one genuinely runtime terrain atom
(ScatterTerrainDetails). Heightmap/splatmap BAKING is deliberately NOT an
atom (see the module's own docstring) — these tests only cover what IS."""
from __future__ import annotations

import pytest

from src.atoms import ATOM_REGISTRY, CATEGORIES, get_atom, list_atoms
from src.atoms.backends.gta5 import gta5_atom_snippet
from src.atoms.backends.unity import unity_atom_snippet


class TestRegistration:
    def test_scatter_terrain_details_is_registered(self):
        atom = get_atom("ScatterTerrainDetails")
        assert atom is not None
        assert atom.category == "terrain"

    def test_terrain_category_is_declared(self):
        assert "terrain" in CATEGORIES

    def test_list_atoms_by_terrain_category_finds_it(self):
        names = {a.name for a in list_atoms("terrain")}
        assert names == {"ScatterTerrainDetails"}

    def test_it_is_unity_only(self):
        atom = ATOM_REGISTRY["ScatterTerrainDetails"]
        assert atom.engines == ["unity"]


class TestUnityBackend:
    def test_default_args_produce_activeterrain_lookup(self):
        snippet = unity_atom_snippet("ScatterTerrainDetails", {})
        assert "Terrain.activeTerrain" in snippet
        assert "GameObject.Find" not in snippet

    def test_terrain_name_switches_to_a_named_lookup(self):
        snippet = unity_atom_snippet("ScatterTerrainDetails", {"terrain_name": "Alpha"})
        assert 'GameObject.Find("Alpha")' in snippet
        assert "Terrain.activeTerrain" not in snippet

    def test_bounds_and_count_are_interpolated(self):
        snippet = unity_atom_snippet("ScatterTerrainDetails", {
            "prefab": "Boulder", "count": 5,
            "xmin": 1, "zmin": 2, "xmax": 3, "zmax": 4, "seed": 99,
        })
        assert "Terrain/Boulder" in snippet
        assert "i < 5" in snippet
        assert "new System.Random(99)" in snippet


class TestGta5BackendIsolation:
    def test_gta5_has_no_snippet_and_fails_loudly_not_silently(self):
        """FAIL-FAST doctrine (src/atoms/backends/gta5.py's module docstring):
        an atom either has a real GTA5 snippet or its `engines` excludes
        gta5 — ScatterTerrainDetails does the latter, so calling the GTA5
        backend on it must raise, never emit a silent comment stub."""
        with pytest.raises(ValueError, match="Unity-only"):
            gta5_atom_snippet("ScatterTerrainDetails", {})
