"""
Non-embodied games — a player who never moves is not a broken game.

The compiler carried one implicit genre axiom: the player is an avatar moving
through space. It shows up in `locomotion` (four values, all movement), in five
spatial primitives that are all movement spaces, and in an ERROR-class `avatar`
check. A live prompt — "build a visual game that teaches the github code
repository E2E" — produced a coherent 2D top-down teaching sim whose concept
explicitly excluded character movement, and Step 7 graded it 81/100 NOT PLAYABLE
for lacking an avatar it correctly should not have.

`locomotion="none"` is the positive declaration that closes that gap, and these
tests pin the property that makes it safe: it must be DECLARED, never inferred.
A schema-fill run that dropped its avatar looks identical to one that never
wanted an avatar, and this evaluator exists because the ToyRacer and DropZone
runs did the former.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import pytest

from src.unity_archetypes import apply_intent, classify
from src.unity_playability import (_EMBODIED_ONLY_CHECKS, evaluate, gate,
                                   is_embodied)
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              UnitySchema, UnityScript)


def _ui_game(**kw) -> UnitySchema:
    """The CodeVault shape: every entity a prop, no avatar, no player script."""
    base = dict(
        project_name="CodeVault", game_type="2d_topdown", platform="pc",
        render_pipeline="URP", locomotion="none",
        entities=[EntitySpec(name="FileIcons", kind="prop", tag="FileIcon", count=8),
                  EntitySpec(name="TerminalWindow", kind="prop", tag="Terminal")],
        scripts=[UnityScript(name="FileDragHandler", type="custom", description="d"),
                 UnityScript(name="GameManager", type="game_manager", description="d")],
        # The real CodeVault run authored a mission; a UI game still needs a
        # goal, and `objective` is deliberately NOT one of the checks that
        # abstains.
        missions=[MissionSpec(id="m1", title="First Commit", objectives=[
            MissionObjective(id="obj_1", description="Stage three files",
                             type="collect", count=3)])],
    )
    base.update(kw)
    return UnitySchema(**base)


# ── The declaration ───────────────────────────────────────────────────────────

def test_locomotion_none_is_a_valid_declaration():
    assert _ui_game().locomotion == "none"
    assert is_embodied(_ui_game()) is False


@pytest.mark.parametrize("loco", ["walk", "drive", "fly", "swim"])
def test_every_moving_game_is_still_embodied(loco):
    assert is_embodied(_ui_game(locomotion=loco)) is True


def test_non_embodiment_is_never_inferred_from_a_missing_avatar():
    """The load-bearing property. An avatar the schema-fill DROPPED must still
    be an error — otherwise the check is vacuous on exactly the builds it
    exists to catch."""
    lost_avatar = _ui_game(locomotion="walk")     # no Player tag, never declared
    rep = evaluate(lost_avatar)
    assert rep.checks["avatar"] is False
    assert not rep.playable
    assert any(f.check == "avatar" for f in rep.findings)


# ── What abstention does and does not change ─────────────────────────────────

def test_declared_ui_game_is_not_failed_for_having_no_avatar():
    rep = evaluate(_ui_game())
    assert "avatar" not in rep.checks
    assert not any(f.check == "avatar" for f in rep.findings)
    assert rep.playable


@pytest.mark.parametrize("check", sorted(_EMBODIED_ONLY_CHECKS))
def test_every_embodied_check_records_why_it_abstained(check):
    """Ungraded, not passed — absence of evidence is never evidence of
    playability, so these still show on the scorecard."""
    rep = evaluate(_ui_game())
    assert check not in rep.checks
    assert check in rep.not_graded
    assert "locomotion='none'" in rep.not_graded[check]


def test_abstention_still_fails_a_gate_that_requires_grading():
    """The eval harness locks its floors with require_graded=True; a declared
    UI game must not sneak past it by having nothing graded."""
    rep = evaluate(_ui_game())
    assert gate(rep, require_graded=True) is not None


def test_non_spatial_checks_are_still_graded():
    """Only the spatial half abstains. Objectives, audio and scene population
    mean exactly as much for a UI game as for a platformer."""
    rep = evaluate(_ui_game())
    assert "objective" in rep.checks
    for name in rep.checks:
        assert name not in _EMBODIED_ONLY_CHECKS


def test_a_ui_game_with_nothing_to_do_still_fails():
    """Abstaining on the avatar must not abstain on the game."""
    rep = evaluate(_ui_game(entities=[EntitySpec(name="Backdrop", kind="prop")],
                            scripts=[], story_beats=[], missions=[]))
    assert rep.checks["objective"] is False
    assert not rep.playable


# ── The declaration is sticky ─────────────────────────────────────────────────

def test_a_stray_movement_keyword_cannot_flip_the_declaration():
    """"walk through the commit history" is a metaphor. The schema-fill read the
    whole brief; the classifier reads keywords."""
    schema = _ui_game()
    apply_intent(schema, classify(
        "a game where you walk through the commit history and run around "
        "the branch tree on foot"))
    assert schema.locomotion == "none"


def test_the_classifier_still_corrects_an_embodied_game():
    schema = _ui_game(locomotion="walk")
    apply_intent(schema, classify(
        "you drive a rally car around a frozen lake circuit, driving the "
        "racing line between corners in your car"))
    assert schema.locomotion == "drive"
