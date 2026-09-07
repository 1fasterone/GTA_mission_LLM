"""Deterministic AST audit for `kind` vs `type` field-naming drift.

Both names are canonical in this codebase and mean DIFFERENT things:
  - `kind`  -> asset/config CATEGORY (EntitySpec.kind: prop/vehicle/character,
              MicrogensSpec.kind: which microgen subsystem,
              MinigameSetting.kind: int/float/bool value type)
  - `type`  -> behavior/action/variant CLASSIFICATION (MissionObjective.type:
              reach/escort, BTNode.type: action/condition, AudioSpec.type:
              sfx/music, UnityScript.type / MissionStep.type / MissionAudio.type)

No class in the schema/resolver/generator layers defines both today — see
docs/NAMING_KIND_VS_TYPE.md for the full audit. A class that starts declaring
BOTH `kind` and `type` as its own fields is exactly the drift CLAUDE.md's
"never invent new schema fields... keep atom sequences valid" discipline
guards against: two names doing one job on the same model, one of them
almost certainly redundant or a copy-paste of the other's semantics. This
scanner is a Python-AST audit (no imports of the target modules, so it
never triggers heavy optional dependencies) — flag, don't guess a rename.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_WATCHED_FIELDS = {"kind", "type"}


@dataclass(frozen=True)
class Collision:
    file: str
    class_name: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: class {self.class_name} declares both 'kind' and 'type'"


def _class_field_names(node: ast.ClassDef) -> dict:
    """Top-level annotated field assignments directly in this class body
    (AnnAssign, e.g. Pydantic/dataclass field declarations) — never fields
    from a nested class or a method body, which are a different namespace."""
    fields: dict = {}
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name in _WATCHED_FIELDS and name not in fields:
                fields[name] = stmt.lineno
    return fields


def scan_file(path: Path) -> list[Collision]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[Collision] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields = _class_field_names(node)
            if _WATCHED_FIELDS <= fields.keys():
                out.append(Collision(file=str(path), class_name=node.name,
                                     line=min(fields.values())))
    return out


def scan_paths(paths: list[Path]) -> list[Collision]:
    """Deterministic order: by file path, then by line number."""
    out: list[Collision] = []
    for path in paths:
        out.extend(scan_file(path))
    return sorted(out, key=lambda c: (c.file, c.line))


def default_scan_roots(repo_root: Path) -> list[Path]:
    """Every .py file under src/ and cli/ — the schema/resolver/generator
    layers for both backends (GTA5 + Unity) that this audit protects."""
    roots = [repo_root / "src", repo_root / "cli"]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files
