"""
Objective targets — the entity an objective is about, recovered from its sentence.

Step 3 leaves `target_ref` blank routinely, and four consumers read it: the
board's `commands` and `requires` derivations, SEAM 8a, and the playability
check that asks whether the goal exists in the build. These pin what the fixer
resolves, what it refuses to guess at, and that a resolved ref really does reach
SEAM 8a as a binding.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import types

from src.micro.board import board_domain_for_missions, board_options_for_entities
from src.unity_compose import _board_objective_binding
from src.unity_objective_targets import resolve_objective_targets
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              UnitySchema)


# ── The live incident: RepoQuest: Enterprise Edition, six blank refs ─────────

def _repoquest_cast() -> list[EntitySpec]:
    """The cast verbatim from the shipped schema, `model_hint` included —
    `ui_element_of` reads it, and "conveyor belt SURFACE" is the whole reason
    `ConveyorBelts` is a place rather than unclassified.

    Two pieces named `*Nodes` is the point of this fixture, not an accident: it
    is what makes half the mission genuinely ambiguous."""
    return [
        EntitySpec(name='HubInterface', kind='prop', count=1, shape='cube',
                   scale=2.0, tag='UI_Hub', model_hint='central hub interface'),
        EntitySpec(name='ZonePortals', kind='mechanism', count=7, shape='cube',
                   scale=1.5, tag='Portal', model_hint='zone portal frame'),
        EntitySpec(name='GraphNodes', kind='prop', count=20, shape='sphere',
                   scale=1.0, tag='GraphNode', model_hint='code node sphere'),
        EntitySpec(name='ConnectionLines', kind='prop', count=30, shape='cylinder',
                   scale=1.5, tag='ConnectionLine', model_hint='connection wire'),
        EntitySpec(name='ConveyorBelts', kind='mechanism', count=12, shape='plane',
                   scale=3.0, tag='ConveyorBelt', model_hint='conveyor belt surface'),
        EntitySpec(name='BuildStations', kind='building', count=4, shape='cube',
                   scale=2.5, tag='BuildStation', model_hint='build server rack'),
        EntitySpec(name='ConflictBlocks', kind='obstacle', count=8, shape='cube',
                   scale=1.2, tag='ConflictBlock', model_hint='conflict code block'),
        EntitySpec(name='AlignmentGuides', kind='prop', count=8, shape='plane',
                   scale=1.0, tag='GuideLine', model_hint='alignment guide line'),
        EntitySpec(name='EmergencyNodes', kind='mechanism', count=5, shape='sphere',
                   scale=1.0, tag='EmergencyNode', model_hint='emergency fix node'),
        EntitySpec(name='HeatEffects', kind='fx', count=5, shape='sphere',
                   scale=0.8, tag='HeatEffect', model_hint='heat distortion effect'),
        EntitySpec(name='DashboardUI', kind='ui', count=1, shape='plane',
                   scale=5.0, tag='Dashboard', model_hint='dashboard interface'),
        EntitySpec(name='MetricVisualizers', kind='fx', count=10, shape='sphere',
                   scale=0.5, tag='MetricData', model_hint='data point visualization'),
    ]


def _repoquest() -> UnitySchema:
    return UnitySchema(
        project_name="RepoQuestEnterprise", game_type="isometric", platform="pc",
        render_pipeline="URP", locomotion="none",
        description="A UI-first simulation of GitHub workflows.",
        entities=_repoquest_cast(),
        missions=[
            MissionSpec(id="m1", title="Connect the Forest", objectives=[
                MissionObjective(id="obj_1", description="Select at least 5 nodes.",
                                 type="collect", count=5),
                MissionObjective(
                    id="obj_2",
                    description="Connect all selected nodes into a single chain.",
                    type="reach"),
            ]),
            MissionSpec(id="m2", title="Initialize Pipeline", objectives=[
                MissionObjective(
                    id="obj_3",
                    description="Place a build artifact on the conveyor.",
                    type="collect"),
                MissionObjective(id="obj_4",
                                 description="Ensure no errors appear in logs.",
                                 type="survive", count=60),
            ]),
            MissionSpec(id="m3", title="Merge Resolution", objectives=[
                MissionObjective(id="obj_5",
                                 description="Align the red conflict markers.",
                                 type="reach"),
                MissionObjective(
                    id="obj_6",
                    description="Resolve all conflicts in the cavern zone.",
                    type="collect", count=8),
            ]),
        ],
    )


def _refs(schema: UnitySchema) -> dict[str, str]:
    return {o.id: o.target_ref for m in schema.missions for o in m.objectives}


def test_the_destination_of_an_imperative_is_a_place():
    """"Place a build artifact ON THE conveyor" — `BuildStations` matches a word
    too, but only `ConveyorBelts` is where the sentence says the thing goes."""
    schema = _repoquest()
    resolve_objective_targets(schema)
    assert _refs(schema)["obj_3"] == "ConveyorBelts"


def test_the_only_entity_the_sentence_names_wins():
    schema = _repoquest()
    resolve_objective_targets(schema)
    assert _refs(schema)["obj_5"] == "ConflictBlocks"


def test_the_type_breaks_a_tie_between_a_piece_and_a_place():
    """"Resolve all conflicts in the cavern zone" names `ConflictBlocks` and
    `ZonePortals` equally well. A `collect` objective advances on a PIECE being
    placed, so the piece is the honest answer."""
    schema = _repoquest()
    resolve_objective_targets(schema)
    assert _refs(schema)["obj_6"] == "ConflictBlocks"


def test_two_pieces_that_answer_equally_well_abstain():
    """`GraphNodes` and `EmergencyNodes` both answer to "nodes", and nothing in
    the sentence, the cast or the objective type tells them apart. A ref guessed
    here would bind the objective to the wrong group forever AND claim it away
    from the generic SEAM 8 source that can still advance it."""
    schema = _repoquest()
    msgs = resolve_objective_targets(schema)
    assert _refs(schema)["obj_1"] == ""
    assert _refs(schema)["obj_2"] == ""
    tie = [m for m in msgs if "obj_1" in m]
    assert len(tie) == 1
    assert "EmergencyNodes" in tie[0] and "GraphNodes" in tie[0]


def test_a_self_driven_objective_is_left_alone():
    """`survive` runs on the tracker's own clock. A ref would let SEAM 8a bind a
    second driver on top of it."""
    schema = _repoquest()
    resolve_objective_targets(schema)
    assert _refs(schema)["obj_4"] == ""


def test_an_authored_ref_is_never_overwritten():
    schema = _repoquest()
    schema.missions[0].objectives[0].target_ref = "EmergencyNodes"
    resolve_objective_targets(schema)
    assert _refs(schema)["obj_1"] == "EmergencyNodes"


def test_every_ref_written_is_an_entity_that_exists():
    """`_check_objective_targets` grades a ref against schema.entities, so a ref
    this pass invents would ship as an ERROR rather than a fix."""
    schema = _repoquest()
    resolve_objective_targets(schema)
    have = {e.name for e in schema.entities}
    assert {r for r in _refs(schema).values() if r} <= have


def test_it_is_deterministic():
    a, b = _repoquest(), _repoquest()
    assert resolve_objective_targets(a) == resolve_objective_targets(b)
    assert _refs(a) == _refs(b)


# ── The point of the exercise: the refs reach SEAM 8a ────────────────────────

def _board_for(schema: UnitySchema):
    """The board compose would build from this schema, as the namespace
    `_board_objective_binding` reads."""
    mission = [m for m in schema.missions if m.objectives][0]
    opts = board_options_for_entities(schema.entities, title=mission.title,
                                      mission=mission)
    board_domain_for_missions(opts, mission, brief=schema.description)
    return types.SimpleNamespace(board={
        "pieces":   [p["id"] for p in opts["pieces"]],
        "zones":    [z["id"] for z in opts["zones"]],
        "commands": [c["verb"] for c in (opts.get("commands") or [])],
        "objective_commands": (opts.get("meta") or {}).get("objective_commands") or {},
    }), opts


def test_blank_refs_bind_nothing_at_all():
    """The shipped failure: six objectives, six blank refs, zero board bindings —
    so the mission could only ever advance the two the generic sources reach."""
    schema = _repoquest()
    board, _opts = _board_for(schema)
    assert all(_board_objective_binding(board, o.model_dump()) is None
               for m in schema.missions for o in m.objectives)


def test_resolved_refs_become_board_bindings():
    """Each of the three the fixer resolved gets the event its type needs, at the
    granularity that event fires: `OnPiecePlaced` counts one per piece, the other
    two fire once and take the whole count."""
    schema = _repoquest()
    resolve_objective_targets(schema)
    board, _opts = _board_for(schema)
    bound = {o.id: _board_objective_binding(board, o.model_dump())
             for m in schema.missions for o in m.objectives}
    assert bound["obj_3"] == ("OnZoneFilled",    "ConveyorBelts",  1)
    assert bound["obj_5"] == ("OnGroupComplete", "ConflictBlocks", 1)
    assert bound["obj_6"] == ("OnPiecePlaced",   "ConflictBlocks", 1)
    # The two the fixer refused to guess at stay available to SEAM 8's generic
    # trigger and interactable sources, which is where they really did bind.
    assert bound["obj_1"] is None and bound["obj_2"] is None


# ── The typed half: verbs, and the objectives only typing can satisfy ────────

def _readout_cast() -> list[EntitySpec]:
    """A cast with a LOG in it — a zone that is not a drop target. `TryPlace`
    refuses a drag into one, so an objective about it can only be satisfied by
    typing, which is the whole reason `objective_commands` exists."""
    return [
        EntitySpec(name="FileIcons",        kind="prop", count=4),
        EntitySpec(name="FolderContainers", kind="prop", count=2),
        EntitySpec(name="CommitHistoryLog", kind="prop", count=1),
    ]


def _readout_schema() -> UnitySchema:
    return UnitySchema(
        project_name="CodeVault", game_type="2d_topdown", platform="pc",
        render_pipeline="URP", locomotion="none",
        description="Stage the file icons, then record what you staged.",
        entities=_readout_cast(),
        missions=[MissionSpec(id="m1", title="First Commit", objectives=[
            MissionObjective(id="obj_1", description="Stage every file icon",
                             type="collect", count=4),
            MissionObjective(
                id="obj_2",
                description="Record what you staged in the commit history log",
                type="reach"),
        ])],
    )


def test_resolved_refs_derive_the_domain_verbs():
    """No ref, no verb: `board_domain_for_missions` only gives an objective a verb
    when it is about something ON the board, so blank refs leave the parser with
    its built-ins alone."""
    before, after = _readout_schema(), _readout_schema()
    _b, before_opts = _board_for(before)
    resolve_objective_targets(after)
    _b, after_opts = _board_for(after)
    assert [c["verb"] for c in (before_opts.get("commands") or [])] == []
    assert [c["verb"] for c in (after_opts.get("commands") or [])] == ["stage",
                                                                      "record"]


def test_an_objective_about_a_readout_becomes_an_objective_command():
    """A drag into a log is refused by name, so this objective's only route is
    the verb — and `meta.objective_commands` is what tells SEAM 8a to bind it to
    `OnCommand` rather than reporting a live step nothing can clear."""
    schema = _readout_schema()
    resolve_objective_targets(schema)
    board, opts = _board_for(schema)
    assert (opts["meta"]["objective_commands"]) == {"obj_2": "record"}
    assert _board_objective_binding(board, schema.missions[0].objectives[1]
                                    .model_dump()) == ("OnCommand", "record", 1)
    # The piece objective keeps the event that counts each placement — rebinding
    # it to a once-firing verb would complete a count of four on one command.
    assert _board_objective_binding(board, schema.missions[0].objectives[0]
                                    .model_dump()) == ("OnPiecePlaced",
                                                       "FileIcons", 1)


# ── Games with no board ──────────────────────────────────────────────────────

def test_an_embodied_game_scores_on_words_alone():
    """No board means no piece/place split to weight with, but the field is still
    read — by `_check_objective_targets`, and by anything that wants to know what
    the goal is."""
    schema = UnitySchema(
        project_name="Scrapyard", game_type="3D", platform="pc",
        render_pipeline="URP", locomotion="walk",
        description="Collect the scrap.",
        entities=[EntitySpec(name="ScrapPiles", kind="prop", count=6),
                  EntitySpec(name="Player", kind="player", count=1)],
        missions=[MissionSpec(id="m1", title="Salvage", objectives=[
            MissionObjective(id="obj_1", description="Collect every scrap pile",
                             type="collect", count=6)])],
    )
    resolve_objective_targets(schema)
    assert schema.missions[0].objectives[0].target_ref == "ScrapPiles"


def test_a_sentence_that_names_no_entity_is_reported_not_guessed():
    schema = UnitySchema(
        project_name="Scrapyard", game_type="3D", platform="pc",
        render_pipeline="URP", locomotion="walk", description="Get out.",
        entities=[EntitySpec(name="ScrapPiles", kind="prop", count=6)],
        missions=[MissionSpec(id="m1", title="Salvage", objectives=[
            MissionObjective(id="obj_1", description="Escape before dawn",
                             type="reach")])],
    )
    msgs = resolve_objective_targets(schema)
    assert schema.missions[0].objectives[0].target_ref == ""
    assert any("obj_1" in m and "name no entity" in m for m in msgs)
