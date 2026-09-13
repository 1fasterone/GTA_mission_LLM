"""Engineer-authored regression fixtures; not Pipeline LLM output."""
import pytest
from src.unity_generator import UnityProjectGenerator, _needs_player_rigidbody
from src.unity_schema import UnitySchema, UnityScript, CodeField, CodeMethod


def script(**kwargs):
    return UnityScript(name="WindSystem", type="custom", description="wind", **kwargs)


def test_wind_binding_preserves_force_and_waits_for_spawn():
    body = "Vector3 windForce = new Vector3(10f, 0, 0); playerRigidbody.AddForce(windForce * Time.deltaTime, ForceMode.Impulse);"
    s = script(update_extra=body, attach_to="systems")
    schema = UnitySchema(project_name="Probe", company_name="A", description="d", game_type="3D", platform="pc", render_pipeline="URP", unity_version="6000.0")
    content, _ = UnityProjectGenerator()._generate_script(schema, s, {})
    assert body in content
    assert 'GameObject.FindWithTag("Player")' in content
    assert 'player.GetComponent<Rigidbody>()' in content
    assert 'if (playerRigidbody == null) return;' in content
    assert 'private Rigidbody playerRigidbody' in content
    assert 'AddComponent<Rigidbody>' not in content
    assert 'Debug.LogWarning' in content
    assert s.extra_fields == []


@pytest.mark.parametrize("body", [
    '// playerRigidbody.AddForce(force);',
    'Debug.Log("playerRigidbody");',
    'var playerRigidbody = GetComponent<Rigidbody>(); playerRigidbody.WakeUp();',
    'other.playerRigidbody.WakeUp();',
])
def test_no_binding_for_non_reference_or_local(body):
    assert not _needs_player_rigidbody(script(update_extra=body))


def test_existing_field_and_method_parameter_are_preserved():
    assert not _needs_player_rigidbody(script(update_extra="playerRigidbody.WakeUp();", extra_fields=[CodeField(name="playerRigidbody", type="Rigidbody")]))
    assert not _needs_player_rigidbody(script(extra_methods=[CodeMethod(signature="void Push(Rigidbody playerRigidbody)", body="playerRigidbody.WakeUp();")]))


def test_method_and_fixed_update_references_are_detected():
    assert _needs_player_rigidbody(script(fixed_update_extra="playerRigidbody.WakeUp();"))
    assert _needs_player_rigidbody(script(extra_methods=[CodeMethod(signature="void Push()", body="playerRigidbody.WakeUp();")]))
