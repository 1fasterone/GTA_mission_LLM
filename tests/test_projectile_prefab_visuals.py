"""Slice 3 contracts for projectile prefabs, visuals, and pooled reset."""

import json
from pathlib import Path

from src.unity_compose import (
    MicroComposer,
    refresh_projectile_visuals,
    subsystems_for,
)
from src.unity_entity_asset_resolver import resolve_entity_assets
from src.unity_projectile_contract import (
    PHYSICAL_PROJECTILE_META_KEY,
    apply_physical_projectile_contract,
    bind_projectile_visual_resolution,
    physical_projectile_contract_for,
)
from src.unity_schema import UnitySchema


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="ProjectileVisuals",
        company_name="CompilerTests",
        description="Projectile visual acceptance",
        game_type="3D",
        platform="PC",
        render_pipeline="builtin",
        scenes=["Main"],
    )


def _empty_lookups(tmp_path: Path) -> Path:
    lookups = tmp_path / "lookups"
    unity = lookups / "unity"
    unity.mkdir(parents=True)
    (unity / "asset_catalog.json").write_text(
        json.dumps({"version": 1, "vocabulary": {}, "assets": []}),
        encoding="utf-8",
    )
    return lookups


def _controller_source(tmp_path: Path, brief: str) -> tuple[str, str, str]:
    contract = physical_projectile_contract_for("3D", brief)
    assert contract is not None
    subsystems = subsystems_for("3D", [], projectile_contract=contract)
    units = MicroComposer("Slice3Visuals").emit(subsystems, tmp_path / "Assets")
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    scripts = tmp_path / "Assets" / "Micros" / "controller" / "Scripts"
    return (
        (scripts / f"{controller_unit.class_name}.cs").read_text(encoding="utf-8"),
        (scripts / "ProjectileController.cs").read_text(encoding="utf-8"),
        (scripts / "ProjectilePool.cs").read_text(encoding="utf-8"),
    )


def test_strict_asset_miss_selects_named_bullet_fallback(tmp_path):
    schema = _schema()
    apply_physical_projectile_contract(
        schema, "A rifle firing visible physical bullets at thin targets."
    )
    visual = next(e for e in schema.entities if e.kind == "projectile_visual")
    schema.entities = [visual]

    paths = resolve_entity_assets(
        schema,
        {},
        {},
        lookups_dir=str(_empty_lookups(tmp_path)),
        strict=True,
    )

    assert paths == {"ProjectileBulletVisual": "primitive:bullet"}


def test_resolved_visual_uses_catalog_then_is_suppressed_from_world(tmp_path):
    schema = _schema()
    apply_physical_projectile_contract(
        schema, "A character throws rocks at moving targets."
    )
    visual = next(e for e in schema.entities if e.kind == "projectile_visual")
    schema.entities = [visual]

    library = tmp_path / "library"
    library.mkdir()
    (library / "small-throwing-rock.fbx").write_bytes(b"FBX")
    lookups = tmp_path / "lookups"
    unity = lookups / "unity"
    unity.mkdir(parents=True)
    (unity / "asset_catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "vocabulary": {},
                "assets": [
                    {
                        "filename": "small-throwing-rock.fbx",
                        "relpath": "small-throwing-rock.fbx",
                        "root": library.as_posix(),
                        "category": "prop",
                        "keywords": ["small", "throwing", "rock", "stone"],
                        "kind_hints": ["prop"],
                        "generic_score": 0,
                        "scale": 1.0,
                        "game_contexts": ["any"],
                        "format": "fbx",
                        "pack": "test",
                        "curated": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    used = {}

    paths = resolve_entity_assets(
        schema, used, {}, lookups_dir=str(lookups), strict=True
    )
    resolved = paths["ProjectileRockVisual"]
    assert resolved.endswith("/small-throwing-rock")
    assert any(model.resource_path == resolved for model in used.values())

    bound = bind_projectile_visual_resolution(schema, paths)
    assert bound is not None
    assert bound["resolution"] == "asset"
    assert bound["resource_path"] == resolved
    assert paths["ProjectileRockVisual"] == "skip:projectile-visual"


def test_bullet_and_rock_emit_distinct_named_fallbacks(tmp_path):
    bullet, projectile, pool = _controller_source(
        tmp_path / "bullet", "A rifle firing visible physical bullets."
    )
    rock, _, _ = _controller_source(
        tmp_path / "rock", "A hero throwing rocks at targets."
    )

    assert 'new string[] { "bullet" }' in bullet
    assert 'new string[] { "rock" }' in rock
    assert "ProjectileVisualFallback_Bullet" in bullet
    assert "ProjectileVisualFallback_Rock" in bullet
    assert "PrimitiveType.Capsule" in bullet
    assert "PrimitiveType.Sphere" in bullet
    assert "Resources.Load<GameObject>(resourcePath)" in bullet
    assert "collision.radius = profile == \"rock\" ? 0.11f : 0.04f;" in bullet
    assert "DisablePresentationPhysics(visual);" in bullet
    assert "projectile.transform.localScale = Vector3.one;" in pool
    assert "CachePresentationState();" in projectile
    assert "RestorePresentationState();" in projectile
    assert "trail.emitting = false;" in projectile
    assert "effect.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);" in projectile
    assert "renderer.sharedMaterials = _presentationMaterials[i];" in projectile


def test_resolved_resource_refresh_reaches_generated_controller(tmp_path):
    contract = physical_projectile_contract_for(
        "3D", "A rifle firing visible physical bullets."
    )
    assert contract is not None
    subsystems = subsystems_for("3D", [], projectile_contract=contract)
    contract["projectile_visual"].update(
        {"resolution": "asset", "resource_path": "Models/Kenney/Prop/bullet"}
    )

    messages = refresh_projectile_visuals(subsystems, contract)
    assert messages
    units = MicroComposer("ResolvedProjectileVisual").emit(
        subsystems, tmp_path / "Assets"
    )
    controller_unit = next(unit for unit in units if unit.kind == "controller")
    source = (
        tmp_path
        / "Assets"
        / "Micros"
        / "controller"
        / "Scripts"
        / f"{controller_unit.class_name}.cs"
    ).read_text(encoding="utf-8")
    assert 'new string[] { "Models/Kenney/Prop/bullet" }' in source


def test_slice3_regeneration_is_byte_stable(tmp_path):
    brief = "A hero throwing rocks at moving targets."
    first = _controller_source(tmp_path / "first", brief)
    second = _controller_source(tmp_path / "second", brief)
    assert first == second


def test_binding_records_primitive_without_resource_path():
    schema = _schema()
    apply_physical_projectile_contract(
        schema, "A rifle firing visible physical bullets."
    )
    paths = {"ProjectileBulletVisual": "primitive:bullet"}

    bound = bind_projectile_visual_resolution(schema, paths)

    assert bound is schema.meta[PHYSICAL_PROJECTILE_META_KEY]["projectile_visual"]
    assert bound["resolution"] == "primitive"
    assert bound["resource_path"] == ""
    assert paths["ProjectileBulletVisual"] == "skip:projectile-visual"
