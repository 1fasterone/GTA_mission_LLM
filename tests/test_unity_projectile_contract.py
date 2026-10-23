"""Slice 0 baseline contracts for the deterministic ProjectileController bridge."""

from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import (
    PHYSICAL_PROJECTILE_META_KEY,
    apply_physical_projectile_contract,
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


def _physical_contract() -> dict:
    return {
        "delivery": "projectile_3d",
        "weapon": {
            "name": "Projectile Weapon",
            "delivery": "projectile_3d",
            "projectile_speed": 60.0,
            "projectile_use_gravity": False,
            "projectile_lifetime": 5.0,
            "projectile_pool_size": 8,
            "projectile_pool_growth": "fixed",
        },
    }


def test_explicit_3d_physical_brief_stamps_typed_loadout_and_weapon_entity():
    schema = _schema()

    messages = apply_physical_projectile_contract(
        schema,
        "A fast FPS shooter where the player fires pooled physical bullets.",
    )

    contract = schema.meta[PHYSICAL_PROJECTILE_META_KEY]
    assert contract["delivery"] == "projectile_3d"
    assert contract["weapon"]["delivery"] == "projectile_3d"
    assert contract["weapon"]["projectile_pool_size"] == 8
    assert [(e.name, e.kind, e.count) for e in schema.entities] == [
        ("PlayerWeapon", "weapon", 1)
    ]
    assert any("typed projectile_3d loadout" in message for message in messages)


def test_bare_fps_and_explicit_hitscan_keep_the_legacy_hitscan_path():
    for brief in (
        "A fast FPS arena shooter.",
        "An FPS with hitscan bullets and a rifle.",
    ):
        schema = _schema()
        assert apply_physical_projectile_contract(schema, brief) == []
        assert PHYSICAL_PROJECTILE_META_KEY not in schema.meta


def test_2d_physical_brief_is_reported_without_forging_a_3d_loadout():
    schema = _schema("2D")

    messages = apply_physical_projectile_contract(
        schema, "A 2D slingshot game with physical projectiles."
    )

    assert PHYSICAL_PROJECTILE_META_KEY not in schema.meta
    assert schema.entities == []
    assert messages == [
        "[projectile.p2] physical projectile request is outside Slice 2's 3D contract; "
        "preserved hitscan delivery (2D belongs to Slice 4)."
    ]


def test_compose_emits_pool_and_muzzle_path_only_for_typed_physical_contract(tmp_path):
    physical_subs = subsystems_for(
        "3D", [], projectile_contract=_physical_contract()
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


def test_slice0_locks_generated_projectile_runtime_invariants(tmp_path):
    """Freeze the merged runtime before later profile or animation slices."""
    subsystems = subsystems_for(
        "3D", [], projectile_contract=_physical_contract()
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

    # Launch remains outside the owner's bounds and uses the typed pool.
    assert "ResolveProjectileSpawnPosition(muzzle, launchDirection)" in controller
    assert "projectile.Configure(" in controller
    assert "projectile.Launch(launchDirection * projectileSpeed[_weaponIndex]);" in controller
    assert "CollisionDetectionMode.ContinuousSpeculative" in controller

    # Swept collision and callbacks converge on one guarded impact/release path.
    assert "Physics.SphereCastNonAlloc(" in projectile
    assert "QueryTriggerInteraction.Collide" in projectile
    assert "private void TryImpact(GameObject other)" in projectile
    assert "if (_released || _hasImpacted" in projectile
    assert "_hasImpacted = true;" in projectile
    assert "_returnToPool(this);" in projectile

    # Every checkout/return rebinds owner collisions and restores reusable state.
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
