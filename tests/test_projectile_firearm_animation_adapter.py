"""Slice 4 deterministic firearm presentation and release contracts."""

from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import physical_projectile_contract_for


def _contract(brief: str) -> dict:
    contract = physical_projectile_contract_for("3D", brief)
    assert contract is not None
    return contract


def _controller_source(tmp_path: Path, brief: str, project: str) -> str:
    contract = _contract(brief)
    units = MicroComposer(project).emit(
        subsystems_for("3D", [], projectile_contract=contract),
        tmp_path / project / "Assets",
    )
    unit = next(candidate for candidate in units if candidate.kind == "controller")
    return (
        tmp_path
        / project
        / "Assets"
        / "Micros"
        / "controller"
        / "Scripts"
        / f"{unit.class_name}.cs"
    ).read_text(encoding="utf-8")


def test_fast_round_owns_closed_firearm_presentation_contract():
    contract = _contract("An automatic rifle firing visible physical bullets.")

    assert contract["version"] == "slice4-3d-v4"
    assert contract["firearm_presentation"] == {
        "adapter": "firearm_animation",
        "release_event": "OnFirearmRelease",
        "trigger_candidates": ["Fire", "Attack", "Shoot"],
        "fallback": "generated_recoil_flash",
        "release_timeout": 0.12,
        "recoil_distance": 0.06,
    }


def test_generated_firearm_adapter_preserves_single_ammo_and_cadence_authority(
    tmp_path,
):
    source = _controller_source(
        tmp_path,
        "An automatic rifle firing visible physical bullets.",
        "FirearmAdapter",
    )

    assert 'private const string FirearmReleaseEventName = "OnFirearmRelease";' in source
    assert '"Fire", "Attack", "Shoot"' in source
    assert "public void OnFirearmRelease()" in source
    assert "animationEvent.functionName != FirearmReleaseEventName" in source
    assert "parameters[i].type != AnimatorControllerParameterType.Trigger" in source
    assert "if (!TryRequestAnimatorFireRelease())" in source
    assert "ReleaseNextFirearmShot(true);" in source
    assert "ReleaseNextFirearmShot(false);" in source
    assert "ReleaseExpiredFirearmShots();" in source
    assert "firearmAnimator.gameObject != gameObject" in source

    queue_start = source.index("private bool QueueFirearmProjectile()")
    queue_end = source.index("public void OnFirearmRelease()", queue_start)
    queue_body = source[queue_start:queue_end]
    assert queue_body.count("_currentAmmo--;") == 1
    assert queue_body.count("_lastFireTime = Time.time;") == 1
    assert queue_body.index("_currentAmmo--;") < queue_body.index(
        "TryRequestAnimatorFireRelease()"
    )

    assert "Input.GetMouseButton(0)" in source
    assert "Input.GetMouseButtonDown(0)" not in source
    assert "ProcessFireInput(fireDown);" in source
    assert "private void ProcessFireInput(bool fireHeld)" in source
    assert (
        "Time.time >= _lastFireTime + weaponFireRate[_weaponIndex]" in source
    )


def test_generated_fallback_is_visible_and_keeps_impact_in_projectile_controller(
    tmp_path,
):
    source = _controller_source(
        tmp_path,
        "A rifle firing visible physical bullets at thin targets.",
        "FirearmFallback",
    )

    assert 'new GameObject("GeneratedProjectileMuzzle")' in source
    assert 'visual.name = "GeneratedFirearmPresentation";' in source
    assert 'new GameObject("GeneratedMuzzleFlash")' in source
    assert "flash.Emit(4);" in source
    assert "KickGeneratedFirearmRecoil();" in source
    assert "FindNamedDescendant" in source
    assert '"Muzzle", "ProjectileMuzzle", "MuzzleFlash", "BarrelEnd"' in source
    assert "projectile.Configure(" in source
    assert "projectile.Launch(" in source

    projectile_source = (
        tmp_path
        / "FirearmFallback"
        / "Assets"
        / "Micros"
        / "controller"
        / "Scripts"
        / "ProjectileController.cs"
    ).read_text(encoding="utf-8")
    assert 'SendMessageUpwards("TakeDamage", damage' in projectile_source
    assert "private void TryImpact(GameObject other)" in projectile_source


def test_thrown_object_does_not_receive_firearm_adapter(tmp_path):
    source = _controller_source(
        tmp_path,
        "A hero throwing rocks at moving targets.",
        "ThrownObject",
    )

    assert "OnFirearmRelease" not in source
    assert "GeneratedMuzzleFlash" not in source
    assert "QueueFirearmProjectile" not in source
    assert "if (!LaunchProjectile()) return;" in source


def test_firearm_adapter_generation_is_byte_deterministic(tmp_path):
    first = _controller_source(
        tmp_path,
        "An automatic rifle firing visible physical bullets.",
        "Repeatable",
    )
    second = _controller_source(
        tmp_path,
        "An automatic rifle firing visible physical bullets.",
        "Repeatable",
    )
    assert first == second
