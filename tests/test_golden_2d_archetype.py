"""
Golden 2D archetype contract tests — mirrors test_golden_3d_archetype.py.

subsystems_for() emits the same 12 always-on kinds for 2D as for 3D, just with
dimension-appropriate variants (2d_platformer/follow_2d/rigidbody_2d). These
tests lock that composition down as a regression baseline. 2.5D is NOT given
its own signature here — the current mapper resolves "2d" and "2.5d" to the
identical controller/camera/physics variants (see GOLDEN_2D_CORE_SIGNATURE's
docstring in src/unity_compose.py), so a 2.5D archetype would just duplicate
this one until the mapper actually distinguishes them.
"""
from __future__ import annotations

from src.unity_compose import GOLDEN_2D_CORE_SIGNATURE, subsystems_for


def _core_signature(features=None, **kw) -> list[tuple[str, str]]:
    subs = subsystems_for("2D", features, locomotion="walk", genre="universal", **kw)
    return [(s.kind, s.variant) for s in subs[: len(GOLDEN_2D_CORE_SIGNATURE)]]


def test_golden_2d_core_signature_matches_subsystems_for():
    assert _core_signature(features=[]) == list(GOLDEN_2D_CORE_SIGNATURE)


def test_golden_2d_core_is_order_stable():
    a = subsystems_for("2D", [], locomotion="walk", genre="universal")
    b = subsystems_for("2D", [], locomotion="walk", genre="universal")
    tup = lambda subs: [(s.kind, s.variant, s.target, s.name) for s in subs]
    assert tup(a) == tup(b)


def test_golden_2d_core_survives_empty_vs_none_features():
    assert _core_signature(features=None) == _core_signature(features=[])


def test_golden_2_5d_currently_matches_2d_signature():
    """2.5D has no distinct archetype yet — documents current behavior so a
    future intentional split shows up as a deliberate test change, not drift."""
    core_25d = [(s.kind, s.variant)
                for s in subsystems_for("2.5D", [], locomotion="walk", genre="universal")
                [: len(GOLDEN_2D_CORE_SIGNATURE)]]
    assert core_25d == list(GOLDEN_2D_CORE_SIGNATURE)
