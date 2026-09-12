"""Engineer-authored stat fixtures; these do not evaluate pipeline schema-fill."""
from pathlib import Path
import shutil
import subprocess

import pytest
from src.unity_compose import MicroComposer, Subsystem


def test_stat_bridge_composes_readers_and_preserves_actor_scope(tmp_path):
    assets = tmp_path / 'Assets'
    MicroComposer('Stats').compose([
        Subsystem(kind='inventory', name='Bag'),
        Subsystem(kind='controller', variant='third_person', target='player', name='Runner'),
        Subsystem(kind='health', target='player', name='Body'),
        Subsystem(kind='ai', variant='fsm_guard', target='world', name='Guard'),
    ], assets)
    context = (assets / 'Micros/context/Scripts/GameContext.cs').read_text(encoding="utf-8")
    assert '__inventory.OnStatBoostEffect +=' in context
    assert '__owners.Count != 1' in context
    assert 'FindObjectsByType<Runner>' in context
    # World guards receive their own stores, not the player's potion.
    owner_collection = context.split('var __owners =')[1]
    assert 'FindObjectsByType<Guard>' not in owner_collection
    assert '__reader.GetComponent<StatModifier>()' in context
    assert '__stats.Support("aggression")' in context
    assert '\\n' not in context
    assert (assets / 'Micros/context/Scripts/StatModifier.cs.meta').exists()
    health = (assets / 'Micros/health/Scripts/Body.cs').read_text(encoding="utf-8")
    assert 'ReadStat?.Invoke("armor")' in health
    controller = (assets / 'Micros/controller/Scripts/Runner.cs').read_text(encoding="utf-8")
    assert 'ReadStat?.Invoke("speed")' in controller
    ai = (assets / 'Micros/ai/Scripts/Guard.cs').read_text(encoding="utf-8")
    assert 'toTarget.magnitude > EffectiveDetectionRadius' in ai
    assert '_agent.speed = EffectiveMoveSpeed' in ai


@pytest.mark.parametrize('variant', ['equipment', 'slot_based'])
def test_no_stat_receiver_never_emits_an_arbitrary_target(tmp_path, variant):
    assets = tmp_path / 'Assets'
    MicroComposer('Missing').compose([Subsystem(kind='inventory', variant=variant)], assets)
    context = (assets / 'Micros/context/Scripts/GameContext.cs').read_text(encoding="utf-8")
    if variant == 'equipment':
        assert 'OnStatBoostEffect' not in context
        assert not (assets / 'Micros/context/Scripts/StatModifier.cs').exists()
    else:
        assert 'Expected one item-effect actor' in context
        assert '__owners.Add' not in context


@pytest.mark.skipif(shutil.which('dotnet') is None, reason='requires .NET SDK')
def test_modifier_runtime_stacking_expiry_decay_and_isolation(tmp_path):
    # Execute the generated store itself against a minimal Unity time adapter.
    source = Path('templates/unity/compose/StatModifier.cs.j2').read_text(encoding="utf-8")
    (tmp_path / 'StatModifier.cs').write_text(source)
    (tmp_path / 'Proof.csproj').write_text('''<Project Sdk="Microsoft.NET.Sdk">
<PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup>
</Project>''')
    (tmp_path / 'NuGet.Config').write_text('<configuration><packageSources><clear /></packageSources></configuration>')
    (tmp_path / 'Program.cs').write_text('''using System;
namespace UnityEngine {
 public class MonoBehaviour {}
 public static class Time { public static float time; }
 public static class Mathf { public static float Max(float a,float b) => Math.Max(a,b); }
 public static class Debug { public static int warnings; public static void LogWarning(string s, object o) { warnings++; } }
}
class Program {
 static void Eq(float got,float want) { if (Math.Abs(got-want)>0.0001f) throw new Exception($"{got} != {want}"); }
 static void Main() {
  var a=new StatModifier(); var b=new StatModifier();
  a.Support("speed"); a.Support("armor"); a.Support("aggression"); b.Support("speed");
  a.ApplyItem(.5f,"speed"); a.Apply("speed",1f,10f,true);
  Eq(a.Read("speed"),1.5f); Eq(b.Read("speed"),0f);
  UnityEngine.Time.time=5f; Eq(a.Read("speed"),1f);
  UnityEngine.Time.time=10f; Eq(a.Read("speed"),.5f);
  a.Apply("armor",1f,2f); Eq(a.Read("armor"),1f);
  UnityEngine.Time.time=12f; Eq(a.Read("armor"),0f);
  if (a.Apply("unknown",1f) || a.Apply("speed",float.NaN) || a.Apply("speed",1f,-1f) || a.Apply("speed",1f,0f,true)) throw new Exception("invalid accepted");
  Eq(a.Read("speed"),.5f); Eq(UnityEngine.Debug.warnings,4f);
  a.Apply("aggression",.25f); Eq(a.Read("aggression"),.25f);
 }
}''')
    result = subprocess.run(['dotnet', 'run', '--project', str(tmp_path / 'Proof.csproj')],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
