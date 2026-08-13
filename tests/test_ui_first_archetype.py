"""
The UI-first archetype — what a game with no avatar actually compiles to.

`locomotion="none"` closed the GRADING half (tests/test_non_embodied_games.py):
Step 7 stopped marking a repo-teaching sim NOT PLAYABLE for lacking an avatar
it correctly should not have. The compiler still built one. It composed a walk
controller, a follow camera hunting a Player tag that would never exist, a
Rigidbody2D and a humanoid animator; it packed the board's panels as circles
around a spawn point; and it resolved "CommitNode" and "FileIcon" to whatever
Kenney furniture scored highest, because the asset library holds fifteen HUD
glyph sprites and no UI kit.

These tests pin the four passes that now read the declaration instead:

  layout     UISurface — a board of panels in reading order, not a packed plane
  compose    a pointer controller and a board camera; nothing rides the avatar
  assets     `ui:<element>` — controls are BUILT, never matched to a wrong model
  fixer      no PlayerController boilerplate, so no capsule in the scene

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import pytest

from src.unity_compose import subsystems_for
from src.unity_entity_asset_resolver import resolve_entity_assets
from src.unity_layout import compute_layout
from src.unity_layout_archetype import (LayoutStrictError, select_archetype,
                                        ui_board_layer, ui_element_of)
from src.unity_playability import evaluate
from src.unity_schema import EntitySpec, UnitySchema, is_embodied
from src.unity_spatial import UI_BOARD


def _board(**kw) -> UnitySchema:
    """The CodeVault shape: a git-teaching board. Every entity is kind='prop'
    — that is what schema-fill actually writes for a UI game, and reading
    `kind` alone is what made the compiler treat the whole thing as scenery."""
    base = dict(
        project_name="CodeVault", game_type="2d_topdown", platform="pc",
        render_pipeline="URP", locomotion="none",
        description=(
            "A teaching sim for a git repository. Drag file icons into folder "
            "containers, stage them, then push the branch to the cloud remote. "
            "The commit history is a tree you climb through and reverted "
            "changes drop out of it."),
        entities=[
            EntitySpec(name="TerminalWindow",   kind="prop", count=1, scale=3.0),
            EntitySpec(name="FolderContainers", kind="prop", count=4, scale=1.2,
                       tag="Interactable"),
            EntitySpec(name="FileIcons",        kind="prop", count=9, scale=0.6,
                       tag="Collectible"),
            EntitySpec(name="CommitNodes",      kind="prop", count=6, scale=0.5),
            EntitySpec(name="StageButton",      kind="prop", count=2, scale=0.8),
            EntitySpec(name="BranchLabel",      kind="prop", count=1, scale=0.7),
        ],
    )
    base.update(kw)
    return UnitySchema(**base)


# ── Selection ────────────────────────────────────────────────────────────────

def test_a_non_embodied_game_selects_uisurface():
    arch = select_archetype(_board())
    assert arch.name == "UISurface"
    assert (arch.motion_axis, arch.medium) == ("static", "screen")
    assert arch.spatial_primitive() == UI_BOARD


def test_the_declaration_beats_the_domain_vocabulary():
    """The single most important property of this archetype.

    A game about version control says branch, tree, cloud, push, drop and climb
    in every paragraph, and not one of them describes a place. Scored as
    evidence they would give a game with no body an air medium and a vertical
    axis — which is not a wrong number, it is the wrong primitive, and the
    layout pass hard-fails rather than guess between them.
    """
    arch = select_archetype(_board(description=(
        "Climb the commit tree through the cloud, watch reverted changes "
        "freefall out of the branch, and parachute a hotfix into the sky "
        "of the staging environment. Dive into the diff.")))
    assert arch.name == "UISurface"
    assert arch.medium == "screen"


def test_the_same_words_still_move_an_embodied_game():
    """The guard is the DECLARATION, not a new blocklist: strip
    locomotion='none' and the identical prose picks a vertical primitive
    again."""
    arch = select_archetype(_board(
        locomotion="fly",
        description=("The player wears a wingsuit and skydives from the jump "
                     "ship at 4000 m altitude, pulling the parachute above "
                     "the drop zone.")))
    assert arch.name == "VerticalCorridor"


def test_an_embodied_game_never_reaches_the_board():
    """`static`+`screen` is unreachable without the declaration — there is no
    prose that derives it."""
    for loco in ("walk", "drive", "fly", "swim"):
        arch_or_err = None
        try:
            arch_or_err = select_archetype(_board(locomotion=loco)).name
        except LayoutStrictError:
            arch_or_err = "abstained"
        assert arch_or_err != "UISurface"


# ── Element vocabulary ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("TerminalWindow",   "window"),
    ("FolderContainers", "panel"),
    ("CommitHistoryLog", "list"),
    ("StageButton",      "button"),
    ("BranchLabel",      "label"),
    ("CommitNodes",      "card"),
    ("FileIcons",        "icon"),
])
def test_domain_nouns_read_as_ui_elements(name, expected):
    assert ui_element_of(EntitySpec(name=name, kind="prop")) == expected


def test_an_entity_that_names_no_element_stays_unclassified():
    """None is a real answer: a teaching game about an engine contains an
    engine, and there IS a model of that."""
    assert ui_element_of(EntitySpec(name="CombustionEngine", kind="prop")) is None


def test_layers_separate_content_from_backing_and_chrome():
    assert ui_board_layer(EntitySpec(name="GridBackdrop", kind="prop")) == "backdrop"
    assert ui_board_layer(EntitySpec(name="BranchLabel", kind="prop")) == "overlay"
    assert ui_board_layer(EntitySpec(name="StageButton", kind="prop")) == "board"


# ── Layout ───────────────────────────────────────────────────────────────────

def test_the_board_is_laid_out_even_though_the_game_is_2d():
    """compute_layout returns [] for every 2D project — the spawn-time builders
    own those. They lay content out around a PLAYER, so a non-embodied 2D game
    is the one exception."""
    s = _board()
    assert compute_layout(s)
    assert all(e.instance_positions for e in s.entities)


def test_an_embodied_2d_game_still_gets_no_layout():
    assert compute_layout(_board(locomotion="walk")) == []


def test_content_sits_on_the_board_plane_facing_the_camera():
    s = _board()
    compute_layout(s)
    content = [e for e in s.entities if ui_board_layer(e) == "board"]
    for e in content:
        assert all(p[2] == 0.0 for p in e.instance_positions), e.name


def test_chrome_is_in_front_of_the_board_and_backing_is_behind_it():
    s = _board(entities=_board().entities
               + [EntitySpec(name="GridBackdrop", kind="prop", count=1)])
    compute_layout(s)
    by_name = {e.name: e for e in s.entities}
    # The camera looks from -Z, so larger z is further away.
    assert by_name["BranchLabel"].instance_positions[0][2] < 0.0
    assert by_name["GridBackdrop"].instance_positions[0][2] > 0.0


def test_the_board_is_centred_on_the_origin():
    """The 2D camera preset sits at (0, 0, -10) and the composed board camera
    fits what it can see. A board packed away from the origin would be framed
    from the corner."""
    s = _board()
    compute_layout(s)
    xs = [p[0] for e in s.entities for p in e.instance_positions]
    ys = [p[1] for e in s.entities for p in e.instance_positions]
    assert abs((min(xs) + max(xs)) / 2) < 2.0
    assert abs((min(ys) + max(ys)) / 2) < 2.0


def test_reading_order_puts_the_workspace_before_the_icons():
    """Not size order. `_pack_groups` places the biggest footprint centrally,
    which is right for a world and wrong for an interface."""
    s = _board()
    compute_layout(s)
    by_name = {e.name: e for e in s.entities}

    def top_left(e):
        # Reading order: higher up first, then further left.
        return (-max(p[1] for p in e.instance_positions),
                min(p[0] for p in e.instance_positions))

    assert top_left(by_name["TerminalWindow"]) < top_left(by_name["FileIcons"])


def test_groups_do_not_overlap_on_the_board():
    s = _board()
    compute_layout(s)
    cells = []
    for e in s.entities:
        if ui_board_layer(e) != "board":
            continue
        xs = [p[0] for p in e.instance_positions]
        ys = [p[1] for p in e.instance_positions]
        cells.append((e.name, min(xs), min(ys), max(xs), max(ys)))
    for i, (n1, x1, y1, x2, y2) in enumerate(cells):
        for n2, a1, b1, a2, b2 in cells[i + 1:]:
            apart = x2 < a1 or a2 < x1 or y2 < b1 or b2 < y1
            assert apart, f"{n1} overlaps {n2}"


def test_the_board_layout_is_idempotent():
    s = _board()
    compute_layout(s)
    first = [list(map(list, e.instance_positions)) for e in s.entities]
    compute_layout(s)
    assert [list(map(list, e.instance_positions)) for e in s.entities] == first


# ── Compose ──────────────────────────────────────────────────────────────────

def _kinds(subs) -> set[str]:
    return {s.kind for s in subs}


def test_the_pointer_replaces_the_walk_controller():
    subs = subsystems_for("2d_topdown", [], "none")
    ctrl = next(s for s in subs if s.kind == "controller")
    cam  = next(s for s in subs if s.kind == "camera")
    inp  = next(s for s in subs if s.kind == "input")
    assert (ctrl.variant, cam.variant, inp.variant) == (
        "pointer", "board_framing", "pointer")


def test_nothing_targets_a_player_that_does_not_exist():
    """`target="player"` is what makes scene.unity.j2 build the Player
    GameObject at all — one unit carrying it rebuilds the avatar the schema
    declared it has none of."""
    subs = subsystems_for("2d_topdown", [], "none")
    assert [s.name for s in subs if s.target == "player"] == []


def test_the_avatars_own_subsystems_are_dropped():
    subs = _kinds(subsystems_for("2d_topdown", [], "none"))
    assert not (subs & {"health", "physics", "animator"})
    # The player FSM goes; the enemy FSM is appended later and is unaffected.
    assert "statemachine" not in subs


def test_an_embodied_game_keeps_every_one_of_them():
    subs = _kinds(subsystems_for("2d_topdown", [], "walk"))
    assert {"health", "physics", "animator", "statemachine"} <= subs


def test_the_mission_loop_still_has_event_sources():
    """SEAM 8 binds `reach` to a trigger and `collect`/`interact` to an
    interactable. Both wait for a tagged BODY by default, so a UI game would
    wire its objectives to events that can never fire."""
    subs = subsystems_for("2d_topdown", [], "none")
    trig = next(s for s in subs if s.kind == "trigger")
    inter = next(s for s in subs if s.kind == "interactable")
    assert trig.variant == "click_zone"
    assert inter.variant == "button_switch"
    assert inter.options["detection"] == "pointer"


def test_the_board_reports_3d_colliders_whatever_the_game_type_says():
    """Collider2D has no Raycast(Ray) overload, so a 2d_topdown teaching sim
    emitting the 2D API would compile and never register a click."""
    subs = subsystems_for("2d_topdown", [], "none")
    trig = next(s for s in subs if s.kind == "trigger")
    assert trig.options["meta"]["game_type"] == "3D"
    embodied = subsystems_for("2d_topdown", [], "walk")
    trig2 = next(s for s in embodied if s.kind == "trigger")
    assert trig2.options["meta"]["game_type"] == "2d_topdown"


def test_the_hud_drops_the_health_bar_it_can_no_longer_feed():
    """health_bar's layout IS the bar SEAM 1 fills from the health micro, and
    a game with no player has no health micro."""
    hud = next(s for s in subsystems_for("2d_topdown", [], "none")
               if s.kind == "hud")
    assert hud.variant == "score_panel"


# ── Assets ───────────────────────────────────────────────────────────────────

def _routes(schema) -> dict[str, str]:
    return resolve_entity_assets(schema, used_models={}, all_models={})


def test_ui_entities_are_built_not_matched_to_a_model():
    routes = _routes(_board())
    assert routes["TerminalWindow"]   == "ui:window"
    assert routes["FolderContainers"] == "ui:panel"
    assert routes["FileIcons"]        == "ui:icon"
    assert routes["StageButton"]      == "ui:button"
    assert routes["BranchLabel"]      == "ui:label"
    assert routes["CommitNodes"]      == "ui:card"


def test_an_unmatched_control_becomes_a_labelled_card_not_a_prop():
    """On a board "nothing in the library matches" is the answer, not a
    failure to report or a crate to guess at."""
    s = _board(entities=[EntitySpec(name="MergeConflict", kind="prop", count=2)])
    assert _routes(s)["MergeConflict"] == "ui:card"


def test_an_embodied_game_never_takes_the_ui_route():
    routes = _routes(_board(locomotion="walk"))
    assert not any(p.startswith("ui:") for p in routes.values())


def test_strict_assets_does_not_hard_fail_a_board():
    """[assets.strict] exists to catch an entity that SHOULD have had a model.
    A commit was never going to have one."""
    routes = resolve_entity_assets(_board(), used_models={}, all_models={},
                                   strict=True)
    assert all(p.startswith("ui:") for p in routes.values())


# ── Grading ──────────────────────────────────────────────────────────────────

def test_a_board_with_targets_is_playable():
    s = _board()
    compute_layout(s)
    rep = evaluate(s)
    assert rep.primitive == UI_BOARD
    assert rep.checks["pointer_targets"] is True


def test_a_board_with_nothing_to_click_is_not():
    """The mirror of content_reachable, and the reason abstaining on the nine
    avatar checks leaves no hole."""
    s = _board(entities=[
        EntitySpec(name="GridBackdrop", kind="prop", count=1),
        EntitySpec(name="TitleBanner",  kind="prop", count=1),
    ])
    rep = evaluate(s)
    assert rep.checks["pointer_targets"] is False
    assert any(f.check == "pointer_targets" for f in rep.errors)


def test_a_board_of_props_is_not_a_diorama():
    """schema-fill writes kind='prop' for a UI game's whole cast; reading kind
    alone called a complete click-driven teaching sim scenery."""
    from src.unity_schema import SceneSpec
    s = _board(scene_specs=[SceneSpec(
        name="Repo", description="the working tree",
        entities=["TerminalWindow", "FolderContainers", "FileIcons"])])
    assert evaluate(s).checks["scene_population"] is True


def test_the_same_scene_is_still_a_diorama_for_a_walking_game():
    from src.unity_schema import SceneSpec
    s = _board(locomotion="walk", scene_specs=[SceneSpec(
        name="Repo", description="the working tree",
        entities=["TerminalWindow", "FolderContainers", "FileIcons"])])
    assert evaluate(s).checks["scene_population"] is False


def test_pointer_targets_is_accounted_for_on_an_embodied_game():
    """An ERROR-class check may never be silently absent — it grades, fails,
    or says why it could not. Popping it would shrink the denominator."""
    rep = evaluate(_board(locomotion="walk"))
    assert "pointer_targets" not in rep.checks
    assert "pointer_targets" in rep.not_graded


# ── The fixer ────────────────────────────────────────────────────────────────

def test_the_fixer_stops_injecting_an_avatar():
    """A "player"-type script is what makes scene.unity.j2 build the capsule.
    On the non-compose path it was the only thing standing in the board."""
    from src.unity_pipeline import _check_consistency
    fixed, fixes = _check_consistency(_board())
    assert not any(x.type == "player" for x in fixed.scripts)
    assert any("locomotion='none'" in f for f in fixes)
    assert is_embodied(fixed) is False


def test_the_fixer_still_injects_one_for_a_walking_game():
    from src.unity_pipeline import _check_consistency
    fixed, _ = _check_consistency(_board(locomotion="walk"))
    assert any(x.type == "player" for x in fixed.scripts)
