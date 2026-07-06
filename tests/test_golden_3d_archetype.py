"""
Golden 3D archetype contract tests.

subsystems_for() already emits a fixed "always-on" core for any plain 3D
project (see GOLDEN_3D_CORE_SIGNATURE in src/unity_compose.py). These tests
lock that composition down as a regression baseline: future subsystems_for()
changes must not silently drift the core kinds/variants a plain 3D project
resolves to, or reorder them (order feeds positional scene fileIDs).
"""
from __future__ import annotations

from src.unity_compose import GOLDEN_3D_CORE_SIGNATURE, subsystems_for


def _core_signature(features=None, **kw) -> list[tuple[str, str]]:
    subs = subsystems_for("3D", features, locomotion="walk", genre="universal", **kw)
    return [(s.kind, s.variant) for s in subs[: len(GOLDEN_3D_CORE_SIGNATURE)]]


def test_golden_3d_core_signature_matches_subsystems_for():
    assert _core_signature(features=[]) == list(GOLDEN_3D_CORE_SIGNATURE)


def test_golden_3d_core_is_order_stable():
    a = subsystems_for("3D", [], locomotion="walk", genre="universal")
    b = subsystems_for("3D", [], locomotion="walk", genre="universal")
    tup = lambda subs: [(s.kind, s.variant, s.target, s.name) for s in subs]
    assert tup(a) == tup(b)


def test_golden_3d_core_survives_empty_vs_none_features():
    assert _core_signature(features=None) == _core_signature(features=[])
