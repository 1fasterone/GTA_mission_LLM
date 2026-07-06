"""
Byte-identical reproducibility for the 2D golden archetype — mirrors
test_unity_golden_reproducibility.py's 3D tests. Also regression-tests the two
2D-pipeline issues noted in project history (humanoid animator on 2D, missing
Collider2D authoring): a full 2D composition must reference only 2D physics/
collider APIs, never the 3D equivalents.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for

# 3D-only APIs that must never leak into a 2D-composed project. Collider/
# Rigidbody are matched only when NOT immediately followed by "2D" (negative
# lookahead), so Collider2D/Rigidbody2D usages are correctly excluded.
_THREE_D_LEAK_RE = re.compile(r"\b(CharacterController|Physics\.Raycast|Rigidbody(?!2D)\b|Collider(?!2D)\b)")


def _emit(tmp_path: Path, subsystems, project="Golden2DProj") -> dict[str, str]:
    assets = tmp_path / "Assets"
    composer = MicroComposer(project, project_config={"audio": {"backend": "dsp"}})
    composer.emit(subsystems, assets, has_game_manager=False)
    files = sorted(assets.rglob("*.cs")) + sorted(assets.rglob("*.cs.meta"))
    return {str(f.relative_to(assets)).replace("\\", "/"): f.read_text(encoding="utf-8")
            for f in files}


def _hash(contents: dict[str, str]) -> str:
    blob = "".join(f"{k}\n{v}" for k, v in sorted(contents.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _golden_2d_subsystems():
    return subsystems_for("2D", ["audio"], locomotion="walk", genre="universal")


def test_golden_2d_archetype_is_byte_identical_across_runs(tmp_path):
    a = _emit(tmp_path / "run_a", _golden_2d_subsystems())
    b = _emit(tmp_path / "run_b", _golden_2d_subsystems())
    assert a == b
    assert _hash(a) == _hash(b)


def test_golden_2d_archetype_has_no_3d_api_leaks(tmp_path):
    contents = _emit(tmp_path, _golden_2d_subsystems())
    leaks = [f"{path}: {m.group(0)}"
             for path, text in contents.items() if path.endswith(".cs")
             for m in _THREE_D_LEAK_RE.finditer(text)]
    assert not leaks, "3D-only API leaked into a 2D-composed project:\n  " + "\n  ".join(leaks)


def test_golden_2d_archetype_trigger_and_interactable_use_collider2d(tmp_path):
    contents = _emit(tmp_path, _golden_2d_subsystems())
    trigger = next(v for k, v in contents.items() if k.endswith("SceneTrigger.cs"))
    interactable = next(v for k, v in contents.items() if k.endswith("WorldInteractable.cs"))
    assert "OnTriggerEnter2D(Collider2D" in trigger
    assert "OnTriggerEnter2D(Collider2D" in interactable
