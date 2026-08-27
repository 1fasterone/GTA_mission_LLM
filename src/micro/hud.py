"""Canvas / UI Toolkit HUD micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal


from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import console, to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, csharp_ident, lint_extensible, clamp_enum,
    neutralize_unavailable_names, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


HUDVariant = Literal[
    "health_bar", "ammo_counter", "minimap",
    "crosshair", "interaction_prompt", "score_panel",
    "pause_menu", "dialogue_box", "full_hud_bundle",
    "racing_hud",
]
VARIANTS = list(HUDVariant.__args__)   # type: ignore[attr-defined]

Backend = Literal["uGUI", "UIToolkit"]
Anchor  = Literal["TL", "TR", "BL", "BR", "center"]
Binding = Literal["events", "find"]

# ── Widgets: the HUD's content model ──────────────────────────────────────────
# `variant` alone can only say WHICH of ten prebuilt layouts to emit, so a brief
# asking for oxygen, depth, sonar contacts and a compass produced the same
# 22-line one-Slider script as "a health bar". Widgets are where that content
# lands: each one becomes a serialized reference, a typed setter, and (on the
# UI Toolkit backend) a UXML element + USS class.
WidgetKind = Literal["bar", "counter", "text", "timer", "objective"]
_VALID_WIDGET_KIND = frozenset(WidgetKind.__args__)   # type: ignore[attr-defined]

# What each kind emits. Suffix keeps two widgets of different kinds from
# colliding on one id; the C# type is the uGUI component the field holds.
_KIND_SPEC: dict[str, tuple[str, str]] = {
    "bar":       ("Bar",   "Slider"),
    "counter":   ("Label", "TMP_Text"),
    "text":      ("Label", "TMP_Text"),
    "timer":     ("Label", "TMP_Text"),
    "objective": ("Label", "TMP_Text"),
}


def _widget_ident(s: str, fallback: str = "Value") -> str:
    """PascalCase identifier for a widget id — see code_fields.csharp_ident."""
    return csharp_ident(s, fallback)


class HudWidget(BaseModel):
    """One readout on the HUD. `source` is advisory metadata for the composer and
    the rendered README — it never changes the emitted C#, which stays a pure
    function of id/kind/label."""
    id:        str
    kind:      str   = "text"
    label:     str   = ""
    source:    str   = ""      # health | score | ammo | objectives | custom
    max_value: float = 100.0   # bar only

    @field_validator("kind", mode="before")
    @classmethod
    def _clamp_kind(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_WIDGET_KIND else "text"

    @property
    def ident(self) -> str:
        return _widget_ident(self.id)

    @property
    def field_name(self) -> str:
        i = self.ident
        return i[:1].lower() + i[1:] + _KIND_SPEC[self.kind][0]

    @property
    def csharp_type(self) -> str:
        return _KIND_SPEC[self.kind][1]

    @property
    def setter(self) -> str:
        return f"Set{self.ident}"

    @property
    def display_label(self) -> str:
        return self.label or self.ident


class HUDSchema(CodeExtensibleSchema):
    name:        str     = "HUD"
    variant:     str     = "health_bar"
    backend:     Backend = "uGUI"
    anchor:      Anchor  = "TL"
    binding:     Binding = "events"
    animate:     bool    = False
    widgets:     list[HudWidget] = Field(default_factory=list)
    description: str     = "HUD element"
    meta:        dict    = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "health_bar"

    @field_validator("backend", mode="before")
    @classmethod
    def _clamp_backend(cls, v: str) -> str:
        return clamp_enum(v, ("uGUI", "UIToolkit"), "uGUI")

    @field_validator("anchor", mode="before")
    @classmethod
    def _clamp_anchor(cls, v: str) -> str:
        return clamp_enum(v, ("TL", "TR", "BL", "BR", "center"), "TL")

    @field_validator("binding", mode="before")
    @classmethod
    def _clamp_binding(cls, v: str) -> str:
        return clamp_enum(v, ("events", "find"), "events")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity HUD element for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- EVERY separate readout the player needs to see, named individually (an oxygen
  gauge, a depth meter, a sonar contact count, a compass bearing, the current
  objective) — be specific and exhaustive, this is the HUD's content
- Which of those is a filling bar, a running count, a countdown, or a line of text
- How it's positioned on screen
- uGUI Canvas or UI Toolkit (UXML/USS) as the backend
- Whether it listens to events or polls via FindObjectOfType
- Show/hide animation if any

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
BACKEND — uGUI | UIToolkit
ANCHOR  — TL | TR | BL | BR | center
BINDING — events | find

WIDGETS — the readouts this HUD shows. This is the important field: the variant
only picks a prebuilt layout, so anything the brief asks for that is not in the
variant must appear here or it will not exist. One entry per readout.
  kind:   bar       — a filling meter (oxygen, stamina, shield); set max_value
          counter   — a running total (contacts, cores, kills)
          timer     — seconds, rendered as m:ss (time left, dive time)
          objective — the current objective line plus its done/total tally
          text      — anything else (a compass bearing, a status word)
  id:     short PascalCase name, unique — becomes the C# setter (id "Oxygen"
          gives SetOxygen). Do NOT reuse a name the variant already owns.
  label:  what the player reads on screen; defaults to the id.
  source: health | score | ammo | objectives | custom — where the value comes
          from. Advisory only; use "custom" when nothing else fits.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "backend": "uGUI",
  "anchor": "TL",
  "binding": "events",
  "animate": false,
  "widgets": [
    {{"id": "Oxygen", "kind": "bar", "label": "OXYGEN", "source": "custom", "max_value": 100}},
    {{"id": "Contacts", "kind": "counter", "label": "CONTACTS", "source": "custom"}}
  ],
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class HUDPipeline(UnityMicroPipeline):
    kind            = "hud"
    display_name    = "Unity HUD"
    schema_cls      = HUDSchema
    variants        = VARIANTS
    default_variant = "health_bar"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "hud"

    _RESERVED_FIELD_NAMES = {
        "bar", "label", "body", "group", "mapimage", "mapcamera",
        "crosshairimage", "document", "_root",
        # racing_hud template-owned refs
        "speedtext", "laptext", "positiontext", "cointext", "boostmeter",
        "countdowntext", "countdowngroup",
        # full_hud_bundle template-owned refs
        "healthbar", "ammotext", "scoretext",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.UI", "TMPro", "UnityEngine.UIElements"}

    # Methods each variant's baseline already declares. A widget whose setter
    # collides with one would be CS0111 (duplicate member), so it is dropped
    # rather than emitted — the variant's own version already does that job.
    _VARIANT_SETTERS: dict[str, set[str]] = {
        "health_bar":         {"SetHealth"},
        "ammo_counter":       {"SetAmmo"},
        "score_panel":        {"SetScore"},
        "interaction_prompt": {"Show", "Hide"},
        "dialogue_box":       {"ShowLine"},
        "pause_menu":         {"SetPaused"},
        "minimap":            set(),
        "crosshair":          set(),
        "racing_hud":         {"SetSpeed", "SetLap", "SetPosition", "SetCoins",
                               "SetBoost", "ShowCountdown", "HideCountdown", "Ordinal"},
        "full_hud_bundle":    {"SetHealth", "SetAmmo", "SetScore"},
    }

    # The serialized refs each variant's baseline actually declares (HUD.cs.j2).
    # Distinct from _RESERVED_FIELD_NAMES, which is the variant-agnostic union
    # used to police LLM-authored extra_fields: applying that union to widgets
    # dropped racing_hud's health readout because `healthBar` is a field of
    # full_hud_bundle — a variant racing_hud shares no code with.
    _VARIANT_FIELDS: dict[str, set[str]] = {
        "health_bar":         {"bar"},
        "ammo_counter":       {"label"},
        "score_panel":        {"label"},
        "interaction_prompt": {"body", "group"},
        "dialogue_box":       {"body", "group"},
        "minimap":            {"mapimage", "mapcamera"},
        "crosshair":          {"crosshairimage"},
        "pause_menu":         {"group"},
        "racing_hud":         {"speedtext", "laptext", "positiontext", "cointext",
                               "boostmeter", "countdowntext", "countdowngroup",
                               "mapimage", "mapcamera"},
        "full_hud_bundle":    {"healthbar", "ammotext", "scoretext",
                               "mapimage", "mapcamera"},
    }
    # Declared ONLY when backend == "UIToolkit" (HUD.cs.j2's UI Toolkit
    # branch is a sibling of the uGUI branch that owns _VARIANT_FIELDS below,
    # never both at once — the name used to claim "every variant, both
    # backends", which was backwards: under backend == "uGUI" neither
    # `document` nor `_root` is declared at all. See default_fix, which now
    # also strips any LLM-authored reference to whichever set the schema's
    # actual backend didn't pick — the collision check below predates that
    # fix and is not itself what caught the live bug (widget field names
    # always carry a Bar/Label/... suffix, so they can never equal these
    # bare names; kept for the variant-field half of the union, which does
    # matter).
    _UITOOLKIT_FIELDS = {"document", "_root"}

    def _lint_widgets(self, schema, variant) -> list[str]:
        """Drop widgets that cannot compile alongside the variant baseline, and
        de-duplicate the rest. Returns human-readable notes (never raises) so the
        caller can report exactly what was dropped instead of silently shrinking
        the HUD."""
        owned_setters = self._VARIANT_SETTERS.get(variant, set())
        # uGUI and UI Toolkit fields are mutually exclusive in the template —
        # union them only for whichever backend the schema actually resolved to.
        owned_fields = (self._UITOOLKIT_FIELDS if schema.backend == "UIToolkit"
                        else self._VARIANT_FIELDS.get(variant, set()))
        kept: list[HudWidget] = []
        seen_fields: set[str] = set()
        seen_setters: set[str] = set()
        notes: list[str] = []
        for w in schema.widgets:
            if not str(w.id).strip():
                notes.append("hud widget with an empty id dropped")
                continue
            if w.setter in owned_setters:
                notes.append(f"hud widget {w.id!r} dropped: {w.setter}() is already "
                             f"emitted by the {variant} baseline")
                continue
            if w.field_name.lower() in owned_fields:
                notes.append(f"hud widget {w.id!r} dropped: field {w.field_name} "
                             f"collides with a {variant} template field")
                continue
            if w.field_name in seen_fields or w.setter in seen_setters:
                notes.append(f"hud widget {w.id!r} dropped: duplicate of an earlier widget")
                continue
            seen_fields.add(w.field_name)
            seen_setters.add(w.setter)
            kept.append(w)
        schema.widgets = kept
        return notes

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "HUD")
        # Crosshair is always centered
        if variant == "crosshair":
            schema.anchor = "center"
        # Widgets are purely additive: an empty list emits exactly the variant
        # baseline, so every pre-widget project renders byte-identically.
        for note in self._lint_widgets(schema, variant):
            console.print(f"  [yellow]WARN[/]  {note}")
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        # `reserved_fields` above only ever guarded against the LLM DECLARING
        # a colliding field name — it never guaranteed `document`/`_root` (or
        # a uGUI variant's own refs) actually EXIST for the backend this
        # schema resolved to. Strip whichever half the LLM wasn't entitled to
        # (hit live: a uGUI-backend HUD whose awake_extra/extra_methods
        # referenced `_root` — CS0103, since HUD.cs.j2 only declares it under
        # backend == "UIToolkit").
        unavailable = (self._VARIANT_FIELDS.get(variant, set())
                       if schema.backend == "UIToolkit" else self._UITOOLKIT_FIELDS)
        neutralize_unavailable_names(schema, unavailable)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("HUD.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1

        if schema.backend == "UIToolkit":
            ui_dir = out_dir / "UI"
            ui_dir.mkdir(parents=True, exist_ok=True)
            (ui_dir / f"{schema.name}.uxml").write_text(
                env.get_template("HUD.uxml.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            (ui_dir / f"{schema.name}.uss").write_text(
                env.get_template("HUD.uss.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 2

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count


def hud_setters(variant: str, widgets: list[HudWidget]) -> set[str]:
    """Every public setter the emitted HUD class will declare: the variant
    baseline's own, plus one per widget that survived `_lint_widgets`.

    The composer gates its HUD seams on this rather than on the variant name, so
    a seam can never call a method the class does not declare (CS1061) and never
    skips one it does. Call it with the POST-`default_fix` widget list.
    """
    return set(HUDPipeline._VARIANT_SETTERS.get(variant, set())) | {
        w.setter for w in widgets
    }
