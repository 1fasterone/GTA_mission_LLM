"""
Tests for the Unity atom backend snippet layer (src/atoms/backends/unity.py).
"""
from __future__ import annotations

from src.atoms.backends.unity import _camera_shake, _scatter_terrain_details


def test_camera_shake_uses_real_cinemachine_namespace():
    # GameSequencer.cs(102,86) CS0234, live: CinemachineImpulseSource was
    # qualified under UnityEngine.Rendering.Universal — a plausible-but-wrong
    # URP namespace. The type ships in Cinemachine's OWN package/namespace
    # (Unity.Cinemachine, the 3.x name this project's Unity 6000.x pin
    # resolves to), never URP's. This snippet is emitted by the deterministic
    # atom backend directly (never an LLM-authored fragment), so nothing
    # downstream sanitizes it — it must be correct at the source.
    snippet = _camera_shake({"intensity": 1.0, "duration": 0.3})
    assert "UnityEngine.Rendering.Universal.CinemachineImpulseSource" not in snippet
    assert "Unity.Cinemachine.CinemachineImpulseSource" in snippet


def test_scatter_terrain_details_defaults_to_active_terrain():
    # No terrain_name bound: must look up Terrain.activeTerrain rather than
    # a hardcoded/missing name lookup.
    snippet = _scatter_terrain_details({})
    assert "var _terrain = Terrain.activeTerrain;" in snippet
    assert "GameObject.Find" not in snippet


def test_scatter_terrain_details_looks_up_named_terrain():
    # A bound terrain_name must route through GameObject.Find + GetComponent
    # instead of the Terrain.activeTerrain default.
    snippet = _scatter_terrain_details({"terrain_name": "HallOneGround"})
    assert 'GameObject.Find("HallOneGround")?.GetComponent<Terrain>()' in snippet
    assert "Terrain.activeTerrain" not in snippet


def test_scatter_terrain_details_binds_prefab_count_and_bounds():
    snippet = _scatter_terrain_details({
        "prefab": "Boulder", "count": 12, "seed": 7,
        "xmin": 5, "xmax": 50, "zmin": -10, "zmax": 40,
    })
    assert 'Resources.Load<GameObject>("Terrain/Boulder")' in snippet
    assert "new System.Random(7)" in snippet
    assert "for (int i = 0; i < 12; i++)" in snippet
    assert "50.0f - 5.0f" in snippet
    assert "40.0f - -10.0f" in snippet


def test_scatter_terrain_details_null_guards_terrain_and_prefab():
    # Terrain.SampleHeight/Instantiate must never run against a missing
    # terrain or an unresolved Resources.Load prefab.
    snippet = _scatter_terrain_details({})
    assert "if (_terrain != null && _detailPrefab != null)" in snippet
    assert "SampleHeight" in snippet
    assert "Instantiate(_detailPrefab" in snippet
