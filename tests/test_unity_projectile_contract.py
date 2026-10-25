"""Slice 0 baseline and Slice 1 projectile-profile acceptance contracts."""

from pathlib import Path

import pytest

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import (
    PHYSICAL_PROJECTILE_META_KEY,
    apply_physical_projectile_contract,
    physical_projectile_contract_for,
)
from src.unity_schema import UnitySchema


def _schema(game_type: str = "3D") -> UnitySchema:
    return UnitySchema(
        project_name="ProjectileContract",
        game_type=game_type,
        platform="PC",
        render_pipeline="builtin",
        scenes=["Main"],
    )


def _controller(subsystems):
    return next(sub for sub in subsystems if sub.kind == "controller")


def _contract_for(brief: str) -> dict:
    contract = physical_projectile_contract_for("3D", brief)
    assert contract is not None
    return contract


def test_explicit_3d_physical_brief_stamps_fast_round_and_weapon_entity():
    schema = _schema()

    messages = apply_physical_projectile_contract(
        schema,
        "A fast FPS shooter where the player fires pooled physical bullets.",
    )

    contract = schema.meta[PHYSICAL_PROJECTILE_META_KEY]
    assert contract["version"] == "slice4-3d-v4"
    assert contract["delivery"] == "projectile_3d"
    assert contract["motion_profile"] == "fast_round"
    assert contract["emission_profile"] == "muzzle"
    assert contract["visual_profile"] == "bullet"
    assert contract["weapon"]["delivery"] == "projectile_3d"
    assert contract["weapon"]["projectile_use_gravity"] is False
    assert contract["weapon"]["projectile_pool_size"] == 8
    assert [(e.name, e.kind, e.count) for e in schema.entities] == [
        ("PlayerWeapon", "weapon", 1),
        ("ProjectileBulletVisual", "projectile_visual", 1),
    ]
    assert contract["projectile_visual"] == {
        "entity_name": "ProjectileBulletVisual",
        "kind": "projectile_visual",
        "model_hint": "bullet cartridge ammunition",
        "fallback": "bullet",
        "target_size": 0.2,
    }
    assert contract["firearm_presentation"] == {
        "adapter": "firearm_animation",
        "release_event": "OnFirearmRelease",
        "trigger_candidates": ["Fire", "Attack", "Shoot"],
        "fallback": "generated_recoil_flash",
        "release_timeout": 0.12,
        "recoil_distance": 0.06,
    }
    assert any("typed projectile_3d loadout" in message for message in messages)


@pytest.mark.parametrize(
    "brief",
    [
        "A third-person game where the hero throws rocks at targets.",
        "A VR arena where the player hurls a stone at moving drones.",
    ],
)
def test_explicit_thrown_rock_uses_gravity_profile(brief):
    contract = _contract_for(brief)

    assert contract["motion_profile"] == "thrown_object"
    assert contract["emission_profile"] == "throw_release"
    assert contract["visual_profile"] == "rock"
    assert "firearm_presentation" not in contract
    assert contract["weapon"] == {
        "name": "Thrown Rock",
        "damage": 20.0,
        "fire_rate": 0.75,
        "magazine_size": 6,
        "reload_time": 1.5,
        "shoot_range": 35.0,
        "delivery": "projectile_3d",
        "projectile_speed": 18.0,
        "projectile_use_gravity": True,
        "projectile_lifetime": 8.0,
        "projectile_pool_size": 8,
        "projectile_pool_growth": "fixed",
    }


def test_thrown_rock_adds_a_rock_view_model_without_replacing_runtime_delivery():
    schema = _schema()

    apply_physical_projectile_contract(
        schema,
        "A third-person game where the hero tosses rocks at targets.",
    )

    contract = schema.meta[PHYSICAL_PROJECTILE_META_KEY]
    assert contract["delivery"] == "projectile_3d"
    assert [(e.name, e.kind, e.model_hint, e.scale) for e in schema.entities] == [
        ("ThrownRock", "weapon", "rough throwing rock stone", 0.22),
        (
            "ProjectileRockVisual",
            "projectile_visual",
            "small rough throwing rock stone",
            0.24,
        ),
    ]


def test_bare_rock_is_not_mistaken_for_a_thrown_projectile():
    assert physical_projectile_contract_for(
        "3D", "A rocky canyon containing stone ruins."
    ) is None


def test_bare_fps_and_explicit_hitscan_keep_the_legacy_hitscan_path():
    for brief in (
        "A fast FPS arena shooter.",
        "An FPS with hitscan bullets and a rifle.",
        "A hitscan game where the hero throws rocks as a cosmetic animation.",
    ):
        schema = _schema()
        assert apply_physical_projectile_contract(schema, brief) == []
        assert PHYSICAL_PROJECTILE_META_KEY not in schema.meta


@pytest.mark.parametrize(
    "brief",
    [
        "A 2D slingshot game with physical projectiles.",
        "A 2D game where the player throws rocks.",
    ],
)
def test_2d_physical_brief_is_reported_without_forging_a_3d_loadout(brief):
    schema = _schema("2D")

    messages = apply_physical_projectile_contract(schema, brief)

    assert PHYSICAL_PROJECTILE_META_KEY not in schema.meta
    assert schema.entities == []
    assert messages == [
        "[projectile.p2] physical projectile request is outside Slice 2's 3D contract; "
        "preserved hitscan delivery (2D belongs to Slice 4)."
    ]


def test_compose_emits_pool_and_muzzle_path_only_for_typed_physical_contract(tmp_path):
    physical_subs = subsystems_for(
        "3D", [], projectile_contract=_contract_for("A rifle firing physical bullets.")
    )
    physical_controller = _controller(physical_subs)
    assert physical_controller.options["weapons"][0]["delivery"] == "projectile_3d"
    assert "shoot" in physical_controller.options["abilities"]

    units = MicroComposer("PhysicalProjectile").emit(
        physical_subs, Path(tmp_path) / "Assets"
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    source = (Path(tmp_path) / "Assets" / "Micros" / "controller" / "Scripts"
              / f"{controller_unit.class_name}.cs").read_text(encoding="utf-8")
    assert "private ProjectilePool[] _projectilePools;" in source
    assert "ResolveProjectileMuzzle()" in source
    assert "new ProjectilePool" in source

    hitscan_source_root = Path(tmp_path) / "hitscan" / "Assets"
    hitscan_units = MicroComposer("HitscanProjectile").emit(
        subsystems_for("3D", [], genre="fps"), hitscan_source_root
    )
    hitscan_controller = next(unit for unit in hitscan_units if unit.kind == "controller")
    hitscan_source = (hitscan_source_root / "Micros" / "controller" / "Scripts"
                      / f"{hitscan_controller.class_name}.cs").read_text(encoding="utf-8")
    assert "private ProjectilePool[] _projectilePools;" not in hitscan_source
    assert "Physics.Raycast" in hitscan_source


def test_thrown_profile_composes_through_existing_projectile_foundation(tmp_path):
    physical = _contract_for("A hero throwing rocks at practice targets.")
    subsystems = subsystems_for("3D", [], projectile_contract=physical)
    controller = _controller(subsystems)
    weapon = controller.options["weapons"][0]
    assert weapon["projectile_speed"] == 18.0
    assert weapon["projectile_use_gravity"] is True

    units = MicroComposer("ThrownRockProjectile").emit(
        subsystems, Path(tmp_path) / "Assets"
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    scripts = Path(tmp_path) / "Assets" / "Micros" / "controller" / "Scripts"
    controller_source = (scripts / f"{controller_unit.class_name}.cs").read_text(
        encoding="utf-8"
    )
    projectile_source = (scripts / "ProjectileController.cs").read_text(
        encoding="utf-8"
    )
    pool_source = (scripts / "ProjectilePool.cs").read_text(encoding="utf-8")

    assert "new float[] { 18" in controller_source
    assert "new bool[] { true" in controller_source
    assert "Physics.SphereCastNonAlloc(" in projectile_source
    assert "CollisionDetectionMode.ContinuousSpeculative" in pool_source


def test_slice0_locks_generated_projectile_runtime_invariants(tmp_path):
    """Freeze the runtime foundation beneath every Slice 1 profile."""
    subsystems = subsystems_for(
        "3D", [], projectile_contract=_contract_for("A rifle firing physical bullets.")
    )
    units = MicroComposer("ProjectileSlice0Baseline").emit(
        subsystems, Path(tmp_path) / "Assets"
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    scripts = Path(tmp_path) / "Assets" / "Micros" / "controller" / "Scripts"
    controller = (scripts / f"{controller_unit.class_name}.cs").read_text(
        encoding="utf-8"
    )
    projectile = (scripts / "ProjectileController.cs").read_text(encoding="utf-8")
    pool = (scripts / "ProjectilePool.cs").read_text(encoding="utf-8")

    assert "ResolveProjectileSpawnPosition(muzzle, launchDirection)" in controller
    assert "projectile.Configure(" in controller
    assert "projectile.Launch(launchDirection * projectileSpeed[weaponIndex]);" in controller
    assert "CollisionDetectionMode.ContinuousSpeculative" in controller

    assert "Physics.SphereCastNonAlloc(" in projectile
    assert "QueryTriggerInteraction.Collide" in projectile
    assert "private void TryImpact(GameObject other)" in projectile
    assert "if (_released || _hasImpacted" in projectile
    assert "_hasImpacted = true;" in projectile
    assert "_returnToPool(this);" in projectile

    assert "BindOwnerCollisions();" in projectile
    assert "RestoreOwnerCollisions();" in projectile
    assert "_body.linearVelocity = Vector3.zero;" in projectile
    assert "_body.angularVelocity = Vector3.zero;" in projectile
    assert "_body.useGravity = false;" in projectile
    assert "ParticleSystemStopBehavior.StopEmittingAndClear" in projectile
    assert "CollisionDetectionMode.ContinuousSpeculative" in projectile
    assert "if (projectile == null || !_borrowed.Remove(projectile)) return;" in pool
    assert "projectile.ResetForPool();" in pool
    assert "CollisionDetectionMode.ContinuousSpeculative" in pool
