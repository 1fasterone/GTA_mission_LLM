"""
Golden 2D / 2.5D / 2D-top-down archetype contract tests — mirrors test_golden_3d_archetype.py.

subsystems_for() emits the same 12 always-on kinds for 2D/2.5D/2d_topdown as for
3D, just with dimension-appropriate variants. 2D gets 2d_platformer/follow_2d/
rigidbody_2d (Rigidbody2D world, gravity+jump). 2.5D gets its OWN signature:
platformer_2_5d/follow_2_5d/rigidbody_3d — a real 3D CharacterController + world
(matching unity_asset_resolver.py's "2.5D can use 3D models" context) constrained
to a lateral movement plane, not a Rigidbody2D game. 2d_topdown gets its OWN
signature too: 2d_topdown/follow_2d/rigidbody_2d — flat Rigidbody2D like 2D, but
free 2-axis movement (no gravity/jump). These lock all three baselines down as
regression tests.
"""
from __future__ import annotations

from src.unity_compose import (
    GOLDEN_2D_CORE_SIGNATURE, GOLDEN_2_5D_CORE_SIGNATURE,
    GOLDEN_2D_TOPDOWN_CORE_SIGNATURE, subsystems_for,
)


def _core_signature(game_type: str, golden, features=None, **kw) -> list[tuple[str, str]]:
    subs = subsystems_for(game_type, features, locomotion="walk", genre="universal", **kw)
    return [(s.kind, s.variant) for s in subs[: len(golden)]]


def test_golden_2d_core_signature_matches_subsystems_for():
    assert _core_signature("2D", GOLDEN_2D_CORE_SIGNATURE, features=[]) == list(GOLDEN_2D_CORE_SIGNATURE)


def test_golden_2d_core_is_order_stable():
    a = subsystems_for("2D", [], locomotion="walk", genre="universal")
    b = subsystems_for("2D", [], locomotion="walk", genre="universal")
    tup = lambda subs: [(s.kind, s.variant, s.target, s.name) for s in subs]
    assert tup(a) == tup(b)


def test_golden_2d_core_survives_empty_vs_none_features():
    assert (_core_signature("2D", GOLDEN_2D_CORE_SIGNATURE, features=None)
            == _core_signature("2D", GOLDEN_2D_CORE_SIGNATURE, features=[]))


def test_golden_2_5d_core_signature_matches_subsystems_for():
    """2.5D now has its own distinct archetype — real 3D controller/camera/physics
    (platformer_2_5d / follow_2_5d / rigidbody_3d), not the 2D Rigidbody2D one."""
    assert (_core_signature("2.5D", GOLDEN_2_5D_CORE_SIGNATURE, features=[])
            == list(GOLDEN_2_5D_CORE_SIGNATURE))


def test_golden_2_5d_diverges_from_2d():
    """Regression guard: 2.5D must NOT silently re-alias onto the 2D signature."""
    core_2d  = _core_signature("2D",   GOLDEN_2D_CORE_SIGNATURE,   features=[])
    core_25d = _core_signature("2.5D", GOLDEN_2_5D_CORE_SIGNATURE, features=[])
    assert core_2d != core_25d
    assert dict(core_25d)["controller"] == "platformer_2_5d"
    assert dict(core_25d)["camera"]     == "follow_2_5d"
    assert dict(core_25d)["physics"]    == "rigidbody_3d"


def test_golden_2d_topdown_core_signature_matches_subsystems_for():
    """2d_topdown has its own distinct archetype too — the "2d_topdown" controller
    (free 2-axis Rigidbody2D, no gravity/jump) already existed in
    src/micro/controller.py but was unreachable until this game_type shipped."""
    assert (_core_signature("2d_topdown", GOLDEN_2D_TOPDOWN_CORE_SIGNATURE, features=[])
            == list(GOLDEN_2D_TOPDOWN_CORE_SIGNATURE))


def test_golden_2d_topdown_diverges_from_2d_side_scroller():
    """Regression guard: 2d_topdown must use its own controller, not silently
    re-alias onto 2D's side-scroller (2d_platformer) controller."""
    core_2d  = _core_signature("2D",         GOLDEN_2D_CORE_SIGNATURE,         features=[])
    core_top = _core_signature("2d_topdown", GOLDEN_2D_TOPDOWN_CORE_SIGNATURE, features=[])
    assert core_2d != core_top
    assert dict(core_top)["controller"] == "2d_topdown"
    assert dict(core_top)["camera"]     == "follow_2d"    # same orthographic X/Y follow as 2D
    assert dict(core_top)["physics"]    == "rigidbody_2d"  # flat, like 2D — unlike 2.5D


def test_golden_2d_topdown_diverges_from_3d_top_down():
    """Regression guard: 2d_topdown must NOT alias onto the existing (real-3D)
    "top_down" game_type's CharacterController archetype."""
    core_top3d = _core_signature("top_down",    GOLDEN_2D_CORE_SIGNATURE,         features=[])
    core_top2d = _core_signature("2d_topdown",  GOLDEN_2D_TOPDOWN_CORE_SIGNATURE, features=[])
    assert dict(core_top3d)["controller"] == "top_down"
    assert dict(core_top3d)["physics"]    == "rigidbody_3d"
    assert dict(core_top2d)["controller"] == "2d_topdown"
    assert dict(core_top2d)["physics"]    == "rigidbody_2d"
