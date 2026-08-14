"""
The board — the domain model and the legal-move logic.

The rules micro says "drag every file icon into a folder container". These pin
the layer that makes doing it work: which groups are pieces and which are
places, whether a move is legal, the parser that reaches the same move core
from a typed command, and the seam that lets a real move advance a real
objective.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import pytest

from src.micro.board import (BoardPipeline, BoardSchema, board_options_for_entities,
                             is_console_entity, norm_ref)
from src.unity_compose import MicroComposer, subsystems_for
from src.unity_playability import evaluate
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              UnitySchema)

CONFIG = {
    "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
    "unity_pipeline": {"unity_output_dir":   "output/unity",
                       "unity_template_dir": "templates/unity"},
}


def _cast() -> list[EntitySpec]:
    """The CodeVault cast, as schema-fill really writes it: every entity
    kind='prop', the meaning carried by the name."""
    return [
        EntitySpec(name="TerminalWindow",   kind="prop", count=1, scale=3.0),
        EntitySpec(name="FolderContainers", kind="prop", count=4, scale=1.2,
                   tag="Interactable"),
        EntitySpec(name="FileIcons",        kind="prop", count=9, scale=0.6,
                   tag="Collectible"),
        EntitySpec(name="CommitHistoryLog", kind="prop", count=1, scale=1.0),
        EntitySpec(name="BranchLabel",      kind="prop", count=1, scale=0.7),
    ]


def _schema(**kw) -> UnitySchema:
    base = dict(
        project_name="CodeVault", game_type="2d_topdown", platform="pc",
        render_pipeline="URP", locomotion="none",
        description="Drag every file icon into a folder container, then commit.",
        entities=_cast(),
        missions=[MissionSpec(id="m1", title="First Commit", objectives=[
            MissionObjective(id="obj_1", description="Stage every modified file",
                             type="collect", target_ref="FileIcons", count=9),
            MissionObjective(id="obj_2", description="Record what you staged",
                             type="reach", target_ref="CommitHistoryLog"),
        ])],
    )
    base.update(kw)
    return UnitySchema(**base)


# ── Derivation: which of the cast is a piece, and which is a place ───────────

def test_the_cast_becomes_pieces_and_places():
    opts = board_options_for_entities(_cast())
    assert [p["id"] for p in opts["pieces"]] == ["FileIcons"]
    assert [z["id"] for z in opts["zones"]] == ["FolderContainers",
                                                "CommitHistoryLog"]
    assert opts["console_group"] == "TerminalWindow"
    assert opts["variant"] == "drag_and_terminal"


def test_a_card_is_a_piece_too():
    """`CommitNodes` reads as a card, and a card is a token you move."""
    opts = board_options_for_entities(
        _cast() + [EntitySpec(name="CommitNodes", kind="prop", count=6)])
    assert "CommitNodes" in [p["id"] for p in opts["pieces"]]


def test_a_readout_is_a_place_you_cannot_drop_into():
    """A log is PRODUCED. Dropping a file into the history by hand is not a
    move any domain with a history has — the reference game says so in as many
    words, and the vocabulary already knows a log from a folder."""
    zones = {z["id"]: z for z in board_options_for_entities(_cast())["zones"]}
    assert zones["FolderContainers"]["drop_target"] is True
    assert zones["CommitHistoryLog"]["drop_target"] is False


def test_chrome_is_neither_a_piece_nor_a_place():
    """A caption is not a token and a backboard is not a container."""
    opts = board_options_for_entities(
        _cast() + [EntitySpec(name="GridBackdrop", kind="prop", count=1)])
    named = {p["id"] for p in opts["pieces"]} | {z["id"] for z in opts["zones"]}
    assert "BranchLabel" not in named and "GridBackdrop" not in named


def test_a_cast_with_nothing_to_move_makes_no_board():
    """None is the answer, not an empty board: a component whose every answer
    is "no" in a scene is worse than saying so at compose time."""
    assert board_options_for_entities([
        EntitySpec(name="FolderContainers", kind="prop", count=4)]) is None
    assert board_options_for_entities([
        EntitySpec(name="FileIcons", kind="prop", count=9)]) is None


def test_a_board_of_readouts_needs_a_command_line_to_exist():
    """Nowhere to drop and nothing to type = no legal move at all."""
    readouts = [EntitySpec(name="FileIcons", kind="prop", count=4),
                EntitySpec(name="CommitHistoryLog", kind="prop", count=1)]
    assert board_options_for_entities(readouts) is None
    with_console = readouts + [EntitySpec(name="TerminalWindow", kind="prop")]
    assert board_options_for_entities(with_console)["variant"] == "terminal"


def test_only_a_shell_is_a_command_line():
    assert is_console_entity(EntitySpec(name="TerminalWindow", kind="prop"))
    assert is_console_entity(EntitySpec(name="CommandConsole", kind="prop"))
    # A window that displays rather than accepts. Composing a parser for it
    # would put a prompt on a picture.
    assert not is_console_entity(EntitySpec(name="StatusDashboard", kind="prop"))


def test_the_objective_ref_matcher_survives_case_and_plural():
    assert norm_ref("file icons") == norm_ref("FileIcons") == norm_ref("FileIcon")
    assert norm_ref("FileIcons") != norm_ref("FolderContainers")


# ── The schema's own repair and report ───────────────────────────────────────

def _fixed(**kw) -> tuple[BoardSchema, list[str]]:
    p = BoardPipeline(CONFIG)
    s = BoardSchema(**kw)
    s = p.default_fix(s, s.variant)
    return s, p.harden(s)


def test_labels_are_filled_so_nothing_is_nameless_on_screen():
    s, _ = _fixed(pieces=[{"id": "FileIcons", "count": 3}],
                  zones=[{"id": "FolderContainers"}])
    assert s.pieces[0].label == "File Icon"
    assert s.zones[0].label == "FOLDER CONTAINERS"


def test_a_piece_no_place_accepts_is_reported():
    """The stranded-step defect, one layer down: a piece that can never be
    placed is an instruction the player can never carry out."""
    _s, msgs = _fixed(
        pieces=[{"id": "FileIcons", "count": 3}, {"id": "CommitNodes", "count": 2}],
        zones=[{"id": "FolderContainers", "accepts": ["FileIcons"]}])
    assert any("no zone accepts CommitNodes" in m for m in msgs)


def test_a_command_to_nowhere_is_reported():
    _s, msgs = _fixed(pieces=[{"id": "FileIcons"}],
                      zones=[{"id": "FolderContainers"}],
                      commands=[{"verb": "add", "to_zone": "StagingArea"}])
    assert any("'add'" in m and "StagingArea" in m for m in msgs)


def test_an_ordering_rule_pointing_at_no_place_is_reported():
    _s, msgs = _fixed(pieces=[{"id": "FileIcons"}],
                      zones=[{"id": "History", "requires": "Staging"}])
    assert any("requires 'Staging'" in m for m in msgs)


def test_a_place_that_requires_itself_is_reported():
    _s, msgs = _fixed(pieces=[{"id": "FileIcons"}],
                      zones=[{"id": "History", "requires": "History"}])
    assert any("requires itself" in m for m in msgs)


def test_two_commands_cannot_share_a_verb():
    """The parser dispatches on the verb, so the second would be unreachable —
    a command the player can type and never run."""
    s, _ = _fixed(pieces=[{"id": "P"}], zones=[{"id": "Z"}],
                  commands=[{"verb": "add", "to_zone": "Z"},
                            {"verb": "add", "to_zone": "Z", "help": "other"}])
    assert [c.verb for c in s.commands] == ["add"]


def test_a_board_with_no_drop_target_falls_back_to_the_parser():
    """Not the variant it was asked for — the one that still has a move in it."""
    s, _ = _fixed(variant="drag", pieces=[{"id": "P"}],
                  zones=[{"id": "Log", "drop_target": False}])
    assert s.variant == "terminal"


# ── The emitted mechanic ─────────────────────────────────────────────────────

def _emit(tmp_path, **kw):
    p = BoardPipeline(CONFIG)
    s = BoardSchema(**kw)
    s = p.default_fix(s, s.variant)
    p.harden(s)
    p.write_files(s, s.variant, tmp_path)
    return (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")


@pytest.fixture
def board_cs(tmp_path):
    opts = board_options_for_entities(_cast())
    variant = opts.pop("variant")
    return _emit(tmp_path, variant=variant, **opts)


def test_every_illegal_move_says_why(board_cs):
    """Silence is the defect. A move that snaps back with no reason teaches the
    player that the game is broken, not that the move was wrong."""
    for refusal in ("is already in",
                    "is a readout",
                    "only takes",
                    "has to be in"):
        assert refusal in board_cs, refusal


def test_one_move_core_serves_both_surfaces(board_cs):
    """The drag and the parser both end in TryPlace, so they cannot disagree
    about what is legal."""
    assert board_cs.count("public bool TryPlace(") == 1
    assert "TryPlace(piece, zone, true, out reason)" in board_cs    # the drag
    assert "TryPlace(p, z, false, out reason)" in board_cs          # a command


def test_the_readout_rule_is_the_only_thing_the_surface_changes(board_cs):
    """`byDrag` is which surface asked, not a permission level: a command may
    write a log, a hand may not, and every other rule applies to both."""
    assert "if (byDrag && !spec.dropTarget)" in board_cs


def test_the_board_adopts_the_spawned_objects_rather_than_building_its_own(board_cs):
    assert "GameObject.Find(g.id)" in board_cs
    assert "GameObject.Find(z.id)" in board_cs
    # A declared group the scene does not contain is named, never skipped in
    # silence — it means the cast and the rules disagree.
    assert "no spawned group named" in board_cs


def test_the_parser_carries_its_built_ins_with_no_authored_commands(board_cs):
    for builtin in ('case "help"', 'case "status"', 'case "zones"', 'case "move"'):
        assert builtin in board_cs, builtin


def test_the_parser_keeps_a_quoted_phrase_in_one_piece(board_cs):
    """`commit -m "what changed"` has to survive the split, or every message
    the domain takes arrives as five arguments."""
    assert "quoted = !quoted" in board_cs


def test_a_declared_verb_reaches_the_parser(tmp_path):
    cs = _emit(tmp_path, pieces=[{"id": "FileIcons", "count": 3}],
               zones=[{"id": "Staging"}], command_prefix="git",
               commands=[{"verb": "add", "moves": "FileIcons", "to_zone": "Staging",
                          "help": "stage a file"}])
    assert 'verb = "add"' in cs and 'commandPrefix = "git"' in cs


def test_the_events_an_objective_can_ride_are_all_declared(board_cs):
    for ev in ("OnPiecePlaced", "OnGroupComplete", "OnZoneFilled", "OnCommand",
               "OnRejected"):
        assert f"public event Action<string> {ev};" in board_cs


def test_progress_is_monotonic(board_cs):
    """A piece dragged in and out and in again must not advance a mission
    twice — an objective of nine would complete on one piece."""
    assert "if (!p.placed)" in board_cs


def test_a_terminal_only_board_has_no_drag_handler(tmp_path):
    cs = _emit(tmp_path, variant="terminal", pieces=[{"id": "P", "count": 2}],
               zones=[{"id": "Log", "drop_target": False}])
    assert "BeginDrag" not in cs and "public string Run(" in cs


def test_a_drag_only_board_still_shows_the_refusal(tmp_path):
    """With no terminal to print into, a rule the player cannot read is a rule
    they cannot learn."""
    cs = _emit(tmp_path, variant="drag", pieces=[{"id": "P", "count": 2}],
               zones=[{"id": "Z"}])
    assert "BoardMessage" in cs and "_message" in cs


# ── Compose ──────────────────────────────────────────────────────────────────

def _subs(schema: UnitySchema):
    return subsystems_for(schema.game_type, [], schema.locomotion,
                          entities=schema.entities, missions=schema.missions)


def test_a_ui_first_game_composes_a_board():
    board = next(s for s in _subs(_schema()) if s.kind == "board")
    assert board.target == "systems"
    assert [p["id"] for p in board.options["pieces"]] == ["FileIcons"]


def test_an_embodied_game_composes_none():
    """An avatar game's verbs are the controller's. The board binds to built
    board plates, which only the `ui:` route produces."""
    assert not [s for s in _subs(_schema(locomotion="walk")) if s.kind == "board"]


def _compose(schema, tmp_path):
    c = MicroComposer(schema.project_name)
    c.compose(_subs(schema), tmp_path)
    wiring = (tmp_path / "Micros" / "wiring" / "Scripts" / "GameWiring.cs")
    return wiring.read_text(encoding="utf-8"), c.wiring_report


def test_the_mission_binds_to_real_moves(tmp_path):
    """The whole point. Before this the `collect` objective bound to any click
    on any interactable, so the instruction "drag every file icon into a folder
    container" was true prose about a mechanic that did not exist."""
    wiring, report = _compose(_schema(), tmp_path)
    assert report["objectives_bound"] == ["FirstCommitObjectives.obj_1",
                                          "FirstCommitObjectives.obj_2"]
    assert report["objectives_unbound"] == []
    assert "_gameboard.OnPiecePlaced += _onBoardObjective0;" in wiring
    assert 'if (key == "FileIcons") _firstcommitobjectives.ReportProgress("obj_1", 1)' in wiring


def test_each_objective_gets_its_own_filtered_binding(tmp_path):
    """A trigger volume cannot tell which objective an event was about, so the
    generic sources bind ONE each. Every board event carries its key, so a
    six-objective board does not have to settle for one live step."""
    wiring, _report = _compose(_schema(), tmp_path)
    assert wiring.count("SEAM 8: board") == 2
    assert 'if (key == "CommitHistoryLog")' in wiring


def test_an_event_that_fires_once_advances_the_whole_count(tmp_path):
    """OnPiecePlaced fires per piece; OnZoneFilled fires once. Binding the
    second at one-per-event would leave a count-of-5 objective permanently at
    1/5 — silently, which is the failure this seam exists to end."""
    schema = _schema(missions=[MissionSpec(id="m", title="M", objectives=[
        MissionObjective(id="fill", description="Fill the folders", type="reach",
                         target_ref="FolderContainers", count=5)])])
    wiring, _ = _compose(schema, tmp_path)
    assert 'ReportProgress("fill", 5)' in wiring


def test_every_board_subscription_is_undone(tmp_path):
    """A stored delegate exists so OnDestroy removes the SAME instance it
    added; a lambda re-created at unsubscribe time removes nothing."""
    wiring, _ = _compose(_schema(), tmp_path)
    assert "_gameboard.OnPiecePlaced -= _onBoardObjective0;" in wiring
    assert "_gameboard.OnZoneFilled -= _onBoardObjective1;" in wiring


def test_an_objective_the_board_knows_nothing_about_falls_through(tmp_path):
    """The board only claims objectives naming something ON it. Anything else
    still reaches the generic sources rather than being swallowed."""
    schema = _schema(missions=[MissionSpec(id="m", title="M", objectives=[
        MissionObjective(id="walk_in", description="Enter the vault",
                         type="reach", target_ref="VaultDoor")])])
    wiring, report = _compose(schema, tmp_path)
    assert "SEAM 8: board" not in wiring
    assert report["objectives_bound"] == ["MObjectives.walk_in"]   # the trigger


def test_the_composed_project_parses(tmp_path):
    """MicroComposer.compose runs the Roslyn gate itself and raises on a
    defect, so reaching this line is the assertion."""
    _compose(_schema(), tmp_path)


# ── Step 7 ───────────────────────────────────────────────────────────────────

def test_a_board_game_is_now_winnable(tmp_path):
    schema = _schema()
    _wiring, report = _compose(schema, tmp_path)
    schema.meta["wiring"] = report
    rep = evaluate(schema)
    assert rep.checks["mission_loop"] is True
    assert not [f for f in rep.errors if f.check == "mission_loop"]
