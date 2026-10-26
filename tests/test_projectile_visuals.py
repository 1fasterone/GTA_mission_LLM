"""Slice 3 contracts for resolved visuals and leak-free pool reuse."""

from pathlib import Path

import pytest

from src.unity_compose import (
    MicroComposer,
    refresh_projectile_presentation,
    subsystems_for,
)
from src.unity_projectile_contract import (
    PHYSICAL_PROJECTILE_META_KEY,
    physical_projectile_contract_for,
    projectile_visual_resolver_entity,
    stamp_projectile_visual_resolution,
)
from src.unity_schema import UnitySchema


def _contract(brief: str) -> dict:
    contract = physical_projectile_contract_for("3D", brief)
    assert contract is not None
    return contract


def _generated_sources(tmp_path: Path, brief: str) -> tuple[str, str, str]:
    contract = _contract(brief)
    assets = tmp_path / "Assets"
    units = MicroComposer("ProjectileSlice3Visuals").emit(
        subsystems_for("3D", [], projectile_contract=contract),
        assets,
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    scripts = assets / "Micros" / "controller" / "Scripts"
    return (
        (scripts / f"{controller_unit.class_name}.cs").read_text(encoding="utf-8"),
        (scripts / "ProjectileController.cs").read_text(encoding="utf-8"),
        (scripts / "ProjectilePool.cs").read_text(encoding="utf-8"),
    )


def test_visual_request_is_compiler_only_and_resolution_stays_in_meta():
    contract = _contract("A rifle fires visible physical bullets at targets.")
    schema = UnitySchema(
        project_name="ProjectileVisualResolution",
        game_type="3D",
        platform="PC",
        render_pipeline="builtin",
        scenes=["Main"],
        meta={PHYSICAL_PROJECTILE_META_KEY: contract},
    )

    request = projectile_visual_resolver_entity(schema)
    assert request is not None
    assert request.name == "ProjectileVisualFastRound"
    assert request.kind == "prop"
    assert request.model_hint == "bullet projectile round"
    assert schema.entities == []

    stamp_projectile_visual_resolution(schema, "Models/Kenney/Projectile/Bullet")
    presentation = schema.meta[PHYSICAL_PROJECTILE_META_KEY]["presentation"]
    assert presentation["resource_path"] == "Models/Kenney/Projectile/Bullet"
    assert schema.entities == []


def test_unknown_motion_profile_abstains_loudly():
    schema = UnitySchema(
        project_name="UnknownProjectileVisual",
        game_type="3D",
        platform="PC",
        render_pipeline="builtin",
        scenes=["Main"],
        meta={PHYSICAL_PROJECTILE_META_KEY: {"motion_profile": "invented_round"}},
    )

    with pytest.raises(ValueError, match="unknown closed motion profile"):
        projectile_visual_resolver_entity(schema)


def test_post_resolution_refresh_reaches_controller_without_schema_fields():
    contract = _contract("A rifle fires visible physical bullets.")
    subsystems = subsystems_for("3D", [], projectile_contract=contract)
    controller = next(sub for sub in subsystems if sub.kind == "controller")
    contract["presentation"]["resource_path"] = "Models/Kenney/Projectile/Bullet"

    messages = refresh_projectile_presentation(subsystems, contract)

    assert controller.options["meta"]["projectile_presentation"]["resource_path"] == (
        "Models/Kenney/Projectile/Bullet"
    )
    assert messages == [
        "compose: controller projectile presentation refreshed from resolved "
        "compiler metadata"
    ]


def test_fast_round_emits_resolved_model_path_and_named_capsule_fallback(tmp_path):
    contract = _contract("A rifle fires visible physical bullets.")
    contract["presentation"]["resource_path"] = "Models/Kenney/Projectile/Bullet"
    assets = tmp_path / "Assets"
    units = MicroComposer("ResolvedProjectileVisual").emit(
        subsystems_for("3D", [], projectile_contract=contract), assets
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    source = (
        assets
        / "Micros"
        / "controller"
        / "Scripts"
        / f"{controller_unit.class_name}.cs"
    ).read_text(encoding="utf-8")

    assert 'ProjectileVisualResourcePath = "Models/Kenney/Projectile/Bullet"' in source
    assert 'ProjectileFallbackName = "ProjectileFallback_FastRound_Capsule"' in source
    assert 'ProjectileFallbackPrimitive = "Capsule"' in source
    assert "Resources.Load<GameObject>(ProjectileVisualResourcePath)" in source
    assert "var prefab = new GameObject(\"Projectile3DPrefab_\" + weaponIndex);" in source
    assert "var physicsShape = prefab.AddComponent<SphereCollider>();" in source
    assert "float forwardOffset = ProjectilePhysicsRadius + skin;" in source
    assert "projectedExtent + ProjectilePhysicsRadius + skin" in source
    assert "StripVisualPhysics(visual);" in source


def test_bullet_and_rock_fallbacks_are_visibly_distinct(tmp_path):
    bullet_controller, _, _ = _generated_sources(
        tmp_path / "bullet", "A rifle fires visible physical bullets."
    )
    rock_controller, _, _ = _generated_sources(
        tmp_path / "rock", "The hero visibly throws rocks at targets."
    )

    assert 'ProjectileFallbackPrimitive = "Capsule"' in bullet_controller
    assert "ProjectileVisualUniformScale = 0.0800f" in bullet_controller
    assert "new Color(\n        1.000f,\n        0.580f" in bullet_controller
    assert 'ProjectileFallbackPrimitive = "Cube"' in rock_controller
    assert "ProjectileVisualUniformScale = 0.2200f" in rock_controller
    assert "new Color(\n        0.340f,\n        0.270f" in rock_controller


def test_checkout_and_return_emit_complete_visual_reset_contract(tmp_path):
    _, projectile, pool = _generated_sources(
        tmp_path, "A rifle fires visible physical bullets."
    )

    assert "public void PrepareForCheckout()" in projectile
    assert "ResetVisualState(true);" in projectile
    assert "ResetVisualState(false);" in projectile
    assert "RemoveTransientChildren();" in projectile
    assert "SetLocalPositionAndRotation(" in projectile
    assert "runtime.CopyPropertiesFromMaterial(baseline);" in projectile
    assert "SetPropertyBlock(null" in projectile
    assert "ParticleSystemStopBehavior.StopEmittingAndClear" in projectile
    assert "state.Target.Clear();" in projectile
    assert "state.Target.emitting = checkout && state.Emitting;" in projectile

    assert "projectile.PrepareForCheckout();" in pool
    assert "projectile.ResetForPool();" in pool
    assert "SetLocalPositionAndRotation(Vector3.zero, Quaternion.identity);" in pool
    assert "projectile.transform.localScale = Vector3.one;" in pool

    assert "Physics.SphereCastNonAlloc(" in projectile
    assert "BindOwnerCollisions();" in projectile
    assert "RestoreOwnerCollisions();" in projectile
    assert '_returnToPool(this);' in projectile
