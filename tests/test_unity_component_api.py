"""Engineer-authored shadow fixtures; no live Pipeline LLM fidelity claims."""
from pathlib import Path
import json
import os
import shutil
import pytest
from src.unity_component_api import ComponentApiError, ComponentApiUnavailable, extract_components, extract_sources, render_baseline
from src.unity_compose import MicroComposer, Subsystem
ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def roslyn():
    if shutil.which('dotnet') is None:
        if os.environ.get('REQUIRE_COMPONENT_API') == '1':
            pytest.fail('required pinned SDK unavailable')
        pytest.skip('component extraction unverified: pinned SDK unavailable')

def api(kind, variant='', name='Fixture', **options):
    return extract_components(MicroComposer('ApiFixture'), [Subsystem(kind=kind, variant=variant, name=name, options=options)])

def members(contract, name='Fixture'):
    return {m['name'] for m in contract.type(name)['members'] if m['accessibility']=='public'}

def member(contract, name):
    return next(m for m in contract.type('Fixture')['members'] if m['name']==name)

def test_missing_tooling_preserves_failed_source(monkeypatch,tmp_path):
    monkeypatch.setattr('src.unity_component_api.shutil.which',lambda _:None)
    source='public class Kept { public int Value; }'
    with pytest.raises(ComponentApiUnavailable,match='pinned .NET SDK unavailable'):
        extract_sources({'Kept.cs':source},output_dir=tmp_path)
    assert (tmp_path/'sources/Kept.cs').read_text()==source
    assert 'failure' in json.loads((tmp_path/'component-api.json').read_text())

def test_hud_variants_dynamic_widgets_and_renamed_class(roslyn):
    health=api('hud','health_bar','Renamed',widgets=[{'id':'oxygen level','kind':'bar'}])
    dialogue=api('hud','dialogue_box','Renamed')
    assert {'SetHealth','SetOxygenLevel'} <= members(health,'Renamed')
    assert 'ShowLine' not in members(health,'Renamed')
    assert 'ShowLine' in members(dialogue,'Renamed')
    assert 'SetOxygenLevel' not in members(dialogue,'Renamed')
    assert health.fingerprint!=dialogue.fingerprint

def test_save_slot_signatures_and_helper_project_type(roslyn):
    plain=api('saveload','json_file');slots=api('saveload','save_slots')
    assert member(plain,'Save')['parameters']==[]
    assert [p['type'] for p in member(slots,'Save')['parameters']]==['int']
    assert member(plain,'Data')['type']=='FixtureData'
    assert plain.type('FixtureData')['kind']=='class'
    assert member(plain,'Data')['setter']['accessibility']=='private'

def test_pointer_multimode_and_ordinary_controller_surfaces(roslyn):
    ordinary=api('controller','third_person',input_backend='legacy')
    pointer=api('controller','pointer',input_backend='legacy')
    multi=api('controller','multi_mode',input_backend='legacy',modes=[{'kind':'on_foot'},{'kind':'vehicle_car'}])
    assert 'IsGrounded' in members(ordinary)
    assert 'IsGrounded' not in members(pointer)
    assert {'Select','Selected','OnPicked'} <= members(pointer)
    assert 'SwitchMode' in members(multi)-members(ordinary)
    assert member(pointer,'OnPicked')['kind']=='event'

def test_animator_dynamic_names_and_optional_arguments(roslyn):
    contract=api('animator',params=[{'name':'Oxygen','param_type':'float'},{'name':'Dive','param_type':'trigger'}])
    assert {'SetOxygen','PlayDive','SetGrounded'} <= members(contract)
    assert 'PlayInvented' not in members(contract)
    assert member(contract,'CrossFade')['parameters'][1]['optional']

def test_authored_fragment_cannot_define_its_judged_baseline(roslyn):
    sub=Subsystem(kind='hud',variant='health_bar',name='Fixture',options={
        'extra_methods':[{'signature':'public void Invented()','body':''}],
        'extensions_methods':[{'signature':'public void Extension()','body':''}],
        'awake_extra':'Invented();'})
    before=sub.model_dump_json();contract=extract_components(MicroComposer('Isolation'),[sub])
    assert not {'Invented','Extension'} & members(contract)
    assert sub.model_dump_json()==before

def test_project_inheritance_generics_overloads_modifiers_and_framework_boundary(roslyn):
    contract=extract_sources({'Project.cs':'''using System;
namespace Contracts {
 public class Parent<T> { public T Value {get; protected set;} private void Secret() {} }
 public class Fixture : Parent<string> {
 public static readonly int Limit=2; public event Action<int> Changed;
 public void Run(int value=2) {} public void Run(params string[] values) {}
 public U Convert<U>(ref U value,out int code) {code=0;return value;}
 }}'''})
    assert contract.type('Fixture')['bases']==[{'type':'Contracts.Parent<string>','definition':'Contracts.Parent<T>','scope':'project','unresolved':False}]
    methods=[m for m in contract.type('Fixture')['members'] if m['name']=='Run']
    assert len(methods)==2
    assert {m['parameters'][0]['type'] for m in methods}=={'int','string[]'}
    assert any(m['parameters'][0]['optional'] for m in methods)
    assert any(m['parameters'][0]['params'] for m in methods)
    assert member(contract,'Convert')['generic_arity']==1
    assert [p['modifier'] for p in member(contract,'Convert')['parameters']]==['ref','out']
    assert member(contract,'Limit')['is_static']
    assert member(contract,'Changed')['parameters'][0]['type']=='int'
    assert next(m for m in contract.type('Parent')['members'] if m['name']=='Secret')['accessibility']=='private'
    assert 'ToString' not in members(contract)

def test_repeat_render_extract_and_source_order_is_identical(roslyn):
    selected=[Subsystem(kind='hud',name='Fixture',options={'widgets':[{'id':'depth','kind':'counter'}]}),Subsystem(kind='saveload',variant='save_slots',name='Archive')]
    composer=MicroComposer('Repeat');a,sel=render_baseline(composer,selected);b,sel2=render_baseline(composer,selected)
    assert a==b and sel==sel2
    first=extract_sources(a,selection=sel);second=extract_sources(dict(reversed(list(b.items()))),selection=sel2)
    assert first.to_json()==second.to_json()
    assert first.document['toolchain']['sdk']=='9.0.312'

def test_malformed_source_retains_roslyn_diagnostic(roslyn,tmp_path):
    source='public class Broken { public void Go() { public int Wrong; } }'
    with pytest.raises(ComponentApiError,match='CS') as failure:
        extract_sources({'Broken.cs':source},output_dir=tmp_path)
    assert failure.value.report['diagnostics'][0]['path']=='Broken.cs'
    assert (tmp_path/'sources/Broken.cs').read_text()==source

def test_real_template_export_mutation_changes_contract_without_table_edit(roslyn,tmp_path):
    root=tmp_path/'templates';shutil.copytree(ROOT/'templates/unity/micro/hud',root/'micro/hud')
    shutil.copy2(ROOT/'templates/unity/micro/_inject.j2',root/'micro/_inject.j2')
    template=root/'micro/hud/HUD.cs.j2';original=template.read_text()
    composer=MicroComposer('Mutation',template_dir=str(root));selected=[Subsystem(kind='hud',variant='health_bar',name='Fixture')]
    before=extract_components(composer,selected)
    assert 'SetHealth' in members(before)
    assert 'public void SetHealth(' in original
    template.write_text(original.replace('public void SetHealth(','public void RenamedHealth('))
    changed=extract_components(composer,selected)
    assert 'SetHealth' not in members(changed)
    assert 'RenamedHealth' in members(changed)
    assert before.fingerprint!=changed.fingerprint
    template.write_text(original)
    assert extract_components(composer,selected).to_json()==before.to_json()

def test_shadow_comparison_exposes_union_false_positive_without_cutover(roslyn):
    from src.unity_generator import _COMPOSED_CLASS_API
    contract=api('hud','health_bar','GameHud')
    assert 'ShowLine' in _COMPOSED_CLASS_API['GameHud']
    assert 'ShowLine' not in members(contract,'GameHud')
    assert 'SetHealth' in _COMPOSED_CLASS_API['GameHud'] & members(contract,'GameHud')


def test_compile_matrix_contains_actual_selected_variants_and_project_interfaces(roslyn,tmp_path):
    from tools.component_api_fixtures import materialize,selections
    report=materialize(tmp_path,ROOT/'templates/unity',extract=True)
    assert {row['fixture'] for row in report['selections']}==set(selections())
    sources={logical:(tmp_path/'Assets/ComponentApiFixtures'/logical).read_text() for logical in report['sources']}
    contract=extract_sources(sources,selection=report['selections'])
    interface=next(b for b in contract.type('ComponentApiFixtures.MultiController.FixturePilot')['bases'] if b['definition'].endswith('IVehicleState'))
    assert interface['scope']=='project'
    assert not contract.document['diagnostics']


def test_duplicate_nonpartial_declarations_cannot_form_an_api_union(roslyn,tmp_path):
    with pytest.raises(ComponentApiError,match='CS0101') as failure:
        extract_sources({'A.cs':'public class Same { public void First() {} }','B.cs':'public class Same { public void Second() {} }'},output_dir=tmp_path)
    assert any(d['code']=='CS0101' for d in failure.value.report['diagnostics'])
    assert (tmp_path/'sources/A.cs').is_file() and (tmp_path/'sources/B.cs').is_file()


def test_selected_writers_cannot_overwrite_each_others_contract(roslyn):
    selected=[Subsystem(kind='hud',variant='health_bar',name='Same'),Subsystem(kind='hud',variant='dialogue_box',name='Same')]
    with pytest.raises(ComponentApiError,match='writers collide'):
        extract_components(MicroComposer('Collision'),selected)


def test_legitimate_partial_project_declarations_are_combined(roslyn):
    contract=extract_sources({'A.cs':'public partial class Fixture {public void First() {}}','B.cs':'public partial class Fixture {public void Second() {}}'})
    assert {'First','Second'} <= members(contract)

# Engineer-authored adversarial fixtures. These exercise the inspection boundary;
# they are not Pipeline LLM output or runtime gameplay evidence.
_FSM_DECLARATION_ESCAPE = (
    'break;\n }\n }\n public void Invented() {}\n'
    ' private void CarryOn() { switch (0) { case 0:'
)
_ANIMATOR_DECLARATION_ESCAPE = (
    '0f); } public void Invented() {} '
    'private void Padding(){ _anim.SetFloat(0, 0f'
)


@pytest.mark.parametrize('channel', ['on_enter', 'on_update', 'on_exit'])
def test_nested_fsm_code_cannot_define_baseline_declarations(roslyn, channel):
    selected=Subsystem(kind='statemachine',variant='object_fsm',name='Fixture',options={
        'states':[{'name':'Inactive',channel:_FSM_DECLARATION_ESCAPE},{'name':'Active'}],
        'transitions':[{'from_state':'Inactive','to_state':'Active','condition':'true'}]})
    before=selected.model_dump_json()
    contract=extract_components(MicroComposer('NestedFsm'),[selected])
    assert 'Invented' not in members(contract)
    assert {'Inactive','Active'} <= {m['name'] for m in contract.type('Fixture.State')['members']}
    assert not contract.document['diagnostics']
    assert selected.model_dump_json()==before


def test_fsm_condition_is_excluded_before_fixer_infers_members(roslyn):
    def selected(condition):
        return Subsystem(kind='statemachine',variant='object_fsm',name='Fixture',options={
            'states':[{'name':'Inactive'},{'name':'Active'}],
            'transitions':[{'from_state':'Inactive','to_state':'Active','condition':condition}]})
    composer=MicroComposer('Conditions')
    before=selected('_authoredFlag').model_dump_json()
    first=selected('_authoredFlag')
    contract=extract_components(composer,[first])
    assert '_authoredFlag' not in {m['name'] for m in contract.type('Fixture')['members']}
    assert contract.to_json()==extract_components(composer,[selected('AnotherAuthoredCall()')]).to_json()
    assert first.model_dump_json()==before


@pytest.mark.parametrize('param_type', ['float','int','bool'])
def test_animator_expression_is_excluded_but_driven_shape_is_retained(roslyn,param_type):
    sub=Subsystem(kind='animator',name='Fixture',options={'params':[
        {'name':'Driven','param_type':param_type,'driven_by':_ANIMATOR_DECLARATION_ESCAPE},
        {'name':'External','param_type':param_type,'driven_by':''},
        {'name':'Pulse','param_type':'trigger','driven_by':''}]})
    before=sub.model_dump_json()
    contract=extract_components(MicroComposer('Driven'),[sub])
    assert not {'Invented','SetDriven'} & members(contract)
    assert {'SetExternal','PlayPulse'} <= members(contract)
    other=sub.model_copy(deep=True)
    other.options['params'][0]['driven_by']='_differentAuthoredExpression'
    assert contract.to_json()==extract_components(MicroComposer('Driven'),[other]).to_json()
    assert sub.model_dump_json()==before


def test_normal_writers_preserve_nested_authored_code(tmp_path):
    composer=MicroComposer('NormalProduction')
    fsm=Subsystem(kind='statemachine',variant='object_fsm',name='NormalFsm',options={
        'states':[{'name':'Inactive','on_enter':'Debug.Log("authored hook");'},{'name':'Active'}],
        'transitions':[{'from_state':'Inactive','to_state':'Active','condition':'true'}]})
    animator=Subsystem(kind='animator',name='NormalAnimator',options={
        'params':[{'name':'Depth','param_type':'float','driven_by':'Time.deltaTime'}]})
    for sub,marker in [(fsm,'Debug.Log("authored hook");'),(animator,'Time.deltaTime')]:
        pipeline,schema,variant,_,_=composer._resolve(sub)
        out=tmp_path/sub.kind;pipeline.write_files(schema,variant,out)
        assert marker in '\n'.join(p.read_text() for p in out.rglob('*.cs'))


@pytest.mark.parametrize('backend',['openrouter','comfyui'])
def test_sprite_inspection_never_invokes_generation_even_without_sdk(monkeypatch,tmp_path,backend):
    from unittest.mock import Mock
    bank=Mock(side_effect=AssertionError('inspection must not synthesize sprites'))
    monkeypatch.setattr('src.sprites.content.generate_sprite_bank',bank)
    monkeypatch.setattr('src.unity_component_api.shutil.which',lambda _:None)
    selected=Subsystem(kind='sprite_animator',name='Fixture')
    before=selected.model_dump_json()
    with pytest.raises(ComponentApiUnavailable):
        extract_components(MicroComposer('SpriteInspection',project_config={'sprites':{'backend':backend}}),[selected],output_dir=tmp_path)
    bank.assert_not_called()
    assert 'class Fixture' in (tmp_path/'sources/000-sprite_animator/Scripts/Fixture.cs').read_text()
    assert selected.model_dump_json()==before


def test_sprite_inspection_and_normal_writer_share_exact_csharp(tmp_path,monkeypatch):
    from unittest.mock import Mock
    bank=Mock(return_value={'generated':24,'placeholders':0,'frame_count':24})
    monkeypatch.setattr('src.sprites.content.generate_sprite_bank',bank)
    sub=Subsystem(kind='sprite_animator',name='Fixture')
    composer=MicroComposer('SharedSpriteWriter')
    inspected,_=render_baseline(composer,[sub])
    bank.assert_not_called()
    pipeline,schema,variant,_,_=composer._resolve(sub)
    out=tmp_path/'normal';pipeline.write_files(schema,variant,out)
    assert bank.call_count==1 and len(bank.call_args.args[0])==24
    actual=(out/'Scripts/Fixture.cs').read_bytes().decode('utf-8')
    assert inspected['000-sprite_animator/Scripts/Fixture.cs']==actual
    # The production renderer still owns and emits its normal asset manifest.
    assert (out/'Resources/sprite_manifest.json').is_file()


def test_audio_and_dialog_inspection_never_fulfills_assets(monkeypatch,tmp_path):
    from unittest.mock import Mock
    audio=Mock(side_effect=AssertionError('unexpected clip synthesis'))
    dialog=Mock(side_effect=AssertionError('unexpected voice synthesis'))
    monkeypatch.setattr('src.audio.safe_content.generate_bank_safe',audio)
    monkeypatch.setattr('src.audio.dialog_content.generate_dialog_bank',dialog)
    composer=MicroComposer('AudioInspection',project_config={'audio':{'generate_clips':True,'backend':'stable3','defer_stable3':True}})
    selected=[Subsystem(kind='audio',variant='crossfade_zones',name='Bank'),Subsystem(kind='npc_dialog',name='Speaker')]
    sources,_=render_baseline(composer,selected)
    assert any(p.endswith('BankCrossfadeZone.cs') for p in sources)
    assert any(p.endswith('SpeakerPlayer.cs') for p in sources)
    audio.assert_not_called();dialog.assert_not_called()
    # Inspection must also restore a writer's prior synthesis setting.
    for sub,flag in zip(selected,['_with_clips','_with_audio']):
        pipeline,schema,variant,_,_=composer._resolve(sub)
        setattr(pipeline,flag,True)
        out=tmp_path/sub.kind;pipeline.write_api_sources(schema,variant,out)
        assert getattr(pipeline,flag) is True
        assert not list(out.rglob('*.wav')) and not list(out.rglob('*batch*'))
