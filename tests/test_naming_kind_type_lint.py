"""Naming-drift lint: `kind` and `type` are both canonical in this codebase
(see docs/NAMING_KIND_VS_TYPE.md) but mean different things — asset/config
CATEGORY vs. behavior/action CLASSIFICATION. No class across either backend's
schema/resolver/generator layer defines both today. This test locks that in:
a future edit that adds a `type` field to an existing `kind`-bearing class
(or vice versa) fails here with the file/line/class name, instead of the
ambiguity being discovered later as a resolver bug.

Deterministic, no imports of the scanned modules (pure src.naming_lint AST
walk) — never triggers optional heavy dependencies.
"""
from __future__ import annotations

from pathlib import Path

from src.naming_lint import default_scan_roots, scan_paths

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_class_declares_both_kind_and_type():
    collisions = scan_paths(default_scan_roots(_REPO_ROOT))
    assert not collisions, (
        "kind/type naming drift detected -- a class below declares BOTH "
        "fields, which is the exact ambiguity this lint guards against "
        "(see docs/NAMING_KIND_VS_TYPE.md for the canonical split):\n"
        + "\n".join(str(c) for c in collisions)
    )


def test_scanner_actually_detects_a_real_collision(tmp_path):
    """Regression guard for the scanner itself: prove it fires on a
    synthetic class before trusting its silence on the real tree."""
    bad = tmp_path / "bad_module.py"
    bad.write_text(
        "class Drifted:\n"
        "    kind: str = 'a'\n"
        "    type: str = 'b'\n",
        encoding="utf-8",
    )
    collisions = scan_paths([bad])
    assert len(collisions) == 1
    assert collisions[0].class_name == "Drifted"


def test_scanner_ignores_separate_classes_each_with_one_field(tmp_path):
    ok = tmp_path / "ok_module.py"
    ok.write_text(
        "class A:\n"
        "    kind: str = 'a'\n"
        "class B:\n"
        "    type: str = 'b'\n",
        encoding="utf-8",
    )
    assert scan_paths([ok]) == []
