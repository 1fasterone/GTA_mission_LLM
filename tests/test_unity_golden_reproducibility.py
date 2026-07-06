"""
Byte-identical reproducibility for the deterministic compose chain (Steps 4-6:
Resolver -> Fixer -> Generator equivalent for --compose: subsystems_for() ->
MicroComposer.plan/emit()).

Definition: given the same subsystem list + config, two independent emit()
runs must produce byte-for-byte identical Assets/ file trees (same paths,
same contents, same deterministic .meta GUIDs). This is achievable because
there is no live LLM call and no datetime/uuid/random nondeterminism anywhere
in the compose/audio/atoms chain (verified by grep) once the audio backend is
"dsp" (the required_cues() genre-preset fallback, itself a pure dict lookup).

Also regression-tests the original bug: GameWiring.cs must never reference the
old hardcoded "player_hurt"/"player_death"/"checkpoint_save_led" cue ids unless
a resolved cue genuinely matches — see src/audio/resolver.py::match_cue_for_role.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from src.unity_compose import MicroComposer, Subsystem, subsystems_for

_OLD_HARDCODED_CUES = ("player_hurt", "player_death", "checkpoint_save_led")


def _emit(tmp_path: Path, subsystems, project="GoldenProj") -> dict[str, str]:
    assets = tmp_path / "Assets"
    composer = MicroComposer(project, project_config={"audio": {"backend": "dsp"}})
    composer.emit(subsystems, assets, has_game_manager=False)
    files = sorted(assets.rglob("*.cs")) + sorted(assets.rglob("*.cs.meta"))
    return {str(f.relative_to(assets)).replace("\\", "/"): f.read_text(encoding="utf-8")
            for f in files}


def _hash(contents: dict[str, str]) -> str:
    blob = "".join(f"{k}\n{v}" for k, v in sorted(contents.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _golden_3d_subsystems():
    return subsystems_for("3D", ["audio"], locomotion="walk", genre="universal")


def test_golden_3d_archetype_is_byte_identical_across_runs(tmp_path):
    a = _emit(tmp_path / "run_a", _golden_3d_subsystems())
    b = _emit(tmp_path / "run_b", _golden_3d_subsystems())
    assert a == b
    assert _hash(a) == _hash(b)


def test_golden_3d_archetype_gamewiring_seam3_omits_unmatched_roles(tmp_path):
    contents = _emit(tmp_path, _golden_3d_subsystems())
    gw = contents["Micros/wiring/Scripts/GameWiring.cs"]
    for old_id in _OLD_HARDCODED_CUES:
        assert old_id not in gw, (
            f"GameWiring.cs must never reference the old hardcoded cue id "
            f"{old_id!r} unless a resolved cue genuinely matches it"
        )


def test_golden_3d_archetype_seam3_uses_real_cue_when_matched(tmp_path):
    subs = [s for s in _golden_3d_subsystems() if s.kind != "audio"]
    subs.append(Subsystem(
        kind="audio", variant="categorical_mixer", target="systems", name="AudioDirector",
        options={"meta": {"_pre_cues": [
            {"id": "crate_break", "category": "sfx",
             "descriptor": "wooden crate breaking apart with a splintering crack",
             "gen_prompt": "wooden crate breaking"},
        ]}},
    ))
    contents = _emit(tmp_path, subs)
    gw = contents["Micros/wiring/Scripts/GameWiring.cs"]
    assert 'Play("crate_break")' in gw
    for old_id in _OLD_HARDCODED_CUES:
        assert old_id not in gw
    # only "break" (death role) matches this descriptor — hurt/heal have no
    # matching keyword, so those seam lines/delegates must be omitted entirely
    assert "_onDamaged" not in gw
    assert "_onHealed" not in gw
