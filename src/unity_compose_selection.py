"""Persist composition choices at the Step-5/6 boundary.

Selection may interpret a brief; generation and saved-schema replay may not.
The versioned record stores the ordered subsystems and their complete options,
including prompt-derived lighting/audio/board decisions, so those helpers cannot
re-decide the game when replaying a saved schema. Authored code still round-trips
through microgens; geometry-dependent bindings are refreshed by the usual passes.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict
from rich.console import Console

from .unity_compose import Subsystem
from .unity_projectile_contract import PHYSICAL_PROJECTILE_META_KEY
from .unity_schema import UnitySchema

SELECTION_KEY = "compose_selection"
console = Console()


class ComposeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    stage: Literal["step5"] = "step5"
    subsystems: list[Subsystem]
    disagreements: list[dict]
    atmosphere: bool
    climbing_contract: bool


def palette_rgb(schema) -> dict:
    from .unity_generator import _unity_color
    pal = getattr(schema, "color_palette", None)
    if pal is None:
        return {}
    return {slot: _unity_color(getattr(pal, slot))
            for slot in ("primary", "secondary", "accent", "background", "trim")}


def select_subsystems(schema: UnitySchema, user_prompt: str,
                      features: list[str] | None = None) -> list[Subsystem]:
    """Pre-generation selection pass; this is the only schema adapter reading a brief."""
    from .unity_compose import subsystems_for
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    return subsystems_for(schema.game_type,
                          list(schema.features if features is None else features),
                          locomotion=schema.locomotion,
                          locomotion_modes=schema.locomotion_modes,
                          flying_abilities=meta.get("flying_abilities"),
                          capabilities=schema.capabilities,
                          user_prompt=user_prompt,
                          scenes=schema.scenes,
                          entities=schema.entities,
                          scripts=schema.scripts,
                          genre=meta.get("genre", "universal"),
                          audio_specs=schema.audio_specs,
                          vfx_specs=schema.vfx,
                          shaders=schema.shaders,
                          animations=schema.animations,
                          characters=schema.characters,
                          missions=schema.missions,
                          behavior_trees=schema.behavior_trees,
                          environment=schema.environment,
                          visual_style=schema.visual_style,
                          palette_rgb=palette_rgb(schema),
                          cover_points=meta.get("cover_points"),
                          render_pipeline=schema.render_pipeline,
                          projectile_contract=meta.get(PHYSICAL_PROJECTILE_META_KEY))


def persist_selection(schema: UnitySchema, user_prompt: str) -> list[str]:
    """Resolve once before generation and persist choices plus disagreements.

    The comparison is against schema-derived defaults, not a claim that every
    default was explicitly authored by the Pipeline LLM. Existing keyword
    selection policy is retained here; its disagreements are now inspectable.
    No raw brief is injected into the record beyond options required by existing
    audio/board consumers. Those options are frozen with the rest of the plan.
    """
    baseline = select_subsystems(schema, "")
    selected = select_subsystems(schema, user_prompt)

    def signatures(subsystems):
        return {(sub.kind, sub.name): {
            "kind": sub.kind, "name": sub.name,
            "variant": sub.variant, "target": sub.target,
        } for sub in subsystems}

    before, after = signatures(baseline), signatures(selected)
    differences = [
        {"kind": "subsystem_selection",
         "schema_derived": before.get(key), "brief_selected": after.get(key),
         "resolution": "brief_selected", "reason": "pre-generation brief selection"}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]
    # These generator gates otherwise re-read the brief after micro selection.
    # Capture them alongside the plan so a changed prompt cannot add a second
    # climbing runtime or vendor a different set of atmosphere scripts.
    from .unity_generator import wants_atmosphere
    selection_source = schema.model_copy(deep=True)
    selection_source.meta.pop(SELECTION_KEY, None)
    selection_source.meta["user_prompt"] = user_prompt
    record = ComposeSelection(
        subsystems=selected,
        atmosphere=wants_atmosphere(selection_source),
        climbing_contract=any(sub.kind == "controller" and sub.variant == "climbing"
                              for sub in selected),
        disagreements=deepcopy(schema.meta.get("intent_conflicts") or []) + differences,
    )
    schema.meta[SELECTION_KEY] = record.model_dump(mode="json")
    return [
        f"compose selection disagreement: {item['schema_derived']} -> "
        f"{item['brief_selected']} (recorded; pre-generation brief selection retained)"
        for item in differences
    ]


def selected_subsystems(schema: UnitySchema) -> list[Subsystem]:
    """Read saved choices, never user_prompt, concept or description.

    Legacy schemas have no captured selection to replay. Their deterministic
    fallback derives a plan from schema fields only and announces the loss of
    original selection provenance. An invalid record fails validation rather
    than silently selecting another game. Validation returns fresh objects, so
    composer/default-fix mutations cannot change the persisted plan.
    """
    if SELECTION_KEY in schema.meta:
        record = ComposeSelection.model_validate(schema.meta[SELECTION_KEY])
        return [sub.model_copy(deep=True) for sub in record.subsystems]
    console.print(
        "  [yellow]WARN[/] compose_selection missing: using schema-only selection; "
        "original controller/micro choices cannot be recovered. "
        "Regenerate from the brief to capture them."
    )
    return select_subsystems(schema, "")
