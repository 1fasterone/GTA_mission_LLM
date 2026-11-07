"""Engineer-authored file/HTTP inventory fixtures, never a live Pipeline LLM run."""
import hashlib
import pytest
from tools.untouched_brief_preflight import editor_inventory, input_inventory, model_inventory


def test_editor_root_is_explicit_and_requires_actual_executable(tmp_path):
    exe = tmp_path / '6000.0.66f2/Editor/Unity.exe'
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'engineer-authored fixture, not an executable')
    (tmp_path / '6000.1.4f1').mkdir()
    rows = editor_inventory(tmp_path)
    assert rows[0]['directory'] == '6000.0.66f2' and rows[0]['executable_present']
    assert rows[1]['directory'] == '6000.1.4f1' and not rows[1]['executable_present']


def test_input_hashes_use_original_bytes_without_newline_or_encoding_changes(tmp_path):
    raw = 'Unmodified café\r\n'.encode('utf-8')
    (tmp_path / 'prompts').mkdir()
    (tmp_path / 'prompts/turbo_toys.txt').write_bytes(raw)
    rows = input_inventory(tmp_path)
    assert rows[0]['sha256'] == hashlib.sha256(raw).hexdigest()
    assert rows[0]['cohort_status'] == 'candidate_only'
    assert all(r['status'] == 'missing' for r in rows[1:])


def test_offline_model_inventory_never_contacts_endpoint(monkeypatch):
    monkeypatch.setattr('tools.untouched_brief_preflight.urlopen', lambda *a, **k: pytest.fail('network called'))
    assert model_inventory('http://localhost:1234/v1', 'fixture', offline=True)['status'] == 'unverified_offline'


@pytest.mark.parametrize('url', ['http://secret:token@localhost/v1', 'http://localhost/v1?api_key=secret'])
def test_credentials_are_not_allowed_in_recorded_endpoint(url):
    with pytest.raises(ValueError, match='without credentials'):
        model_inventory(url, 'fixture', offline=True)
