"""Cover points must come from the layout the scene actually spawns.

Step 5d (src/unity_cover_points.py) bakes cover points from the Step 5 layout.
Step 6 (UnityProjectGenerator.generate) then re-lays the scene out with
measured model footprints — moving obstacle and enemy groups — and only AFTER
that does composer.emit() render the cover_shooter AI. Before this fix the AI
shipped the Step 5 points: cover positions beside where the rocks USED to be.

These tests pin both halves of the fix:
  * _model_aware_relayout re-bakes schema.meta["cover_points"] from the
    layout it produces;
  * refresh_cover_points re-points the already-built AI unit at them before
    emit (pipeline and `unity-validate --compose` both call it).
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from src.unity_compose import MicroComposer, Subsystem, refresh_cover_points, subsystems_for
from src.unity_cover_points import compute_cover_points
from src.unity_generator import UnityProjectGenerator
from src.unity_layout import compute_layout
from src.unity_schema import EntitySpec, UnitySchema

_COVER_PROMPT = "guards take cover behind boulders and shoot at the player"


def _arena_after_step5() -> UnitySchema:
    """A ground-plane arena exactly as Step 5 leaves it: laid out, cover baked."""
    s = UnitySchema(project_name="CoverRelayout", game_type="3D", platform="pc",
                    render_pipeline="URP", locomotion="walk")
    s.entities = [
        EntitySpec(name="Player", kind="player", tag="Player", count=1, placement="single"),
        EntitySpec(name="Guards", kind="enemy", tag="Enemy", count=4,
                   placement="scatter", spacing=3.0),
        EntitySpec(name="Boulders", kind="rock", count=6, placement="scatter", spacing=2.0),
    ]
    compute_layout(s)
    compute_cover_points(s)
    return s


def _positions(schema: UnitySchema, name: str) -> list:
    return next(e for e in schema.entities if e.name == name).instance_positions


def _total(table: dict) -> int:
    return sum(len(v) for v in table.values())


# ── the generator re-bakes what its re-layout invalidates ─────────────────────

def test_scenario_really_moves_the_cover_geometry():
    """Guard on the premise: in this arena the Step 6 re-layout does move the
    boulders, and cover computed from the final layout differs from Step 5's.
    If a layout change ever made them coincide, the tests below would pass
    without proving anything."""
    s = _arena_after_step5()
    step5_boulders = copy.deepcopy(_positions(s, "Boulders"))
    step5_cover = copy.deepcopy(s.meta["cover_points"])
    assert _total(step5_cover) > 0

    UnityProjectGenerator._model_aware_relayout(s, {"Boulders": 1.5})

    assert _positions(s, "Boulders") != step5_boulders
    final = copy.deepcopy(s)
    compute_cover_points(final)
    assert final.meta["cover_points"] != step5_cover


def test_model_aware_relayout_rebakes_cover_points_from_the_final_layout():
    for footprints in ({"Boulders": 1.5}, {"Guards": 2.5}, {"Boulders": 3.0, "Guards": 3.0}):
        s = _arena_after_step5()
        UnityProjectGenerator._model_aware_relayout(s, footprints)
        expected = copy.deepcopy(s)
        compute_cover_points(expected)
        assert s.meta["cover_points"] == expected.meta["cover_points"], footprints


def test_model_aware_relayout_leaves_unmeasured_groups_in_place():
    """Only groups with a measured footprint and a packable placement are
    re-packed; a group with no footprint keeps its positions and is avoided."""
    s = _arena_after_step5()
    guards = copy.deepcopy(_positions(s, "Guards"))
    UnityProjectGenerator._model_aware_relayout(s, {"Boulders": 1.5})
    assert _positions(s, "Guards") == guards


# ── the already-built AI unit is re-pointed before emit ───────────────────────

def _ai(subs: list) -> Subsystem:
    return next(sub for sub in subs if sub.kind == "ai")


def test_refresh_repoints_cover_shooter_at_final_points():
    s = _arena_after_step5()
    subs = subsystems_for("3D", [], user_prompt=_COVER_PROMPT, entities=s.entities,
                          cover_points=s.meta["cover_points"])
    stale = copy.deepcopy(_ai(subs).options["cover_points"])
    assert stale == s.meta["cover_points"]["Guards"]

    UnityProjectGenerator._model_aware_relayout(s, {"Boulders": 1.5})
    msgs = refresh_cover_points(subs, s.entities, s.meta["cover_points"])

    assert _ai(subs).options["cover_points"] == s.meta["cover_points"]["Guards"]
    assert _ai(subs).options["cover_points"] != stale
    assert len(msgs) == 1 and "re-pointed at the final layout" in msgs[0]


def test_refresh_is_silent_when_nothing_moved():
    s = _arena_after_step5()
    subs = subsystems_for("3D", [], user_prompt=_COVER_PROMPT, entities=s.entities,
                          cover_points=s.meta["cover_points"])
    before = copy.deepcopy(_ai(subs).options)
    assert refresh_cover_points(subs, s.entities, s.meta["cover_points"]) == []
    assert _ai(subs).options == before


def test_refresh_leaves_units_without_cover_logic_alone():
    enemy = EntitySpec(name="Guard", kind="enemy")
    patrol = Subsystem(kind="ai", variant="navmesh_patrol", name="EnemyAI", options={})
    hud = Subsystem(kind="hud", variant="minimal", name="HUD", options={"cover_points": "x"})
    table = {"Guard": [{"position": [1.0, 0.0, 1.0], "cover_direction": [0.0, 0.0, -1.0]}]}
    assert refresh_cover_points([patrol, hud], [enemy], table) == []
    assert "cover_points" not in patrol.options
    assert hud.options == {"cover_points": "x"}


def test_refresh_clears_points_when_the_final_layout_has_no_cover():
    enemy = EntitySpec(name="Guard", kind="enemy")
    stale = [{"position": [1.0, 0.0, 1.0], "cover_direction": [0.0, 0.0, -1.0]}]
    unit = Subsystem(kind="ai", variant="cover_shooter", name="EnemyAI",
                     options={"cover_points": list(stale)})
    msgs = refresh_cover_points([unit], [enemy], {})
    assert unit.options["cover_points"] == []
    assert "0 point(s), was 1" in msgs[0]


def test_emitted_ai_carries_final_cover_not_the_step5_bake(tmp_path):
    """End to end over the real composer: build the unit from Step 5 (as the
    pipeline must), re-lay out as Step 6 does, refresh, emit — the AI.cs holds
    exactly the final layout's cover coordinates, not the Step 5 bake."""
    s = _arena_after_step5()
    subs = subsystems_for("3D", [], user_prompt=_COVER_PROMPT, entities=s.entities,
                          cover_points=s.meta["cover_points"])
    step5 = copy.deepcopy(s.meta["cover_points"]["Guards"])

    UnityProjectGenerator._model_aware_relayout(s, {"Boulders": 1.5})
    refresh_cover_points(subs, s.entities, s.meta["cover_points"])
    final = s.meta["cover_points"]["Guards"]

    units = MicroComposer("CoverRelayout").emit(subs, Path(tmp_path) / "Assets")
    ai_unit = next(u for u in units if u.kind == "ai")
    ai_cs = (Path(tmp_path) / "Assets" / "Micros" / "ai" / "Scripts"
             / f"{ai_unit.class_name}.cs").read_text(encoding="utf-8")

    block = re.search(r"_coverPoints = new Vector3\[\] \{ (.*?) \};", ai_cs)
    assert block is not None
    rendered = re.findall(r"new Vector3\(([^)]*)\)", block.group(1))

    def render(p: dict) -> str:   # AI.cs.j2 formats each component "%.2f" + "f"
        return ", ".join(f"{c:.2f}f" for c in p["position"])

    assert rendered == [render(p) for p in final]
    assert rendered != [render(p) for p in step5]
