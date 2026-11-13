"""Slice 4 firearm presentation adapter, ported onto the R3 firing provider.

The adapter decides how an ACCEPTED shot is presented (Animator release event,
generated muzzle flash and recoil) and when its reserved round launches.
Whether a shot is accepted stays with IFiringProvider.TryFire(), the gate the
fire input and the Shoot atom share. Every brief and schema below is
engineer-authored test data, never Pipeline LLM output.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.intent.projectile import physical_projectile_contract_for
from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import (
    PHYSICAL_PROJECTILE_META_KEY,
    validate_physical_projectile_contract,
)
from src.unity_schema import EntitySpec, UnitySchema

RIFLE = "An automatic rifle firing visible physical bullets."
ROCKS = "A hero throwing rocks at moving targets."


def _contract(brief: str) -> dict:
    contract = physical_projectile_contract_for("3D", brief)
    assert contract is not None
    return contract


def _emit(tmp_path: Path, brief: str, project: str) -> tuple[str, Path]:
    units = MicroComposer(project).compose(
        subsystems_for("3D", [], projectile_contract=_contract(brief)),
        tmp_path / project / "Assets",
    )
    unit = next(candidate for candidate in units if candidate.kind == "controller")
    scripts = tmp_path / project / "Assets" / "Micros" / "controller" / "Scripts"
    return (scripts / f"{unit.class_name}.cs").read_text(encoding="utf-8"), scripts


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start:source.index("\n    }\n", start)]


# ── the closed contract ───────────────────────────────────────────────────────

def test_fast_round_owns_closed_firearm_presentation_contract():
    contract = _contract(RIFLE)
    assert contract["version"] == "slice4-3d-v4"
    assert contract["firearm_presentation"] == {
        "adapter": "firearm_animation",
        "release_event": "OnFirearmRelease",
        "trigger_candidates": ["Fire", "Attack", "Shoot"],
        "fallback": "generated_recoil_flash",
        "release_timeout": 0.12,
        "recoil_distance": 0.06,
    }
    assert "firearm_presentation" not in _contract(ROCKS)


def test_subsystems_hand_the_adapter_to_the_controller_only_for_fast_rounds():
    def controller_meta(brief: str) -> dict:
        subsystems = subsystems_for("3D", [], projectile_contract=_contract(brief))
        controller = next(sub for sub in subsystems if sub.kind == "controller")
        return controller.options.get("meta", {})

    assert controller_meta(RIFLE)["firearm_presentation"]["release_event"] == "OnFirearmRelease"
    assert "firearm_presentation" not in controller_meta(ROCKS)


def _validated(contract: dict) -> list[str]:
    schema = UnitySchema(project_name="FirearmProof", game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["Main"],
                         entities=[EntitySpec(name="PlayerWeapon", kind="weapon")])
    schema.meta = {PHYSICAL_PROJECTILE_META_KEY: contract}
    return validate_physical_projectile_contract(schema)


def test_validator_accepts_the_closed_contracts():
    assert _validated(_contract(RIFLE)) == []
    assert _validated(_contract(ROCKS)) == []


@pytest.mark.parametrize(("mutate", "finding"), [
    (lambda c: c.pop("firearm_presentation"), "firearm_presentation is missing"),
    (lambda c: c["firearm_presentation"].update(adapter="other"), "adapter must be firearm_animation"),
    (lambda c: c["firearm_presentation"].update(release_event="On Release()"), "release_event must be a C# method name"),
    (lambda c: c["firearm_presentation"].update(trigger_candidates=[]), "trigger_candidates must be Animator parameter names"),
    (lambda c: c["firearm_presentation"].update(release_timeout=0), "release_timeout must be positive"),
    (lambda c: c["firearm_presentation"].update(recoil_distance=-1), "recoil_distance must be positive"),
])
def test_validator_reports_a_drifted_firearm_contract(mutate, finding):
    contract = copy.deepcopy(_contract(RIFLE))
    mutate(contract)
    assert any(finding in error for error in _validated(contract))


def test_validator_rejects_a_firearm_adapter_on_a_thrown_object():
    contract = copy.deepcopy(_contract(ROCKS))
    contract["firearm_presentation"] = copy.deepcopy(_contract(RIFLE)["firearm_presentation"])
    assert any("cannot carry a firearm adapter" in error for error in _validated(contract))


# ── the generated adapter keeps TryFire the only gate ─────────────────────────

def test_adapter_reserves_inside_the_one_gate_and_spends_once(tmp_path):
    source, _ = _emit(tmp_path, RIFLE, "FirearmGate")

    # The gate is still R3's: input and atom reach TryFire, TryFire reaches TryShoot.
    assert "if (fireDown) TryFire();" in source
    calls = [line for line in source.splitlines()
             if "TryShoot()" in line and not line.lstrip().startswith("//")
             and "bool TryShoot()" not in line]
    assert len(calls) == 1 and "return TryShoot() ?" in calls[0]

    # The projectile branch of the shot reserves through the adapter.
    shot = _body(source, "private bool TryShoot()")
    assert "return QueueFirearmProjectile();" in shot
    assert source.count("QueueFirearmProjectile()") == 2   # declaration + TryShoot

    # Reservation spends ammo and cadence exactly once, after a round was
    # borrowed and before either presentation path can release it.
    queue = _body(source, "private bool QueueFirearmProjectile()")
    assert queue.index("if (projectile == null) return false;") < queue.index("_currentAmmo--;")
    assert queue.count("_currentAmmo--;") == 1
    assert queue.count("_lastFireTime = Time.time;") == 1
    assert queue.index("_currentAmmo--;") < queue.index("TryRequestAnimatorFireRelease()")
    # Nothing else in the class spends: the hit-scan branch is the only other.
    assert source.count("_currentAmmo--;") == 2

    assert 'private const string FirearmReleaseEventName = "OnFirearmRelease";' in source
    assert '"Fire", "Attack", "Shoot"' in source
    assert "public void OnFirearmRelease()" in source
    assert "animationEvent.functionName != FirearmReleaseEventName" in source
    assert "parameters[i].type != AnimatorControllerParameterType.Trigger" in source
    assert "firearmAnimator.gameObject != gameObject" in source
    assert "ReleaseNextFirearmShot(true);" in source and "ReleaseNextFirearmShot(false);" in source
    assert "ReleaseExpiredFirearmShots();" in source
    assert "Time.time + 0.12f" in source
    # Repo rule: no GetInstanceID; the checked controller is compared by reference.
    assert "GetInstanceID" not in source


def test_generated_fallback_is_visible_and_impact_stays_in_projectile_controller(tmp_path):
    source, scripts = _emit(tmp_path, RIFLE, "FirearmFallback")
    assert 'new GameObject("GeneratedProjectileMuzzle")' in source
    assert 'visual.name = "GeneratedFirearmPresentation";' in source
    assert 'new GameObject("GeneratedMuzzleFlash")' in source
    assert "flash.Emit(4);" in source
    assert "KickGeneratedFirearmRecoil();" in source
    assert '"Muzzle", "ProjectileMuzzle", "MuzzleFlash", "BarrelEnd"' in source
    assert "0.06f);" in _body(source, "private void KickGeneratedFirearmRecoil()")

    projectile = (scripts / "ProjectileController.cs").read_text(encoding="utf-8")
    assert 'SendMessageUpwards("TakeDamage", damage' in projectile
    assert "private void TryImpact(GameObject other)" in projectile


def test_thrown_object_does_not_receive_the_firearm_adapter(tmp_path):
    source, _ = _emit(tmp_path, ROCKS, "ThrownObject")
    for adapter_only in ("OnFirearmRelease", "GeneratedMuzzleFlash", "QueueFirearmProjectile",
                         "_pendingFirearmShots"):
        assert adapter_only not in source
    assert "if (!LaunchProjectile()) return false;" in source


def test_firearm_adapter_generation_is_byte_deterministic(tmp_path):
    first, _ = _emit(tmp_path / "a", RIFLE, "Repeatable")
    second, _ = _emit(tmp_path / "b", RIFLE, "Repeatable")
    assert first == second


def test_projectile_never_sets_velocity_on_a_kinematic_body(tmp_path):
    _, scripts = _emit(tmp_path, RIFLE, "KinematicOrder")
    projectile = (scripts / "ProjectileController.cs").read_text(encoding="utf-8")
    configure = _body(projectile, "public void Configure(float damageAmount, GameObject owner, float lifetime")
    assert configure.index("_body.isKinematic = false;") < configure.index("_body.linearVelocity = Vector3.zero;")
    for parked in ("public void PrepareForCheckout()", "public void ResetForPool()"):
        body = _body(projectile, parked)
        assert "!_body.isKinematic" in body
        assert body.index("!_body.isKinematic") < body.index("_body.linearVelocity = Vector3.zero;")


# ── Roslyn against the real Unity reference assemblies ────────────────────────

def test_adapter_controller_compiles_against_real_unity(tmp_path):
    from tests.test_firing_provider import _compile

    _, scripts = _emit(tmp_path, RIFLE, "FirearmCompile")
    micros = scripts.parent.parent
    sources = {f"Assets/Micros/{path.relative_to(micros).as_posix()}": path.read_text(encoding="utf-8")
               for kind in ("controller", "input")
               for path in sorted((micros / kind / "Scripts").glob("*.cs"))}
    sources["Assets/Scripts/ZzProbe.cs"] = "using UnityEngine;\npublic class ZzProbe : MonoBehaviour { }\n"
    check = _compile(sources)
    # Input System types in the input micro stay unverified without
    # package_assemblies (R0); the adapter controller must bind cleanly.
    findings = [error for error in check.errors
                if not (error["path"].startswith("Assets/Micros/input/")
                        and error["code"] in {"CS0246", "CS0234"})]
    assert findings == [], findings
