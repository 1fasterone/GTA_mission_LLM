"""Focused acceptance contracts for the deterministic ProjectileController P2 bridge."""

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
    physical = {
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
    physical_subs = subsystems_for("3D", [], projectile_contract=physical)
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
