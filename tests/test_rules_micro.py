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
from src.micro.rules import (RulesPipeline, RulesSchema, steps_from_mission,
                             steps_from_missions)
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


# ── Every mission, not just the first ────────────────────────────────────────

@pytest.fixture
def three_missions():
    """The RepoQuest shape: three missions, six objectives. SEAM 8a binds an
    objective from any of them, so a progression that stopped at the first told
    the player about two of the six that were live in the build."""
    return [
        MissionSpec(id="m1", title="Connect the Forest",
                    description="Build a complete path from root to branch.",
                    objectives=[
                        MissionObjective(id="obj_1", type="collect",
                                         description="Select at least 5 nodes."),
                        MissionObjective(id="obj_2", type="reach",
                                         description="Connect the selected nodes.")]),
        MissionSpec(id="m2", title="Initialize Pipeline",
                    description="Configure the first build station.",
                    objectives=[
                        MissionObjective(id="obj_3", type="collect",
                                         description="Place an artifact on the belt."),
                        MissionObjective(id="obj_4", type="survive", count=60,
                                         description="Ensure no errors appear.")]),
        MissionSpec(id="m3", title="Merge Resolution",
                    description="Align conflicting code blocks.",
                    objectives=[
                        MissionObjective(id="obj_5", type="reach",
                                         description="Align the conflict markers.")]),
    ]


def test_the_progression_spans_every_mission(three_missions):
    steps = steps_from_missions(three_missions)
    assert [s["objective_id"] for s in steps] == ["obj_1", "obj_2", "obj_3",
                                                  "obj_4", "obj_5"]
    assert steps[0]["title"].startswith("1 / 5")
    assert steps[-1]["title"].startswith("5 / 5"), "numbering runs to the total"


def test_a_step_names_the_phase_it_belongs_to(three_missions):
    steps = steps_from_missions(three_missions)
    assert "CONNECT THE FOREST" in steps[0]["title"]
    assert "MERGE RESOLUTION" in steps[4]["title"]


def test_one_mission_reads_exactly_as_it_did(mission):
    """No phase label where there is only one phase — the title would say the
    same thing on every step."""
    steps = steps_from_missions([mission])
    assert steps == steps_from_mission(mission)
    assert "FIRST COMMIT" not in steps[0]["title"]
    assert steps[0]["title"].startswith("1 / 2")


def test_compose_states_every_objective_and_allows_every_id(three_missions):
    """`harden` reports a step whose id the project does not declare, so the
    allowed-id list has to widen with the steps — otherwise every step from the
    second mission onward is reported as one that can never clear."""
    rules = next(s for s in subsystems_for("3D", [], "walk",
                                           missions=three_missions)
                 if s.kind == "rules")
    assert len(rules.options["steps"]) == 5
    assert rules.options["meta"]["objective_ids"] == ["obj_1", "obj_2", "obj_3",
                                                      "obj_4", "obj_5"]
    p = RulesPipeline({"lm_studio": {}, "unity_pipeline": {}})
    s = RulesSchema(steps=rules.options["steps"], meta=rules.options["meta"])
    assert not [m for m in p.harden(p.default_fix(s, "guided"))
                if "never clear" in m]


def test_the_goal_names_the_whole_game_not_the_first_third(three_missions):
    rules = next(s for s in subsystems_for("3D", [], "walk",
                                           missions=three_missions)
                 if s.kind == "rules")
    goal = rules.options["goal"]
    assert "root to branch" in goal
    assert "build station" in goal
    assert "conflicting code blocks" in goal


def test_the_briefing_is_still_titled_after_the_first_mission(three_missions):
    """The board is captioned from the same place. Two player-facing surfaces
    calling the game different things is the defect that pinned this."""
    rules = next(s for s in subsystems_for("3D", [], "walk",
                                           missions=three_missions)
                 if s.kind == "rules")
    assert rules.options["game_title"] == "Connect the Forest"


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

    @pytest.mark.parametrize("kind", ["pickup", "collectible", "item"])
    def test_the_kinds_that_mean_a_thing_you_take_are_pieces(self, kind):
        """Not UI words — the IR's own words for a thing the player TAKES, which
        on a board is exactly a piece. The kind outranks the name, because the
        name is where this went wrong."""
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec
        assert ui_element_of(EntitySpec(name="Umbrellas", kind=kind)) == "card"

    def test_a_pile_of_pickups_is_the_pieces_not_the_place(self):
        """Live (Platform Four Lost Property, 2026-08-16): schema-fill wrote the
        only movable group as kind='pickup', count=12, named `LostPropertyPile`.
        The kind said nothing, the word table read "pile" as somewhere to stack
        onto, and a cast of twelve draggable objects came out as two panels with
        nothing to put in them — so no board composed at all."""
        from src.micro.board import board_options_for_entities
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec

        cast = [EntitySpec(name="Shelves", kind="building", count=1),
                EntitySpec(name="LedgerTerminal", kind="prop", count=1),
                EntitySpec(name="LostPropertyPile", kind="pickup", count=12)]
        assert ui_element_of(cast[2]) == "card"
        opts = board_options_for_entities(cast)
        assert opts is not None, "this cast was never short of pieces"
        assert [p["id"] for p in opts["pieces"]] == ["LostPropertyPile"]
        assert [z["id"] for z in opts["zones"]] == ["Shelves"]
        assert opts["console_group"] == "LedgerTerminal"

    def test_a_pile_with_no_kind_behind_it_is_still_a_place(self):
        """The word rule is not wrong — it is just outranked. `DiscardPile` with
        nothing but its name to go on is still somewhere you stack onto."""
        from src.unity_layout_archetype import ui_element_of
        from src.unity_schema import EntitySpec
        assert ui_element_of(EntitySpec(name="DiscardPile", kind="prop")) == "panel"


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
