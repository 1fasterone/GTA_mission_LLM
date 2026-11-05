"""Engineer-authored rendered variant matrix; compiled, never scene-attached."""
from pathlib import Path
import json
from src.unity_component_api import extract_sources, render_baseline
from src.unity_compose import MicroComposer, Subsystem
from src.unity_generator import _make_guid, _meta_file


def selections():
    matrix = {
        'HealthHud': [Subsystem(kind='hud',variant='health_bar',name='HealthReadout',options={'widgets':[{'id':'oxygen level','kind':'bar'},{'id':'depth','kind':'counter'}]})],
        'DialogueHud': [Subsystem(kind='hud',variant='dialogue_box',name='DialogueReadout')],
        'PlainSave': [Subsystem(kind='saveload',variant='json_file',name='PlainArchive')],
        'SlottedSave': [Subsystem(kind='saveload',variant='save_slots',name='SlottedArchive')],
        'DynamicAnimator': [Subsystem(kind='animator',name='CreatureDriver',options={'params':[{'name':'Oxygen','param_type':'float'},{'name':'Dive','param_type':'trigger'}]})],
        'DrivenAnimator': [Subsystem(kind='animator',name='DrivenDriver',options={'params':[
            {'name':'Speed','param_type':'float','driven_by':'Time.deltaTime'},
            {'name':'Visible','param_type':'bool','driven_by':'true'},
            {'name':'Charge','param_type':'int','driven_by':'1'}]})],
        'NestedStateMachine': [Subsystem(kind='statemachine',variant='object_fsm',name='StateDriver',options={
            'states':[{'name':'Inactive','on_enter':'Debug.Log("authored");'},{'name':'Active'}],
            'transitions':[{'from_state':'Inactive','to_state':'Active','condition':'true'}]})],
        'SpriteSourceOnly': [Subsystem(kind='sprite_animator',variant='player',name='SpriteDriver')],
    }
    for label,variant,options in [('OrdinaryController','third_person',{}),('PointerController','pointer',{}),('MultiController','multi_mode',{'modes':[{'kind':'on_foot'},{'kind':'vehicle_car'}]})]:
        matrix[label]=[Subsystem(kind='controller',variant=variant,name='FixturePilot',options={'input_backend':'legacy',**options}),Subsystem(kind='input',variant='keyboard_mouse',name='FixtureInput')]
    return matrix


def matrix_sources(template_root):
    sources={};selected=[]
    for label,subs in selections().items():
        rendered,rows=render_baseline(MicroComposer('ComponentApiFixture',template_dir=str(template_root)),subs)
        if label=='MultiController':
            rendered['project/IVehicleState.cs']=(template_root/'IVehicleState.cs.j2').read_text()
        for logical,text in rendered.items():
            sources[f'{label}/{logical}']=f'namespace ComponentApiFixtures.{label}\n{{\n{text}\n}}\n'
        selected.extend({'fixture':label,**row} for row in rows)
    return sources,selected


def materialize(project:Path,template_root:Path,*,extract=False):
    sources,selected=matrix_sources(template_root)
    for logical,text in sources.items():
        target=project/'Assets/ComponentApiFixtures'/logical;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text,encoding='utf-8')
        target.with_suffix('.cs.meta').write_text(_meta_file(_make_guid(f'ComponentApiFixtures/{logical}')),encoding='utf-8')
    report={'format':'component-api-fixture-matrix/v1','selections':selected,'sources':sorted(sources),'evidence':'engineer-authored synthetic fixtures; no runtime claim'}
    (project/'component-api-fixtures.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    if extract:extract_sources(sources,selection=selected,output_dir=project/'ComponentApiEvidence')
    return report
