"""Digest-inventory lint (docs/SCENE_GRAPH_COMPILER.md, Phase 0).

src/ident/ is the one place identity is derived. Every other `hashlib` call in
src/ and cli/ must be inventoried in src/ident/frozen.py with a reason — and the
inventory must match the tree exactly, so a new ad-hoc digest fails here with
its file, scope and line instead of quietly becoming a fifth canonicalization.

Pure AST (src.ident.lint), no imports of the scanned modules.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ident.frozen import SITES, DigestSite
from src.ident.lint import audit, scan_file, scan_tree

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_inventory_matches_the_tree_exactly():
    problems = audit(scan_tree(_REPO_ROOT), SITES)
    assert not problems, (
        "hashlib digest inventory is out of sync with the tree "
        "(see src/ident/frozen.py):\n" + "\n".join(problems))


def test_every_migrate_row_names_a_plan_slice_that_exists():
    doc = (_REPO_ROOT / "docs" / "SCENE_GRAPH_COMPILER.md").read_text(encoding="utf-8")
    for s in SITES:
        if s.disposition == "migrate":
            assert f"### {s.slice} " in doc, f"{s.path} {s.scope}: no slice {s.slice!r}"


def test_the_substrate_itself_is_not_scanned():
    assert not any(c.path.startswith("src/ident/") for c in scan_tree(_REPO_ROOT))


# ── the scanner itself ────────────────────────────────────────────────────────

def _scan(tmp_path: Path, source: str):
    f = tmp_path / "mod.py"
    f.write_text(source, encoding="utf-8")
    return scan_file(f, "src/mod.py")


def test_scanner_finds_plain_aliased_and_from_imports(tmp_path):
    calls = _scan(tmp_path, (
        "import hashlib\n"
        "import hashlib as _hl\n"
        "from hashlib import sha1, blake2b as b2\n"
        "x = hashlib.md5(b'').hexdigest()\n"
        "class C:\n"
        "    def m(self):\n"
        "        return _hl.sha256(b'')\n"
        "def f():\n"
        "    def inner():\n"
        "        return sha1(b''), b2(b'')\n"
        "    return inner\n"
    ))
    assert [(c.scope, c.algo) for c in calls] == [
        ("<module>", "md5"), ("C.m", "sha256"),
        ("f.inner", "sha1"), ("f.inner", "blake2b"),
    ]


def test_scanner_ignores_strings_and_non_calls(tmp_path):
    calls = _scan(tmp_path, (
        "import hashlib\n"
        "SCRIPT = 'import hashlib as _hl\\n_hl.md5(b\"\")'\n"
        "ALGOS = hashlib.algorithms_available\n"
        "h = None\n"
        "def f(h):\n"
        "    h.update(b'')\n"
    ))
    assert calls == []


def test_scanner_refuses_to_skip_an_unparseable_file(tmp_path):
    with pytest.raises(SyntaxError):
        _scan(tmp_path, "def broken(:\n")


def test_audit_reports_new_stale_miscounted_and_duplicate_rows(tmp_path):
    calls = _scan(tmp_path, (
        "import hashlib\n"
        "def new_one():\n"
        "    return hashlib.sha1(b'')\n"
        "def counted():\n"
        "    return hashlib.md5(b''), hashlib.md5(b'x')\n"
    ))
    inventory = [
        DigestSite("src/mod.py", "counted", 1, "keep", "test"),
        DigestSite("src/mod.py", "gone", 1, "keep", "test"),
        DigestSite("src/other.py", "dup", 1, "keep", "test"),
        DigestSite("src/other.py", "dup", 1, "keep", "test"),
    ]
    problems = "\n".join(audit(calls, inventory))
    assert "src/mod.py new_one: NEW digest (line 3)" in problems
    assert "src/mod.py gone: STALE inventory row" in problems
    assert "src/mod.py counted: 2 digest call(s)" in problems
    assert "src/other.py dup: listed 2 times" in problems


def test_audit_is_silent_when_in_sync(tmp_path):
    calls = _scan(tmp_path, "import hashlib\ndef f():\n    return hashlib.sha1(b'')\n")
    assert audit(calls, [DigestSite("src/mod.py", "f", 1, "keep", "test")]) == []


def test_digest_site_rows_must_be_well_formed():
    with pytest.raises(ValueError, match="retiring"):
        DigestSite("src/a.py", "f", 1, "migrate", "why")
    with pytest.raises(ValueError, match="retiring"):
        DigestSite("src/a.py", "f", 1, "keep", "why", slice="A4")
    with pytest.raises(ValueError, match="why"):
        DigestSite("src/a.py", "f", 1, "keep", "  ")
    with pytest.raises(ValueError, match=">= 1"):
        DigestSite("src/a.py", "f", 0, "keep", "why")
