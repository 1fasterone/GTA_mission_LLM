"""The rules micro — the layer that makes a build a game.

Every other micro is a mechanism. These tests assert the three things whose
absence meant no generated project was ever playable by a human: a briefing
that states the goal, a step progression taken from the mission's own
objectives, and a hint bar that names the next action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.csharp_syntax_gate import check_files
from src.micro import REGISTRY
from src.micro.rules import RulesPipeline, RulesSchema, steps_from_mission
from src.unity_compose import subsystems_for
from src.unity_schema import MissionObjective, MissionSpec

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return {"lm_studio": {"base_url": "http://localhost:1234/v1", "model": "test"},
            "unity_pipeline": {"unity_output_dir": str(REPO / "output" / "unity"),
                               "unity_template_dir": str(REPO / "templates" / "unity")}}


@pytest.fixture
def mission():
    return MissionSpec(
        id="m1", title="First Commit",
        description="Get every change into a pushed commit.",
        objectives=[
            MissionObjective(id="obj_1", type="collect", target_ref="FileIcons",
                             description="Drag all file icons into a folder."),
            MissionObjective(id="obj_2", type="reach", target_ref="Terminal",
                             description="Type 'git commit' and press Enter."),
        ])


def test_registered():
    assert REGISTRY["rules"] is RulesPipeline


def test_a_step_per_objective_in_schema_order(mission):
    steps = steps_from_mission(mission)
    assert [s["objective_id"] for s in steps] == ["obj_1", "obj_2"]
    assert all(s["ask"] for s in steps), "every step must name an action"


def test_compose_adds_exactly_one_rules_unit(mission):
    subs = subsystems_for("3D", [], "walk", missions=[mission])
    rules = [s for s in subs if s.kind == "rules"]
    assert len(rules) == 1, "rules is per-PROJECT, not per-subsystem"
    assert rules[0].target == "ui"
    assert len(rules[0].options["steps"]) == 2


def test_no_mission_means_no_rules_unit():
    """With nothing to sequence there is no game to explain, and a briefing
    saying 'achieve the goal' is the empty scaffolding this replaces."""
    assert not [s for s in subsystems_for("3D", [], "walk") if s.kind == "rules"]
    empty = MissionSpec(id="m", title="Empty", objectives=[])
    assert not [s for s in subsystems_for("3D", [], "walk", missions=[empty])
                if s.kind == "rules"]


def test_goal_is_never_blank(cfg):
    """'What counts as winning' is the first thing a player needs and the last
    thing any build has ever stated."""
    p = RulesPipeline(cfg)
    s = p.default_fix(RulesSchema(), "guided")
    assert s.goal and s.briefing and s.outro


def test_a_step_without_an_ask_is_filled_not_shipped_blank(cfg):
    p = RulesPipeline(cfg)
    s = RulesSchema(steps=[{"objective_id": "o1", "teaches": "Staging is a shortlist."}])
    s = p.default_fix(s, "guided")
    assert s.steps[0].ask, "a step with no ask is a step the player cannot act on"
    assert s.steps[0].title


def test_step_pointing_at_an_unknown_objective_is_reported(cfg):
    """Such a step can never clear, so the player is stranded on it forever."""
    p = RulesPipeline(cfg)
    s = RulesSchema(steps=[{"objective_id": "nope", "title": "GHOST", "ask": "do it"}],
                    meta={"objective_ids": ["obj_1", "obj_2"]})
    msgs = p.harden(p.default_fix(s, "guided"))
    assert any("never clear" in m for m in msgs), msgs


def test_emitted_csharp_parses_and_carries_the_player_facing_text(cfg, mission, tmp_path):
    p = RulesPipeline(cfg)
    s = RulesSchema(game_title="CodeVault", goal="Push a commit.",
                    steps=steps_from_mission(mission))
    s = p.default_fix(s, "guided")
    p.write_files(s, "guided", tmp_path)
    src = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")

    assert check_files(sorted(tmp_path.rglob("*.cs"))).ok
    assert "Push a commit." in src
    assert "obj_1" in src and "obj_2" in src
    assert "NEXT:" in src, "the hint bar must name the next action"
    assert "START" in src, "the guided variant must ship a briefing the player dismisses"


def test_hud_only_variant_drops_the_briefing(cfg, mission, tmp_path):
    p = RulesPipeline(cfg)
    s = p.default_fix(RulesSchema(variant="hud_only",
                                  steps=steps_from_mission(mission)), "hud_only")
    p.write_files(s, "hud_only", tmp_path)
    src = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "NEXT:" in src, "the hint bar survives in every variant"
    assert "Briefing" not in src


# ── Board: pieces, places and playing surfaces ───────────────────────────────

class TestBoardVocabulary:
    """A cast of pawns with nowhere to go is the same dead board as a cast of
    places with nothing to put in them. Measured before this: 27 of 39 ordinary
    board-game nouns classified as nothing, so the board micro abstained and no
    legal-move logic was composed at all."""

    @pytest.mark.parametrize("name", [
        "GamePieces", "Pawns", "Counters", "Meeples", "Chips", "Discs",
        "Stones", "Dice", "Coins", "Gems", "ChessPieces", "Cubes",
    ])
    def test_things_you_move_are_pieces(self, name):
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec
        assert ui_element_of(EntitySpec(name=name, kind="prop")) in ("icon", "card")

    @pytest.mark.parametrize("name", [
        "StagingArea", "PlayingSurface", "PlayMat", "DiscardPile", "DrawPile",
        "PlayerHand", "ScoreTrack", "StorageRack", "LoadingBay", "BuildLane",
        "ResourceBin", "TileGrid", "HexGrid", "Shelf", "Workbench",
    ])
    def test_playing_surfaces_are_places(self, name):
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec
        assert ui_element_of(EntitySpec(name=name, kind="prop")) in ("panel", "list")

    def test_a_table_top_is_a_surface_not_a_log(self):
        """First-row-wins resolved 'table top' through `table` in the list row,
        so a surface you drop pieces onto became a read-only readout."""
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec
        assert ui_element_of(EntitySpec(name="TableTop", kind="prop")) == "panel"
        # ...while a bare data table is still a list.
        assert ui_element_of(EntitySpec(name="CommitTable", kind="prop")) == "list"


class TestBoardFromObjectives:
    """The vocabulary is a closed list and a closed list can never cover every
    game. The brief does not have that problem."""

    def test_a_cast_no_word_list_can_name_still_makes_a_board(self):
        from src.micro.board import board_options_for_entities
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec, MissionObjective, MissionSpec

        ents = [EntitySpec(name="HighHeeledShoes", kind="prop", count=18),
                EntitySpec(name="WobblyTower", kind="prop", count=1)]
        assert all(ui_element_of(e) is None for e in ents), \
            "precondition: the vocabulary must NOT know these"

        m = MissionSpec(id="m1", title="Shoe Tower", objectives=[
            MissionObjective(id="o1", type="collect", target_ref="HighHeeledShoes",
                             description="Drag the high heeled shoes onto the "
                                         "wobbly tower without toppling it.")])
        opts = board_options_for_entities(ents, mission=m)
        assert opts, "the mission names what moves where — that is a board"
        assert [p["id"] for p in opts["pieces"]] == ["HighHeeledShoes"]
        assert [z["id"] for z in opts["zones"]] == ["WobblyTower"]
        assert opts["pieces"][0]["count"] == 18

    def test_no_mission_still_abstains_when_the_cast_is_unreadable(self):
        """The fallback must not invent a board out of nothing."""
        from src.micro.board import board_options_for_entities
        from src.unity_schema import EntitySpec
        assert board_options_for_entities(
            [EntitySpec(name="HighHeeledShoes", kind="prop", count=18)]) is None

    def test_an_objective_with_no_move_verb_contributes_nothing(self):
        from src.micro.board import board_options_for_entities
        from src.unity_schema import EntitySpec, MissionObjective, MissionSpec
        m = MissionSpec(id="m1", title="Watch", objectives=[
            MissionObjective(id="o1", type="survive",
                             description="Observe the wobbly tower for 60 seconds.")])
        assert board_options_for_entities(
            [EntitySpec(name="HighHeeledShoes", kind="prop"),
             EntitySpec(name="WobblyTower", kind="prop")], mission=m) is None
