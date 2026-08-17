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

from src.micro.board import (BoardPipeline, BoardSchema, board_domain_for_missions,
                             board_options_for_entities, is_console_entity, norm_ref)
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


# ── The legal move: which place takes what, and in what order ────────────────
#
# `accepts` and `requires` are the board's whole refusal vocabulary. Until they
# had a producer every composed board took every piece in every place, so the
# only illegal moves left were "already there" and "that is a readout".

def _piles() -> list[EntitySpec]:
    """Two colours of card and a pile for each — a board where which piece goes
    where is a real rule."""
    return [EntitySpec(name="RedCards",  kind="prop", count=3),
            EntitySpec(name="BlueCards", kind="prop", count=3),
            EntitySpec(name="RedSlots",  kind="prop", count=1),
            EntitySpec(name="BlueSlots", kind="prop", count=1)]


def _staged() -> list[EntitySpec]:
    """One kind of piece and two places it passes through in order."""
    return [EntitySpec(name="FileIcons",    kind="prop", count=9),
            EntitySpec(name="StagingPanel", kind="prop", count=1),
            EntitySpec(name="CommitPanel",  kind="prop", count=1)]


def _domain(entities, objectives, brief=""):
    opts = board_options_for_entities(entities, title="T")
    mission = (MissionSpec(id="m", title="T", objectives=objectives)
               if objectives is not None else None)
    return opts, board_domain_for_missions(opts, mission, brief=brief)


# A brief as they really arrive: hard-wrapped, with the roster mid-sentence.
BRIEF = """A git teaching sim. The desk shows nine file icons — README.md, main.py,
utils.py, setup.cfg, app.js, index.html, styles.css, test_all.py and Makefile —
waiting to be staged. Drag each into a folder container, then git commit to
write the commit history log."""


def _accepts(opts, zid) -> list[str]:
    return next(z for z in opts["zones"] if z["id"] == zid).get("accepts") or []


def _requires(opts, zid) -> str:
    """Absent and empty are the same answer — `BoardZoneGroup.requires`
    defaults to "", so a rule that abstained writes no key at all."""
    return next(z for z in opts["zones"] if z["id"] == zid).get("requires") or ""


def test_a_place_takes_the_piece_that_shares_its_name():
    opts, _msgs = _domain(_piles(), [])
    assert _accepts(opts, "RedSlots") == ["RedCards"]
    assert _accepts(opts, "BlueSlots") == ["BlueCards"]


def test_a_place_that_matches_nothing_leaves_every_place_open():
    """All-or-nothing on purpose. One place left taking anything while its
    neighbours are restricted reads as a bug in the game, not a rule of it."""
    opts, msgs = _domain(_staged(), [])
    assert all(not z.get("accepts") for z in opts["zones"])
    assert any(not ok and "share no word with any piece" in m for ok, m in msgs)


def test_a_piece_with_nowhere_to_go_is_never_derived():
    """A restriction that strands a piece is the stranded-step defect again —
    an instruction the player can never carry out."""
    opts, msgs = _domain(
        _piles() + [EntitySpec(name="GreenCards", kind="prop", count=2)], [])
    assert all(not z.get("accepts") for z in opts["zones"])
    assert any(not ok and "GreenCards would have nowhere legal to go"
               in m for ok, m in msgs)


def test_a_restriction_that_refuses_nothing_is_not_derived():
    opts, msgs = _domain(
        [EntitySpec(name="CardIcons",  kind="prop", count=4),
         EntitySpec(name="CardSlots",  kind="prop", count=1),
         EntitySpec(name="CardPanels", kind="prop", count=1)], [])
    assert all(not z.get("accepts") for z in opts["zones"])
    assert any(not ok and "refuses nothing" in m for ok, m in msgs)


def test_one_place_to_drop_into_has_nothing_to_tell_apart():
    opts, msgs = _domain(_cast(), [])
    assert all(not z.get("accepts") for z in opts["zones"])
    assert any(not ok and "only one place to drop into" in m for ok, m in msgs)


def test_the_brief_names_the_pieces_on_the_plates():
    """`ItemLabel` falls back to "File Icon 3"; the brief said README.md. The
    names are also what `ResolvePieces` matches, so naming them is what makes
    `stage README.md` mean anything."""
    opts, msgs = _domain(_cast(), [], brief=BRIEF)
    items = next(p for p in opts["pieces"] if p["id"] == "FileIcons")["items"]
    assert items[:3] == ["README.md", "main.py", "utils.py"]
    # "Makefile" carries no extension and no quotes — indistinguishable from a
    # prose word, so it is left to the plate's own caption rather than guessed.
    assert "Makefile" not in items
    assert any(ok and "named pieces" in m for ok, m in msgs)


def test_a_wrapped_roster_is_one_list():
    """Briefs arrive hard-wrapped. Treating the soft newline as a break cut the
    roster after the second name and lost seven of the nine."""
    opts, _msgs = _domain(_cast(), [], brief=BRIEF)
    assert len(next(p for p in opts["pieces"]
                    if p["id"] == "FileIcons")["items"]) == 8


def test_never_more_names_than_the_group_has_plates():
    ents = [EntitySpec(name="FileIcons", kind="prop", count=2),
            EntitySpec(name="FolderContainers", kind="prop", count=1)]
    opts, _msgs = _domain(ents, [], brief=BRIEF)
    assert next(p for p in opts["pieces"]
                if p["id"] == "FileIcons")["items"] == ["README.md", "main.py"]


def test_one_name_in_passing_is_not_a_roster():
    opts, _msgs = _domain(
        _cast(), [], brief="The file icons sit beside README.md on the desk.")
    assert not next(p for p in opts["pieces"]
                    if p["id"] == "FileIcons").get("items")


def test_a_roster_for_a_group_the_clause_never_names_is_not_taken():
    """The clause has to be about THIS group, or every group gets the same
    names — the smearing defect the stage resolution refuses too."""
    opts, _msgs = _domain(
        _cast(), [], brief="The build outputs are README.md, main.py, app.js.")
    assert not next(p for p in opts["pieces"]
                    if p["id"] == "FileIcons").get("items")


def test_the_brief_names_the_tool_the_command_line_belongs_to():
    opts, msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Commit what you staged",
                         type="reach", target_ref="CommitHistoryLog")],
        brief=BRIEF)
    assert opts["command_prefix"] == "git"
    assert any(ok and "`git <verb>`" in m for ok, m in msgs)


def test_grammar_in_front_of_a_verb_is_not_a_tool_name():
    opts, msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Commit what you staged",
                         type="reach", target_ref="CommitHistoryLog")],
        brief="When you are ready you must commit the staged work.")
    assert not opts.get("command_prefix")
    assert any(not ok and "no command prefix derived" in m for ok, m in msgs)


def test_the_console_names_the_tool_when_the_brief_does_not():
    ents = [EntitySpec(name="FileIcons", kind="prop", count=9),
            EntitySpec(name="FolderContainers", kind="prop", count=4),
            EntitySpec(name="GitConsole", kind="prop", count=1)]
    opts, _msgs = _domain(ents, [
        MissionObjective(id="o1", description="Stage the files", type="collect",
                         target_ref="FileIcons")], brief="Put the files away.")
    assert opts["command_prefix"] == "git"


def test_a_generic_console_names_no_tool():
    """`TerminalWindow` is what the thing IS, not what it runs."""
    opts, _msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Stage the files", type="collect",
                         target_ref="FileIcons")], brief="Put the files away.")
    assert not opts.get("command_prefix")


def test_a_prefix_that_is_also_a_verb_is_refused():
    """`Run` strips argv[0] when it matches the prefix, so a bare `commit`
    would be eaten as a prefix with no verb behind it and answer with usage —
    the verb would become untypeable."""
    opts, _msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Commit the staged work",
                         type="reach", target_ref="CommitHistoryLog")],
        brief="commit commit the staged work.")
    assert not opts.get("command_prefix")


def test_an_instruction_about_typing_is_not_a_tool_name():
    """"Type commit to finish" describes the keyboard, not the tool."""
    opts, _msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Commit the staged work",
                         type="reach", target_ref="CommitHistoryLog")],
        brief="Type commit to finish the exercise.")
    assert not opts.get("command_prefix")


def test_a_terminal_only_board_still_gets_its_verbs():
    """The board with NO drop target is the one that needs verbs most, and the
    old guard returned before deriving any."""
    ents = [EntitySpec(name="FileIcons", kind="prop", count=4),
            EntitySpec(name="CommitHistoryLog", kind="prop", count=1),
            EntitySpec(name="TerminalWindow", kind="prop", count=1)]
    opts, _msgs = _domain(ents, [
        MissionObjective(id="o1", description="Record the changes", type="reach",
                         target_ref="CommitHistoryLog")])
    assert opts["variant"] == "terminal"
    assert [c["verb"] for c in opts["commands"]] == ["record"]
    assert opts["meta"]["objective_commands"] == {"o1": "record"}


def test_the_objectives_own_words_become_the_domains_verbs():
    """The hint bar already shows "Stage every modified file". A parser that
    answers to its first word is one the player has been told how to use."""
    opts, msgs = _domain(_cast(), [
        MissionObjective(id="obj_1", description="Stage every modified file",
                         type="collect", target_ref="FileIcons", count=9),
        MissionObjective(id="obj_2", description="Record what you staged",
                         type="reach", target_ref="CommitHistoryLog")])
    assert [(c["verb"], c["to_zone"], c["moves"]) for c in opts["commands"]] == [
        ("stage",  "FolderContainers",  "FileIcons"),
        ("record", "CommitHistoryLog",  "")]
    assert opts["commands"][0]["help"] == "Stage every modified file"
    assert any(ok and "verbs — stage" in m for ok, m in msgs)


def test_only_the_step_a_drag_cannot_do_is_bound_to_its_verb():
    """`TryPlace` refuses a DRAG into a readout, so an objective about the log
    can only be satisfied by typing. The piece objective keeps OnPiecePlaced,
    which counts each placement — a verb fires once and would complete a count
    of nine on the first command."""
    opts, _msgs = _domain(_cast(), [
        MissionObjective(id="obj_1", description="Stage every modified file",
                         type="collect", target_ref="FileIcons", count=9),
        MissionObjective(id="obj_2", description="Record what you staged",
                         type="reach", target_ref="CommitHistoryLog")])
    assert opts["meta"]["objective_commands"] == {"obj_2": "record"}


def test_a_step_the_board_owns_nothing_in_gets_no_verb():
    """SEAM 8a runs before every other source. A verb for an objective the
    board has nothing to do with would claim it away from the trigger volume
    that really raises it — and print a word that moves nothing."""
    opts, _msgs = _domain(_cast(), [
        MissionObjective(id="walk_in", description="Enter the server room",
                         type="reach", target_ref="ServerRoomDoor")])
    assert not opts.get("commands")


def test_a_verb_the_parser_already_answers_is_refused():
    """`Run` switches on its built-ins before it reads the declared commands,
    so a colliding verb is not overridden — it is unreachable."""
    opts, msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="List the staged files",
                         type="reach", target_ref="CommitHistoryLog")])
    assert not opts.get("commands")
    assert any(not ok and "the parser answers itself" in m for ok, m in msgs)


def test_two_steps_describing_one_verb_declare_it_once():
    opts, msgs = _domain(_cast(), [
        MissionObjective(id="o1", description="Stage the modified files",
                         type="collect", target_ref="FileIcons"),
        MissionObjective(id="o2", description="Stage the rest",
                         type="reach", target_ref="CommitHistoryLog")])
    assert [c["verb"] for c in opts["commands"]] == ["stage"]
    # The loser is NOT bound to the winner's verb — that would report a dead
    # step as a live one.
    assert opts["meta"]["objective_commands"] == {}
    assert any(not ok and "one verb cannot mean two moves" in m for ok, m in msgs)


def test_a_verb_shadowed_by_a_builtin_is_reported_by_harden():
    """The LLM path can declare one too, and it fails the same way."""
    _s, msgs = _fixed(pieces=[{"id": "FileIcons", "count": 3}],
                      zones=[{"id": "FolderContainers"}],
                      commands=[{"verb": "list", "to_zone": "FolderContainers"}])
    assert any("can never run" in m for m in msgs)


def test_the_mission_order_becomes_the_ordering_rule():
    """You cannot commit what you have not staged. The mission already says so
    in its objective order; nothing used to read it."""
    opts, msgs = _domain(_staged(), [
        MissionObjective(id="o1", description="Stage the files", type="reach",
                         target_ref="StagingPanel"),
        MissionObjective(id="o2", description="Commit them", type="reach",
                         target_ref="CommitPanel")])
    assert _requires(opts, "CommitPanel") == "StagingPanel"
    assert not _requires(opts, "StagingPanel")
    assert any(ok and "CommitPanel after StagingPanel" in m for ok, m in msgs)


def test_the_ordering_rule_reads_every_mission():
    """Split the same two beats across two missions and the rule must be the
    same rule. SEAM 8a binds the second mission's objective either way, and the
    step panel now states it — a domain derived from the first mission alone
    would enforce an order the player was never held to."""
    opts = board_options_for_entities(_staged(), title="T")
    msgs = board_domain_for_missions(opts, [
        MissionSpec(id="m1", title="Stage", objectives=[
            MissionObjective(id="o1", description="Stage the files", type="reach",
                             target_ref="StagingPanel")]),
        MissionSpec(id="m2", title="Commit", objectives=[
            MissionObjective(id="o2", description="Commit them", type="reach",
                             target_ref="CommitPanel")]),
    ])
    assert _requires(opts, "CommitPanel") == "StagingPanel"
    assert any(ok and "CommitPanel after StagingPanel" in m for ok, m in msgs)


def test_the_verbs_come_from_every_mission():
    """A verb derived only from the first mission left the parser unable to
    answer two thirds of the actions the hint bar names."""
    opts = board_options_for_entities(_staged(), title="T")
    board_domain_for_missions(opts, [
        MissionSpec(id="m1", title="Stage", objectives=[
            MissionObjective(id="o1", description="Stage the files", type="reach",
                             target_ref="StagingPanel")]),
        MissionSpec(id="m2", title="Commit", objectives=[
            MissionObjective(id="o2", description="Commit them", type="reach",
                             target_ref="CommitPanel")]),
    ])
    assert [c["verb"] for c in opts["commands"]] == ["stage", "commit"]


def test_one_mission_may_still_be_passed_bare():
    """A caller holding a single MissionSpec should not have to wrap it."""
    one = MissionSpec(id="m", title="T", objectives=[
        MissionObjective(id="o1", description="Stage the files", type="reach",
                         target_ref="StagingPanel"),
        MissionObjective(id="o2", description="Commit them", type="reach",
                         target_ref="CommitPanel")])
    bare, wrapped = (board_options_for_entities(_staged(), title="T"),
                     board_options_for_entities(_staged(), title="T"))
    assert (board_domain_for_missions(bare, one)
            == board_domain_for_missions(wrapped, [one]))
    assert bare == wrapped


def test_an_objective_naming_a_piece_reaches_the_place_that_alone_takes_it():
    """A mission that never names a zone still orders them, through the piece
    each objective is about — the stage is the place that accepts it. Proven
    here by the guard below naming BOTH piles, which it could only do if both
    stages resolved from a piece ref."""
    _opts, msgs = _domain(_piles(), [
        MissionObjective(id="o1", description="Reds", type="collect",
                         target_ref="RedCards"),
        MissionObjective(id="o2", description="Blues", type="collect",
                         target_ref="BlueCards")])
    assert any(not ok and "BlueSlots does not require RedSlots" in m
               for ok, m in msgs)


def test_a_stage_several_places_could_be_is_refused_not_guessed():
    """Every place takes FileIcons, so the objective names no one place. A
    guessed stage is the silent wrong answer this module exists to stop."""
    opts, _msgs = _domain(_staged(), [
        MissionObjective(id="o1", description="Stage", type="collect",
                         target_ref="FileIcons"),
        MissionObjective(id="o2", description="Commit", type="reach",
                         target_ref="CommitPanel")])
    assert not _requires(opts, "CommitPanel")


def test_an_ordering_rule_no_piece_could_satisfy_is_refused():
    """BlueSlots requiring RedSlots would not sequence the board, it would
    close BlueSlots forever: no piece RedSlots takes may enter it."""
    opts, msgs = _domain(_piles(), [
        MissionObjective(id="o1", description="Reds", type="collect",
                         target_ref="RedCards"),
        MissionObjective(id="o2", description="Blues", type="collect",
                         target_ref="BlueCards")])
    assert not _requires(opts, "BlueSlots")
    assert any(not ok and "could never be satisfied" in m for ok, m in msgs)


def test_no_mission_means_no_ordering_rule():
    opts, msgs = _domain(_staged(), None)
    assert all(not z.get("requires") for z in opts["zones"])
    assert any(not ok and "no mission objectives" in m for ok, m in msgs)


def test_a_derived_rule_survives_into_the_schema():
    """The fields the template reads are the fields the derivation wrote."""
    opts, _msgs = _domain(_staged(), [
        MissionObjective(id="o1", description="Stage", type="reach",
                         target_ref="StagingPanel"),
        MissionObjective(id="o2", description="Commit", type="reach",
                         target_ref="CommitPanel")])
    variant = opts.pop("variant")
    s = BoardPipeline(CONFIG).default_fix(BoardSchema(**opts, variant=variant),
                                          variant)
    assert not BoardPipeline(CONFIG).harden(s)
    assert next(z for z in s.zones if z.id == "CommitPanel").requires == "StagingPanel"


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


def test_the_board_is_titled_after_the_mission():
    """A board captioned "Game Board" under a briefing that calls the game
    something else is two player-facing surfaces disagreeing about what is
    being played. Both read the same mission title."""
    board = next(s for s in _subs(_schema()) if s.kind == "board")
    rules = next(s for s in _subs(_schema()) if s.kind == "rules")
    assert board.options["title"] == "First Commit" == rules.options["game_title"]


def test_an_untitled_mission_still_names_the_board():
    board = next(s for s in _subs(_schema(missions=[MissionSpec(
        id="m1", title="", objectives=[MissionObjective(
            id="o1", description="Stage", type="collect",
            target_ref="FileIcons")])])) if s.kind == "board")
    variant = dict(board.options).pop("variant", board.variant)
    s = BoardPipeline(CONFIG).default_fix(
        BoardSchema(name="GameBoard", variant=board.variant, **board.options),
        variant)
    assert s.title == "Game Board"


def test_the_derived_rules_reach_the_emitted_csharp(tmp_path):
    schema = _schema(entities=_staged(), missions=[MissionSpec(
        id="m1", title="Ship It", objectives=[
            MissionObjective(id="o1", description="Stage the files",
                             type="reach", target_ref="StagingPanel"),
            MissionObjective(id="o2", description="Commit them",
                             type="reach", target_ref="CommitPanel")])])
    _compose(schema, tmp_path)
    cs = (tmp_path / "Micros" / "board" / "Scripts" / "GameBoard.cs").read_text(
        encoding="utf-8")
    assert 'requires = "StagingPanel"' in cs
    assert 'boardTitle = "Ship It"' in cs
    # The verbs and the words that explain them, in the parser the player types
    # into — help text lifted from the objective so the terminal and the hint
    # bar over it say the same thing.
    assert 'verb = "stage"' in cs and 'verb = "commit"' in cs
    assert "Stage the files" in cs


def test_the_brief_reaches_the_board_through_compose(tmp_path):
    """`subsystems_for` already receives the brief (unity_pipeline passes
    user_prompt); the board reads the names and the tool out of it.

    The mission says "Commit", so `commit` is a verb the board has and the
    brief's "git commit" is evidence about it. A prefix is only ever taken from
    in front of a verb that exists — `git record` would name no real command.
    """
    schema = _schema(missions=[MissionSpec(id="m1", title="First Commit",
        objectives=[
            MissionObjective(id="obj_1", description="Stage every modified file",
                             type="collect", target_ref="FileIcons", count=9),
            MissionObjective(id="obj_2", description="Commit what you staged",
                             type="reach", target_ref="CommitHistoryLog")])])
    subs = subsystems_for(schema.game_type, [], schema.locomotion,
                          user_prompt=BRIEF, entities=schema.entities,
                          missions=schema.missions)
    board = next(s for s in subs if s.kind == "board")
    assert board.options["command_prefix"] == "git"
    c = MicroComposer(schema.project_name)
    c.compose(subs, tmp_path)
    cs = (tmp_path / "Micros" / "board" / "Scripts" / "GameBoard.cs").read_text(
        encoding="utf-8")
    assert '"README.md"' in cs and '"styles.css"' in cs
    assert 'commandPrefix = "git"' in cs


def test_the_readout_step_rides_the_verb_that_writes_it(tmp_path):
    """The bridge, end to end: a history is produced by a command, so the
    objective about it advances on OnCommand rather than on a zone being
    filled by a drag the board would have refused."""
    wiring, report = _compose(_schema(), tmp_path)
    assert "_gameboard.OnCommand += _onBoardObjective1;" in wiring
    assert ('if (key == "record") '
            '_firstcommitobjectives.ReportProgress("obj_2", 1)') in wiring
    assert report["objectives_unbound"] == []


def test_a_drop_target_zone_step_still_rides_the_zone(tmp_path):
    """Only the readout moved. A place you CAN drop into keeps OnZoneFilled,
    so the bridge did not quietly re-route the bindings that already worked."""
    schema = _schema(missions=[MissionSpec(id="m", title="M", objectives=[
        MissionObjective(id="fill", description="Fill the folders", type="reach",
                         target_ref="FolderContainers", count=5)])])
    wiring, _ = _compose(schema, tmp_path)
    assert "_gameboard.OnZoneFilled += _onBoardObjective0;" in wiring
    assert 'ReportProgress("fill", 5)' in wiring


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
    # Two objectives, two different keys on two different events: the pieces
    # being placed, and the verb that writes the history.
    assert 'if (key == "FileIcons")' in wiring
    assert 'if (key == "record")' in wiring


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
    assert "_gameboard.OnCommand -= _onBoardObjective1;" in wiring


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
