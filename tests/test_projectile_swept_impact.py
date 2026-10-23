"""Slice 2 contracts for authoritative, deterministic swept impact output."""

from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import physical_projectile_contract_for


def _generated_projectile_source(tmp_path: Path) -> str:
    contract = physical_projectile_contract_for(
        "3D", "A rifle firing pooled physical bullets at thin targets."
    )
    assert contract is not None
    units = MicroComposer("SweptImpactContract").emit(
        subsystems_for("3D", [], projectile_contract=contract),
        tmp_path / "Assets",
    )
    assert any(unit.kind == "controller" for unit in units)
    projectile_path = (
        tmp_path
        / "Assets"
        / "Micros"
        / "controller"
        / "Scripts"
        / "ProjectileController.cs"
    )
    return projectile_path.read_text(encoding="utf-8")


def test_slice2_selects_nearest_valid_sweep_hit_with_stable_tie_break(tmp_path):
    source = _generated_projectile_source(tmp_path)

    assert "Physics.SphereCastNonAlloc(" in source
    assert "QueryTriggerInteraction.Collide" in source
    assert "IsSelf(candidate.gameObject)" in source
    assert "IsOwner(candidate.gameObject)" in source
    assert "IsPreferredSweepHit(candidateHit, nearestHit)" in source
    assert "CompareVector3(candidate.point, selected.point)" in source
    assert "StableColliderKey(candidate.collider)" in source
    assert "ColliderComponentIndex(collider)" in source
    assert "GetInstanceID()" not in source


def test_slice2_routes_sweep_and_callbacks_through_once_only_impact(tmp_path):
    source = _generated_projectile_source(tmp_path)

    assert "TryImpact(nearestHit.collider.gameObject);" in source
    assert "OnCollisionEnter(Collision collision) => TryImpact(" in source
    assert "OnTriggerEnter(Collider other) => TryImpact(" in source
    assert "if (_released || _hasImpacted" in source
    assert source.index("_hasImpacted = true;") < source.index(
        'other.SendMessageUpwards("TakeDamage"'
    )
    assert source.count('other.SendMessageUpwards("TakeDamage"') == 1
    assert "if (_released) return;" in source


def test_slice2_places_body_at_sphere_cast_contact_without_double_radius_offset(tmp_path):
    source = _generated_projectile_source(tmp_path)

    assert "Mathf.Clamp(nearestHit.distance, 0f, distance)" in source
    assert "nearestDistance - _sweepRadius" not in source
    assert "CollisionDetectionMode.ContinuousSpeculative" in source
