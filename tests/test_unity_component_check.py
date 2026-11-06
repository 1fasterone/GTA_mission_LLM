"""Engineer-authored shadow binding fixtures, never live model/runtime evidence."""
from pathlib import Path
import json
import os
import shutil

import pytest

from src.unity_component_api import ComponentApiError, ComponentApiUnavailable, extract_components, extract_sources
from src.unity_component_check import approve_candidate, check_candidate
from src.unity_compose import MicroComposer, Subsystem


@pytest.fixture(autouse=True)
def roslyn():
    if shutil.which('dotnet') is None:
        if os.environ.get('REQUIRE_COMPONENT_API') == '1':
            pytest.fail('required pinned SDK unavailable')
        pytest.skip('consumer binding unverified: pinned SDK unavailable')


PROVIDER = '''using System;
public class Parent { public int BaseValue { get; set; } }
public class Provider : Parent {
 public event Action<int> Changed;
 public int Value {get; private set;}
 public string Label;
 private void Secret() {}
 public static void Static(int value) {}
 public void Run(int value) {}
 public void Run(string value) {}
 public void Optional(int value=1) {}
 public void Many(params int[] values) {}
 public T Echo<T>(T value) {return value;}
}'''


def baseline():
    return extract_sources({'Provider.cs': PROVIDER})


def consumer(body, *, parameters='Provider target', fields=''):
    return f'''using System;
public class Consumer {{
 {fields}
 public void Apply({parameters}) {{ {body} }}
}}'''


def codes(check):
    return {d['code'] for d in check.errors}


def test_contract_cannot_be_mutated_through_constructor_or_public_views():
    contract = baseline()
    before = contract.to_json()
    contract.document['types'].clear()
    contract.sources['Provider.cs'] = 'wrong'
    contract.type('Provider')['members'].clear()
    assert contract.to_json() == before
    assert contract.sources['Provider.cs'] == PROVIDER
    assert contract.type('Provider')['members']


@pytest.mark.parametrize(('body', 'code'), [
    ('target.Secret();', 'CS0122'),
    ('target.Missing();', 'CS1061'),
    ('target.Run();', 'CS1501'),
    ('target.Run(1,2);', 'CS1501'),
    ('target.Run(true);', 'CS1503'),
    ('target.Static(1);', 'CS0176'),
    ('Provider.Run(1);', 'CS0120'),
    ('target.Changed(1);', 'CS0070'),
    ('var handler = target.Changed;', 'CS0070'),
    ('target.Value = 3;', 'CS0272'),
    ('target.Label = 3;', 'CS0029'),
    ('target.Echo<int,string>(1);', 'CS0305'),
    ('var missing = new UnprovidedHelper();', 'CS0246'),
])
def test_invalid_project_calls_retain_source_and_exact_diagnostic(body, code, tmp_path):
    contract = baseline()
    source = consumer(body)
    check = check_candidate(contract, {'Consumer.cs': source}, output_dir=tmp_path)
    assert not check.accepted and code in codes(check)
    assert check.sources['Consumer.cs'] == source
    assert (tmp_path / 'sources/Consumer.cs').read_bytes() == source.encode()
    assert (tmp_path / 'baseline/sources/Provider.cs').read_bytes() == PROVIDER.encode()
    assert contract.sources == {'Provider.cs': PROVIDER}
    errors = json.loads((tmp_path / 'component-api.json').read_text())['diagnostics']
    assert any(d['path'] == 'Consumer.cs' and d['code'] == code and d['line'] > 0 for d in errors)


@pytest.mark.parametrize(('body', 'fields'), [
    ('target.Run(1);', ''),
    ('var local = target; local.Run(1);', ''),
    ('field.Run(1);', 'private Provider field;'),
    ('Getter.Run(1);', 'private Provider Getter {get;set;}'),
    ('target?.Run(1);', ''),
    ('target.Run("one"); target.Optional(); target.Many(1,2,3);', ''),
    ('var value = target.Echo<int>(1); target.BaseValue = value;', ''),
    ('target.Changed += OnChanged; target.Changed -= OnChanged;', 'private void OnChanged(int value) {}'),
])
def test_valid_receivers_overloads_events_and_inheritance(body, fields):
    check = check_candidate(baseline(), {'Consumer.cs': consumer(body, fields=fields)})
    assert check.accepted, check.report
    assert check.report['unity_editor_execution'] == 'unverified'
    assert check.errors == []


@pytest.mark.parametrize(('body', 'fields', 'receiver'), [
    ('target.Secret();', '', 'Provider'),
    ('var local = target; local.Secret();', '', 'Provider'),
    ('field.Secret();', 'private Provider field;', 'Provider'),
    ('Getter.Secret();', 'private Provider Getter {get;set;}', 'Provider'),
    ('target?.Secret();', '', 'Provider'),
    ('target.Label = 3;', '', 'Provider'),
])
def test_receiver_and_assignment_rhs_callsite_evidence(body, fields, receiver):
    check = check_candidate(baseline(), {'Consumer.cs': consumer(body, fields=fields)})
    site = next(d['callsite'] for d in check.errors if d['path'] == 'Consumer.cs')
    assert site['receiver_type'] == receiver
    assert site['member'] in {'Secret', 'Label'}
    assert site['line_text']


def test_plain_data_type_never_receives_monobehaviour_members():
    contract = extract_sources({'Data.cs': 'public class Data {}'})
    check = check_candidate(contract, {'Consumer.cs': 'public class Consumer {void Apply(Data data) {var t=data.transform;}}'})
    assert not check.accepted and 'CS1061' in codes(check)


def test_only_an_approved_prior_addition_is_available_to_later_consumers(tmp_path):
    original = baseline()
    addition = 'public class Extension {public static int Read() {return 3;}}'
    later = {'Later.cs': 'public class Later {int Read() {return Extension.Read();}}'}
    assert not check_candidate(original, later).accepted
    refreshed = approve_candidate(original, {'Extension.cs': addition}, output_dir=tmp_path)
    assert check_candidate(refreshed, later).accepted
    assert 'Extension.cs' not in original.sources
    assert refreshed.type('Extension')['members']
    assert (tmp_path / 'approved/component-api.json').is_file()


def test_failed_addition_cannot_bootstrap_its_unprovided_collaborator(tmp_path):
    original = baseline()
    bad = {'Extension.cs': 'public class Extension {public static int Read() {return Missing.Read();}}'}
    with pytest.raises(ComponentApiError, match='candidate rejected') as rejected:
        approve_candidate(original, bad, output_dir=tmp_path)
    assert rejected.value.sources['Extension.cs'] == bad['Extension.cs']
    assert 'Extension.cs' not in original.sources
    assert 'CS0103' in {d['code'] for d in rejected.value.report['diagnostics']}
    assert not (tmp_path / 'approved').exists()


def test_all_missing_framework_reference_errors_remain_visible(tmp_path):
    source = 'using UnityEngine; public class Actor : MonoBehaviour { public Vector3 Position; }'
    contract = extract_sources({'Actor.cs': source})
    check = check_candidate(contract, {}, output_dir=tmp_path)
    assert not check.accepted
    assert 'CS0246' in codes(check)
    assert len(check.errors) >= 3
    assert check.report['reference_scope'].startswith('SDK core runtime')
    assert check.sources['Actor.cs'] == source


def test_missing_sdk_retains_original_and_candidate(monkeypatch, tmp_path):
    contract = baseline()
    monkeypatch.setattr('src.unity_component_api.shutil.which', lambda _: None)
    source = consumer('target.Run(1);')
    with pytest.raises(ComponentApiUnavailable):
        check_candidate(contract, {'Consumer.cs': source}, output_dir=tmp_path)
    report = json.loads((tmp_path / 'component-api.json').read_text())
    assert report['accepted'] is False and 'failure' in report
    assert (tmp_path / 'sources/Consumer.cs').read_text() == source
    assert (tmp_path / 'baseline/sources/Provider.cs').read_text() == PROVIDER


def test_missing_real_reference_is_a_visible_failure(tmp_path):
    contract = baseline()
    with pytest.raises(ComponentApiUnavailable, match='consumer check unavailable'):
        check_candidate(contract, {'Consumer.cs': consumer('target.Run(1);')},
                        references=[tmp_path / 'absent-UnityEngine.dll'], output_dir=tmp_path / 'evidence')
    assert json.loads((tmp_path / 'evidence/component-api.json').read_text())['accepted'] is False


def test_crlf_and_utf8_sources_are_preserved_byte_for_byte(tmp_path):
    contract = baseline()
    source = '// café\r\n' + consumer('target.Missing();').replace('\n', '\r\n')
    check = check_candidate(contract, {'Consumer.cs': source}, output_dir=tmp_path)
    assert not check.accepted
    assert (tmp_path / 'sources/Consumer.cs').read_bytes() == source.encode('utf-8')
    assert check.sources['Consumer.cs'] == source


def test_duplicate_type_declaration_cannot_become_union():
    check = check_candidate(baseline(), {'Duplicate.cs': 'public class Provider {public void Fake() {}}'})
    assert not check.accepted and 'CS0101' in codes(check)


def test_check_result_views_are_defensive_and_order_is_stable():
    contract = baseline()
    candidates = {'Consumer.cs': consumer('target.Missing();')}
    first = check_candidate(contract, candidates)
    second = check_candidate(contract, dict(reversed(list(candidates.items()))))
    assert first.report == second.report
    first.report['diagnostics'].clear()
    first.sources.clear()
    assert first.errors and first.sources


def test_define_changes_actual_candidate_binding():
    source = 'public class Flagged {\n#if SELECTED\npublic void Run() {}\n#endif\n}'
    plain = extract_sources({'Flagged.cs': source})
    selected = extract_sources({'Flagged.cs': source}, defines=['SELECTED'])
    candidate = {'Consumer.cs': 'public class Consumer {void Apply(Flagged f) {f.Run();}}'}
    assert not check_candidate(plain, candidate).accepted
    assert check_candidate(selected, candidate).accepted


def test_actual_selected_hud_shadow_check_preserves_variant_failure(tmp_path):
    from src.unity_generator import _COMPOSED_CLASS_API
    assert 'ShowLine' in _COMPOSED_CLASS_API['GameHud']
    selected = [Subsystem(kind='hud', variant='health_bar', name='GameHud')]
    contract = extract_components(MicroComposer('Shadow'), selected)
    source = consumer('target.ShowLine("text");', parameters='GameHud target')
    check = check_candidate(contract, {'Consumer.cs': source}, output_dir=tmp_path)
    assert not check.accepted
    assert any(d['code'] == 'CS1061' and d['callsite']['member'] == 'ShowLine' for d in check.errors)
    # Missing Unity references also survive; this is not semantic Unity approval.
    assert 'CS0246' in codes(check)
    assert (tmp_path / 'sources/Consumer.cs').read_text() == source


def test_approval_refuses_unapproved_collaborator_union():
    with pytest.raises(ValueError, match='one source owner'):
        approve_candidate(baseline(), {
            'First.cs': 'public class First {int Read() {return Second.Value;}}',
            'Second.cs': 'public class Second {public static int Value;}',
        })


def test_first_failure_evidence_cannot_be_overwritten(tmp_path):
    contract = baseline()
    original = consumer('target.Missing();')
    check_candidate(contract, {'Consumer.cs': original}, output_dir=tmp_path)
    with pytest.raises(ValueError, match='new attempt directory'):
        check_candidate(contract, {'Consumer.cs': consumer('target.Run(1);')}, output_dir=tmp_path)
    assert (tmp_path / 'sources/Consumer.cs').read_text() == original


def test_real_template_mutation_changes_shadow_consumer_diagnostics(tmp_path):
    root = Path(__file__).resolve().parents[1]
    templates = tmp_path / 'templates'
    shutil.copytree(root / 'templates/unity/micro/hud', templates / 'micro/hud')
    shutil.copy2(root / 'templates/unity/micro/_inject.j2', templates / 'micro/_inject.j2')
    template = templates / 'micro/hud/HUD.cs.j2'
    original = template.read_text()
    composer = MicroComposer('Mutation', template_dir=str(templates))
    selected = [Subsystem(kind='hud', variant='health_bar', name='GameHud')]
    source = {'Consumer.cs': consumer('target.SetHealth(1f,2f);', parameters='GameHud target')}
    before = extract_components(composer, selected)
    first = check_candidate(before, source)
    assert not any(d['code'] == 'CS1061' and d['callsite']['member'] == 'SetHealth' for d in first.errors)
    template.write_text(original.replace('public void SetHealth(', 'public void RenamedHealth('))
    mutated = extract_components(composer, selected)
    changed = check_candidate(mutated, source)
    assert any(d['code'] == 'CS1061' and d['callsite']['member'] == 'SetHealth' for d in changed.errors)
    template.write_text(original)
    restored = extract_components(composer, selected)
    assert restored.to_json() == before.to_json()
    assert check_candidate(restored, source).report == first.report


def test_cli_checks_saved_baseline_and_preserves_failure(tmp_path):
    import subprocess
    import sys
    saved = tmp_path / 'baseline'
    extract_sources({'Provider.cs': PROVIDER}, output_dir=saved)
    candidate = tmp_path / 'Consumer.cs'
    candidate.write_text(consumer('target.Missing();'))
    out = tmp_path / 'attempt-1'
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/check_component_candidate.py'),
               '--baseline', str(saved), '--candidate', 'Consumer.cs', str(candidate), '--output', str(out)]
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode == 1, process.stderr
    assert json.loads(process.stdout)['accepted'] is False
    assert (out / 'sources/Consumer.cs').read_bytes() == candidate.read_bytes()
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode == 2 and 'earlier evidence' in process.stderr


def test_cli_can_read_a_previously_approved_contract(tmp_path):
    import subprocess
    import sys
    saved = tmp_path / 'first'
    approve_candidate(baseline(), {'Extension.cs': 'public class Extension {public static int Read() {return 1;}}'},
                      output_dir=saved)
    candidate = tmp_path / 'Later.cs'
    candidate.write_text('public class Later {int Read() {return Extension.Read();}}')
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/check_component_candidate.py'),
               '--baseline', str(saved / 'approved'), '--candidate', 'Later.cs', str(candidate),
               '--output', str(tmp_path / 'next')]
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)['accepted'] is True


@pytest.mark.parametrize('response', ['not-json', 'null', '[]', '{}', '{"format":"csharp-consumer-check/v1"}',
                                     '{"format":"csharp-consumer-check/v1","accepted":true,"diagnostics":[{}]}'])
def test_malformed_tool_response_preserves_failure_evidence(monkeypatch, tmp_path, response):
    from types import SimpleNamespace
    contract = baseline()
    monkeypatch.setattr('src.unity_component_check._tool', lambda sources: (Path('/tool.dll'), {'sdk': '9.0.312'}))
    monkeypatch.setattr('src.unity_component_check.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=response, stderr=''))
    with pytest.raises(ComponentApiError):
        check_candidate(contract, {'Consumer.cs': consumer('target.Run(1);')}, output_dir=tmp_path)
    report = json.loads((tmp_path / 'component-api.json').read_text())
    assert report['accepted'] is False and report['failure']
    assert (tmp_path / 'baseline/sources/Provider.cs').read_text() == PROVIDER


def test_assignment_with_member_rhs_keeps_the_target_receiver():
    check = check_candidate(baseline(), {'Consumer.cs': consumer('target.Label = other.Value;',
                            parameters='Provider target, Provider other')})
    site = next(d['callsite'] for d in check.errors if d['code'] == 'CS0029')
    assert site['receiver'] == 'target'
    assert site['member'] == 'Label'
    assert site['assignment_target'] == 'target.Label'
    assert site['assignment_value'] == 'other.Value'


def test_explicit_official_reference_profile_does_not_mix_host_corelib():
    dotnet = Path(shutil.which('dotnet')).resolve()
    profiles = sorted((dotnet.parent / 'packs/Microsoft.NETCore.App.Ref').glob('*/ref/net9.0'))
    assert profiles, 'pinned SDK official reference pack missing'
    references = sorted(profiles[-1].glob('*.dll'))
    contract = extract_sources({'Provider.cs': PROVIDER})
    check = check_candidate(contract, {'Consumer.cs': consumer('target.Run(1);')}, references=references)
    assert check.accepted, check.report
    assert check.report['core_runtime']['used_as_compilation_reference'] is False
    assert check.report['reference_scope'].startswith('explicit reference profile')
    assert len(check.report['references']) == len(references)


@pytest.mark.parametrize('diagnostic', [
    {'severity': [], 'path': 'Consumer.cs', 'code': 'CS0000', 'message': 'fixture'},
    {'severity': 'error', 'path': {}, 'code': 'CS0000', 'message': 'fixture'},
])
def test_nonhashable_malformed_diagnostic_retains_evidence(monkeypatch, tmp_path, diagnostic):
    from types import SimpleNamespace
    contract = baseline()
    monkeypatch.setattr('src.unity_component_check._tool', lambda sources: (Path('/tool.dll'), {'sdk': '9.0.312'}))
    raw = {'format': 'csharp-consumer-check/v1', 'language': 'CSharp9', 'defines': [], 'accepted': False,
           'roslyn': 'fixture', 'core_runtime': {}, 'diagnostics': [diagnostic]}
    monkeypatch.setattr('src.unity_component_check.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=json.dumps(raw), stderr=''))
    with pytest.raises(ComponentApiError):
        check_candidate(contract, {'Consumer.cs': consumer('target.Run(1);')}, output_dir=tmp_path)
    assert json.loads((tmp_path / 'component-api.json').read_text())['accepted'] is False
    assert (tmp_path / 'sources/Consumer.cs').is_file()
