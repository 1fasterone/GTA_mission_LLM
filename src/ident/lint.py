"""
Deterministic AST audit of every `hashlib` digest outside src/ident/.

Phase 0 of docs/SCENE_GRAPH_COMPILER.md makes src/ident/ the one place identity
is derived. The digests that predate it can't all move — some are contracts
(Unity GUIDs, the npm registry's sha1), some seed generated output — so rather
than pretend they don't exist, src/ident/frozen.py inventories each one with a
disposition. This scanner is what keeps that inventory true: the real per-scope
call counts must equal the inventory EXACTLY. A new digest anywhere fails
(route it through src.ident, or add a row that says why not), and so does a
stale row (a migrated site whose inventory line was left behind).

Pure AST, no imports of the scanned modules (same approach as
src/naming_lint.py), so it never triggers heavy optional dependencies. Aliased
imports (`import hashlib as _hl`, `from hashlib import sha1`) are followed.
Code that merely MENTIONS hashlib inside a string — e.g. the script
src/blender/mcp_backend.py emits for Blender's own interpreter — is not a
call in this process and is correctly not counted.

Unlike naming_lint, an unparseable file is an error, not a skip: silently
skipping a file is exactly how a digest would escape the inventory.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.ident.frozen import DigestSite

SCAN_ROOTS = ("src", "cli")
EXCLUDED = ("src/ident/",)   # the substrate itself


@dataclass(frozen=True, order=True)
class DigestCall:
    path: str    # repo-relative, forward slashes
    scope: str   # enclosing qualname ("Class.method"), "<module>" at top level
    line: int
    algo: str    # the hashlib callable used, e.g. "sha1", "new"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: hashlib.{self.algo}() in {self.scope}"


def _aliases(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Names bound to the hashlib module, and names bound to its callables."""
    modules: set[str] = set()
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "hashlib":
                    modules.add(a.asname or "hashlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "hashlib":
            for a in node.names:
                names[a.asname or a.name] = a.name
    return modules, names


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, modules: set[str], names: dict[str, str]):
        self.path = path
        self.modules = modules
        self.names = names
        self.stack: list[str] = []
        self.calls: list[DigestCall] = []

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        algo = None
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in self.modules:
            algo = f.attr
        elif isinstance(f, ast.Name) and f.id in self.names:
            algo = self.names[f.id]
        if algo is not None:
            scope = ".".join(self.stack) or "<module>"
            self.calls.append(DigestCall(self.path, scope, node.lineno, algo))
        self.generic_visit(node)


def scan_file(path: Path, rel: str) -> list[DigestCall]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules, names = _aliases(tree)
    if not modules and not names:
        return []
    scanner = _Scanner(rel, modules, names)
    scanner.visit(tree)
    return scanner.calls


def scan_tree(repo_root: Path) -> list[DigestCall]:
    """Every digest call under SCAN_ROOTS minus EXCLUDED, in a stable order."""
    calls: list[DigestCall] = []
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(repo_root).as_posix()
            if "__pycache__" in path.parts or rel.startswith(EXCLUDED):
                continue
            calls.extend(scan_file(path, rel))
    return sorted(calls)


def audit(calls: Iterable[DigestCall], inventory: Iterable[DigestSite]) -> list[str]:
    """Every disagreement between the real digest calls and the inventory,
    one human-readable line each, in a stable order. Empty = in sync."""
    calls = list(calls)
    inventory = list(inventory)
    problems: list[str] = []

    rows = Counter((s.path, s.scope) for s in inventory)
    for key, n in sorted(rows.items()):
        if n > 1:
            problems.append(f"{key[0]} {key[1]}: listed {n} times in src/ident/frozen.py")

    actual = Counter((c.path, c.scope) for c in calls)
    expected = {(s.path, s.scope): s.calls for s in inventory}
    for key in sorted(actual.keys() | expected.keys()):
        got, want = actual.get(key, 0), expected.get(key, 0)
        if got == want:
            continue
        path, scope = key
        where = ", ".join(str(c.line) for c in calls if (c.path, c.scope) == key)
        if want == 0:
            problems.append(
                f"{path} {scope}: NEW digest (line {where}) — derive identity with "
                "src.ident.chash()/fhash(), or add a DigestSite row to "
                "src/ident/frozen.py saying why it must stay")
        elif got == 0:
            problems.append(
                f"{path} {scope}: STALE inventory row — no hashlib call there any "
                "more; delete the row from src/ident/frozen.py")
        else:
            problems.append(
                f"{path} {scope}: {got} digest call(s) (line {where}) but the "
                f"inventory lists {want} — update the row or route the new one "
                "through src.ident")
    return problems
