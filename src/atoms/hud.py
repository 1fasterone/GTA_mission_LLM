"""HUD / UI display atoms — show, hide, and update screen-space UI elements.

These target the canonical Unity HUD layout:
  GameObject "HUD" -> CanvasGroup (alpha/interactable)
  "HealthBar"       -> UI.Slider (value = 0..1)
  "AmmoText"        -> UI.Text or TMP_Text
  "ObjectiveText"   -> UI.Text or TMP_Text

Generators are expected to create matching GameObjects; these atoms drive them.
"""

from .base import AtomDef, AtomParam

HUD_ATOMS: list[AtomDef] = [
    AtomDef(
        name="ShowHUD",
        category="hud",
        description="Fade in the HUD canvas group (instant when duration=0).",
        duration="instant",
        side_effects=["ui_visibility"],
        aliases=["show_hud", "display_hud", "hud_on"],
        engines=["unity"],
        params=[
            AtomParam(name="duration", type="float", default=0.0, description="Fade-in time in seconds (0 = instant)"),
        ],
    ),
    AtomDef(
        name="HideHUD",
        category="hud",
        description="Fade out the HUD canvas group (instant when duration=0).",
        duration="instant",
        side_effects=["ui_visibility"],
        aliases=["hide_hud", "hud_off", "clear_hud"],
        engines=["unity"],
        params=[
            AtomParam(name="duration", type="float", default=0.0, description="Fade-out time in seconds (0 = instant)"),
        ],
    ),
    AtomDef(
        name="UpdateHealth",
        category="hud",
        description="Set the health bar UI slider to value/max_value.",
        duration="instant",
        side_effects=["ui_value"],
        aliases=["update_health", "set_health_bar", "health_ui"],
        engines=["unity"],
        params=[
            AtomParam(name="value",     type="float", default=100.0, description="Current health value"),
            AtomParam(name="max_value", type="float", default=100.0, description="Maximum health (denominator)"),
        ],
    ),
    AtomDef(
        name="UpdateAmmo",
        category="hud",
        description="Update the ammo counter text to 'current / reserve'.",
        duration="instant",
        side_effects=["ui_text"],
        aliases=["update_ammo", "set_ammo", "ammo_ui"],
        engines=["unity"],
        params=[
            AtomParam(name="current", type="int", default=30, description="Rounds in current magazine"),
            AtomParam(name="reserve", type="int", default=90, description="Reserve ammunition count"),
        ],
    ),
    AtomDef(
        name="UpdateObjective",
        category="hud",
        description="Set the objective text label to a new string.",
        duration="instant",
        side_effects=["ui_text"],
        aliases=["update_objective", "set_objective", "objective_ui", "show_objective"],
        engines=["unity"],
        params=[
            AtomParam(name="text",        type="string", default="Complete the objective", description="Objective text to display"),
            AtomParam(name="show_marker", type="bool",   default=True, description="Show waypoint marker on minimap"),
        ],
    ),
]
