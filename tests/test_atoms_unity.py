"""
Tests for the Unity atom backend snippet layer (src/atoms/backends/unity.py).
"""
from __future__ import annotations

from src.atoms.backends.unity import _camera_shake


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
