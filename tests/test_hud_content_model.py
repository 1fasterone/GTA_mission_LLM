"""
HUD content model — widgets, and the seams that publish into them.

Before this change the HUD was the thinnest generator in the set and the one the
player actually reads: ten variants, four knobs, no content field and no composer
options, so a brief asking for an objective line and a carried-item count emitted
the same 22-line one-Slider script as "a health bar".

These tests pin the three properties that make widgets real rather than
decorative: a widget becomes a typed setter on the class, the composer only adds
a widget some composed subsystem can actually publish into, and every widget the
composer adds is wired by a seam (SEAM 1/9/10) instead of sitting inert.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.hud import HUDPipeline, HUDSchema, HudWidget, hud_setters
from src.unity_compose import MicroComposer, subsystems_for
from src.unity_schema import EntitySpec, MissionObjective, MissionSpec


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dummy_config():
    """Pipeline ctor needs LMStudio config, but these tests never call the LLM."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _project(tmp_path, project="HudProof", **kw) -> Path:
    subs = subsystems_for(kw.pop("game_type", "3D"), kw.pop("features", []), **kw)
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project, project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets)
    return assets


def _hud_cs(assets: Path) -> str:
    d = assets / "Micros" / "hud" / "Scripts"
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(d.glob("*.cs")))


def _gamewiring(assets: Path) -> str:
    return (assets / "Micros" / "wiring" / "Scripts"
            / "GameWiring.cs").read_text(encoding="utf-8")


def _fixed(schema, variant="minimal"):
    """default_fix without needing a live config (no template rendering)."""
    return HUDPipeline.default_fix(HUDPipeline.__new__(HUDPipeline), schema, variant)


_MISSION = MissionSpec(id="m1", title="Vault Run", objectives=[
    MissionObjective(id="grab_cores", description="Collect 3 cores",
                     type="collect", count=3)])


# ── Widgets become real emitted members ───────────────────────────────────────

@pytest.mark.parametrize("kind,csharp_type,arg", [
    ("bar",       "Slider",   "float current"),
    ("counter",   "TMP_Text", "long value"),
    ("timer",     "TMP_Text", "float seconds"),
    ("objective", "TMP_Text", "string description"),
    ("text",      "TMP_Text", "string value"),
])
def test_each_widget_kind_emits_a_typed_setter(dummy_config, tmp_path,
                                               kind, csharp_type, arg):
    p = HUDPipeline(dummy_config)
    s = _fixed(HUDSchema(name="DiveHud", variant="crosshair",
                         widgets=[HudWidget(id="Oxygen", kind=kind, label="OXYGEN")]),
               "crosshair")
    p.write_files(s, "crosshair", tmp_path)
    cs = (tmp_path / "Scripts" / "DiveHud.cs").read_text(encoding="utf-8")
    assert f"private {csharp_type} oxygen" in cs
    assert f"public void SetOxygen({arg}" in cs


def test_lowercase_id_still_yields_a_pascal_case_setter():
    s = _fixed(HUDSchema(widgets=[HudWidget(id="oxygen", kind="bar")]), "crosshair")
    # to_pascal leaves an identifier-shaped string alone, which would have given
    # `Setoxygen` — _widget_ident exists precisely to prevent that.
    assert s.widgets[0].setter == "SetOxygen"


def test_widgetless_hud_is_unchanged(dummy_config, tmp_path):
    """Widgets are additive: no widgets must render the pre-widget baseline."""
    p = HUDPipeline(dummy_config)
    for i, widgets in enumerate(([], None)):
        s = HUDSchema(name="Plain", variant="health_bar")
        if widgets:
            s.widgets = widgets
        out = tmp_path / str(i)
        out.mkdir()
        p.write_files(_fixed(s, "health_bar"), "health_bar", out)
    a = (tmp_path / "0" / "Scripts" / "Plain.cs").read_text(encoding="utf-8")
    b = (tmp_path / "1" / "Scripts" / "Plain.cs").read_text(encoding="utf-8")
    assert a == b
    assert "Widgets" not in a


# ── Collision lint: a widget must never break the build it decorates ──────────

def test_widget_colliding_with_the_variant_baseline_is_dropped():
    """health_bar already emits SetHealth; a second one would be CS0111."""
    s = _fixed(HUDSchema(variant="health_bar",
                         widgets=[HudWidget(id="Health", kind="bar"),
                                  HudWidget(id="Depth",  kind="bar")]),
               "health_bar")
    assert [w.id for w in s.widgets] == ["Depth"]


def test_duplicate_widgets_are_collapsed():
    s = _fixed(HUDSchema(variant="crosshair",
                         widgets=[HudWidget(id="Depth", kind="bar"),
                                  HudWidget(id="depth", kind="bar"),
                                  HudWidget(id="", kind="text")]),
               "crosshair")
    assert [w.id for w in s.widgets] == ["Depth"]


def test_unknown_kind_clamps_to_text():
    assert HudWidget(id="X", kind="hologram").kind == "text"


def test_hud_setters_reports_the_whole_class_surface():
    s = _fixed(HUDSchema(variant="racing_hud",
                         widgets=[HudWidget(id="Objective", kind="objective")]),
               "racing_hud")
    setters = hud_setters("racing_hud", s.widgets)
    assert "SetSpeed" in setters        # variant baseline
    assert "SetObjective" in setters    # widget
    assert "SetHealth" not in setters   # racing_hud shows no HP of its own


# ── The composer only adds widgets something can publish into ─────────────────

def test_objective_widget_appears_only_when_a_mission_has_objectives(tmp_path):
    with_obj = _project(tmp_path / "a", missions=[_MISSION])
    assert "public void SetObjective(" in _hud_cs(with_obj)

    without = _project(tmp_path / "b", missions=[
        MissionSpec(id="m1", title="Empty", objectives=[])])
    assert "public void SetObjective(" not in _hud_cs(without)


def test_carried_widget_appears_only_with_an_inventory(tmp_path):
    with_inv = _project(tmp_path / "a", features=["inventory"], missions=[_MISSION])
    assert "public void SetCarried(" in _hud_cs(with_inv)

    without = _project(tmp_path / "b", missions=[_MISSION])
    assert "public void SetCarried(" not in _hud_cs(without)


# ── Every composer-added widget is driven by a seam ───────────────────────────

def test_seam9_wires_objectives_into_the_hud(tmp_path):
    wiring = _gamewiring(_project(tmp_path, missions=[_MISSION]))
    assert "SEAM 9" in wiring
    # Both events, because ReportProgress raises OnObjectiveProgress BEFORE it
    # marks the objective complete — a progress-only binding shows a stale line.
    assert "OnObjectiveProgress += _onObjectiveHudProgress" in wiring
    assert "OnObjectiveComplete += _onObjectiveHudComplete" in wiring
    assert "SetObjective(label, done, total)" in wiring
    assert "OnObjectiveProgress -= _onObjectiveHudProgress" in wiring


# ── One readout, every tracker ───────────────────────────────────────────────

_MISSIONS_3 = [
    MissionSpec(id="m1", title="Connect the Forest", objectives=[
        MissionObjective(id="obj_1", type="collect", target_ref="Crates",
                         description="Select at least 5 nodes."),
        MissionObjective(id="obj_2", type="reach", target_ref="Crates",
                         description="Connect the selected nodes.")]),
    MissionSpec(id="m2", title="Initialize Pipeline", objectives=[
        MissionObjective(id="obj_3", type="collect", target_ref="Crates",
                         description="Place an artifact on the belt.")]),
    MissionSpec(id="m3", title="Merge Resolution", objectives=[
        MissionObjective(id="obj_4", type="reach", target_ref="Crates",
                         description="Align the conflict markers.")]),
]


def test_seam9_binds_every_tracker_not_just_the_first(tmp_path):
    """One tracker per mission, one readout. Bound to the first alone, the tally
    froze at that mission's total while the other objectives advanced live —
    under a rules panel that states every one of them as a step."""
    wiring = _gamewiring(_project(tmp_path, missions=_MISSIONS_3))
    for cls in ("_connecttheforestobjectives", "_initializepipelineobjectives",
                "_mergeresolutionobjectives"):
        assert f"{cls}.OnObjectiveProgress += _onObjectiveHudProgress" in wiring
        assert f"{cls}.OnObjectiveComplete += _onObjectiveHudComplete" in wiring
    assert wiring.count("SEAM 9") == 3


def test_the_tally_sums_every_tracker(tmp_path):
    """`done`/`total` are the whole project's, or the readout counts a third of
    the progression the step panel walks the player through."""
    wiring = _gamewiring(_project(tmp_path, missions=_MISSIONS_3))
    assert wiring.count("done  += ") == 3
    assert wiring.count("total += ") == 3
    assert "SetObjective(label, done, total)" in wiring


def test_the_label_is_the_first_tracker_still_running(tmp_path):
    """Schema order — which is the order the rules micro states the steps in, so
    the hint bar and the HUD name the same action."""
    wiring = _gamewiring(_project(tmp_path, missions=_MISSIONS_3))
    assert wiring.count("if (label.Length == 0 && a") == 3


def test_every_subscription_is_unsubscribed(tmp_path):
    """The delegates are created once and shared, so OnDestroy has to remove the
    same instance from all three events — re-assigning per tracker would leave
    two live subscriptions firing into a destroyed HUD."""
    wiring = _gamewiring(_project(tmp_path, missions=_MISSIONS_3))
    assert wiring.count("OnObjectiveProgress +=") == 3
    assert wiring.count("OnObjectiveProgress -=") == 3
    assert wiring.count("OnObjectiveComplete +=") == 3
    assert wiring.count("OnObjectiveComplete -=") == 3
    assert wiring.count("private Action<string, int, int> _onObjectiveHudProgress") == 1


def test_seam10_wires_inventory_into_the_hud(tmp_path):
    wiring = _gamewiring(_project(tmp_path, features=["inventory"], missions=[_MISSION]))
    assert "SEAM 10" in wiring
    assert "OnChanged += _onInventoryHud" in wiring
    assert "SetCarried(" in wiring
    assert "OnChanged -= _onInventoryHud" in wiring


def test_inventory_exposes_the_total_the_seam_binds_to(tmp_path):
    assets = _project(tmp_path, features=["inventory"], missions=[_MISSION])
    inv = "\n".join(
        f.read_text(encoding="utf-8")
        for f in sorted((assets / "Micros" / "inventory" / "Scripts").glob("*.cs")))
    assert "public int TotalCount" in inv


def test_racing_hud_gets_a_health_readout_via_seam1(tmp_path):
    """racing_hud owns no SetHealth, so before the widget it showed no HP at all
    and SEAM 1 (gated on variant == health_bar) never fired."""
    assets = _project(tmp_path, genre="racing", user_prompt="kart racing",
                      missions=[_MISSION])
    assert "public void SetHealth(" in _hud_cs(assets)
    wiring = _gamewiring(assets)
    assert "SEAM 1" in wiring
    assert "OnHealthChanged += " in wiring


def test_every_seam_target_is_a_setter_the_class_declares(tmp_path):
    """The anti-CS1061 invariant: GameWiring may only call HUD methods that the
    emitted HUD class actually has."""
    assets = _project(tmp_path, features=["inventory"], genre="racing",
                      user_prompt="kart racing", missions=[_MISSION],
                      entities=[EntitySpec(name="Rival", kind="enemy", count=4)])
    hud, wiring = _hud_cs(assets), _gamewiring(assets)
    for setter in ("SetObjective", "SetCarried", "SetHealth"):
        if setter in wiring:
            assert f"public void {setter}(" in hud, (
                f"GameWiring calls {setter} but the HUD does not declare it")
