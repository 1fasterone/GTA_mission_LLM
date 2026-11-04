"""S0 support inventory: inspect real registries, never infer runtime acceptance.

Run ``python -m src.atoms.support_audit --check`` in CI. ``--write`` refreshes
only the derived inventory; it cannot approve a new missing implementation.
The small reviewed baseline records legacy debt, not another implementation map.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import ALL_ATOMS
from .backends import _ENGINE_SNIPPETS, gta5, unity
from .base import AtomDef
from .behavior_map import GTA5_MICROGEN_ATOM_MAP, MICROGEN_ATOM_MAP

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "lookups/atoms/support_baseline.json"
INVENTORY = ROOT / "docs/atom_backend_inventory.json"
EMISSION_CASES = ROOT / "tests/fixtures/atom_support_emission_cases.json"
BACKENDS = {"gta5": gta5, "unity": unity}

# These describe existing call paths, NOT allowed execution phases for new
# operations. S4 must add a real host/phase binding before migrating gameplay.
CALL_PATHS = {
    "unity": [
        {"caller": "src/unity_generator.py:UnityProjectGenerator.generate",
         "template": "templates/unity/GameSequencer.cs.j2",
         "host": "GameManager", "phase": "Start coroutine, once", "input": "schema.atoms"},
        {"caller": "src/unity_generator.py:UnityProjectGenerator.generate",
         "template": "templates/unity/EntityBehavior.cs.j2",
         "host": "EntitySpawner.AddComponent", "phase": "Start coroutine, may restart",
         "input": "entity.atoms"},
        {"caller": "src/unity_compose.py:MicroComposer._emit_wiring",
         "template": "templates/unity/compose/GameWiring.cs.j2",
         "host": "GameWiring", "phase": "Awake subscriptions, Update bridges and event callbacks",
         "input": "compiler-selected wiring atoms only"},
    ],
    "gta5": [
        {"caller": "src/micro/gta5_base.py:GTA5MicroPipeline._after_write",
         "host": "standalone Script", "phase": "Tick, once behind _ran; optional yielding loop",
         "input": "GTA5_MICROGEN_ATOM_MAP (companion, not primary micro behavior)"},
    ],
}


def _source(obj: Any) -> str:
    path = inspect.getsourcefile(obj)
    return Path(path).resolve().relative_to(ROOT).as_posix() if path else ""


def duplicate_dict_keys(source: str) -> list[str]:
    """Catch overwritten literal keys before Python's dict loses the evidence."""
    errors = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [key.value for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)]
        for key, count in Counter(keys).items():
            if count > 1:
                errors.append(f"duplicate dictionary key {key!r} at line {node.lineno}")
    return sorted(errors)


def identity_errors(atoms: list[AtomDef]) -> list[str]:
    errors: list[str] = []
    owners: dict[str, str] = {}
    for name, count in Counter(atom.name for atom in atoms).items():
        if count > 1:
            errors.append(f"duplicate canonical atom {name}")
    for atom in atoms:
        for token in [atom.name, *atom.aliases]:
            key = token.lower()
            if key in owners and owners[key] != atom.name:
                errors.append(f"identity {token!r} belongs to both {owners[key]} and {atom.name}")
            owners[key] = atom.name
        if not atom.engines or set(atom.engines) - BACKENDS.keys():
            errors.append(f"{atom.name}: invalid backend declaration {atom.engines}")
        if len(set(atom.engines)) != len(atom.engines):
            errors.append(f"{atom.name}: duplicate backend declaration")
    return sorted(set(errors))


def _real_statements(snippet: str) -> bool:
    # Emission smoke only. This is not a C# parser, API extractor or compiler.
    uncommented = re.sub(r"/\*.*?\*/|//[^\n]*", "", snippet, flags=re.S)
    return bool(uncommented.strip()) and "TODO" not in snippet


def _definition_locations() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src/atoms").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AtomDef":
                for keyword in node.keywords:
                    if (keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)):
                        result.setdefault(keyword.value.value, []).append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return result


def _reference_inventory() -> list[dict[str, Any]]:
    # Python AST, not prompt strings or a guessed C# parser. Dynamic references
    # remain a declared limit; actual execution requires per-build S4 bindings.
    names = {atom.name for atom in ALL_ATOMS}
    aliases = {alias for atom in ALL_ATOMS for alias in atom.aliases}
    internal = set().union(*(module._SNIPPETS.keys() for module in BACKENDS.values())) - names
    references: dict[str, set[str]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values: list[ast.expr] = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "AtomInvocation":
                    values.extend(key.value for key in node.keywords if key.arg == "atom")
                elif node.func.id in {"unity_atom_snippet", "gta5_atom_snippet"} and node.args:
                    values.append(node.args[0])
            elif isinstance(node, ast.Dict):
                values.extend(value for key, value in zip(node.keys, node.values)
                              if isinstance(key, ast.Constant) and key.value == "atom")
            for value in values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    references.setdefault(value.value, set()).add(
                        f"{path.relative_to(ROOT).as_posix()}:{value.lineno}")
    return [{"atom": name, "sources": sorted(paths),
             "resolution": "registered" if name in names else "alias" if name in aliases
                           else "dispatch_only" if name in internal else "unregistered"}
            for name, paths in sorted(references.items())]


def _micro_inventory() -> list[dict[str, Any]]:
    from ..micro import GTA_REGISTRY, REGISTRY

    result = []
    for backend, registry, mapping in [
        ("unity", REGISTRY, MICROGEN_ATOM_MAP), ("gta5", GTA_REGISTRY, GTA5_MICROGEN_ATOM_MAP),
    ]:
        for kind, pipeline in sorted(registry.items()):
            result.append({
                "backend": backend, "kind": kind,
                "source": _source(pipeline),
                "variants": sorted(getattr(pipeline, "variants", [])),
                "ownership": "asset_pipeline" if kind == "model" else "template_owned",
                "mapped_atoms": [item["atom"] for item in mapping.get(kind, [])],
                "mapping_status": "descriptive; companion emission does not transfer ownership",
                "companion_emission": ("suppressed by camera override" if kind == "camera" else
                                       "standalone _after_write" if kind in mapping else "none"),
                "companion_attachment": "unverified" if backend == "unity" else "standalone Script",
                "composed_companion": "not emitted by MicroComposer.write_files" if backend == "unity"
                                      else "not evidence of minigame inclusion",
                "runtime_acceptance": "unverified",
            })
    return result


def build_inventory() -> dict[str, Any]:
    """Inspect live definitions and dispatchers, with deterministic ordering."""
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if set(_ENGINE_SNIPPETS) != set(BACKENDS):
        errors.append("backend dispatcher and audit backend inventory differ")
    definitions = _definition_locations()
    for relative in ["src/atoms/__init__.py", "src/atoms/backends/__init__.py",
                     "src/atoms/behavior_map.py", "src/micro/__init__.py", "src/micro/gta5/__init__.py"]:
        for error in duplicate_dict_keys((ROOT / relative).read_text(encoding="utf-8")):
            errors.append(f"{relative}: {error}")
    cases = json.loads(EMISSION_CASES.read_text(encoding="utf-8"))
    names = {atom.name for atom in ALL_ATOMS}
    for backend, module in sorted(BACKENDS.items()):
        table = module._SNIPPETS
        assert module.__file__ is not None
        for error in duplicate_dict_keys(Path(module.__file__).read_text(encoding="utf-8")):
            errors.append(f"{backend}: {error}")
        for atom in sorted(ALL_ATOMS, key=lambda item: item.name):
            handler = table.get(atom.name)
            advertised = backend in atom.engines
            status = "unsupported" if not advertised else "descriptive" if handler is None else "executable"
            smoke = "not_applicable"
            if handler is not None:
                try:
                    args = {param.name: param.default for param in atom.params}
                    args.update(cases.get(f"{backend}:{atom.name}", {}))
                    first, second = handler(dict(args)), handler(dict(args))
                    smoke = "passed" if _real_statements(first) and first == second else "failed"
                except Exception as exc:
                    smoke = f"failed: {type(exc).__name__}: {exc}"
                if not advertised:
                    errors.append(f"{backend}:{atom.name}: handler exists for unsupported backend")
                if smoke != "passed":
                    errors.append(f"{backend}:{atom.name}: emission smoke {smoke}")
            rows.append({
                "backend": backend, "atom": atom.name, "category": atom.category,
                "aliases": sorted(atom.aliases), "advertised": advertised, "classification": status,
                "definitions": definitions.get(atom.name, []),
                "structural_fixture": "tests/test_atom_support_audit.py",
                "emission_fixture": "tests/fixtures/atom_support_emission_cases.json"
                                    if f"{backend}:{atom.name}" in cases else "AtomDef defaults",
                "handler": f"{_source(handler)}:{handler.__name__}" if callable(handler) else None,
                "evidence": "synthetic bound emission fixture" if f"{backend}:{atom.name}" in cases
                            else "deterministic default-argument emission only" if handler else "no lowerer",
                "emission_smoke": smoke, "runtime_acceptance": "unverified",
            })
        for name in sorted(set(table) - names):
            handler = table[name]
            rows.append({"backend": backend, "atom": name, "classification": "internal",
                         "advertised": False, "aliases": [],
                         "handler": f"{_source(handler)}:{handler.__name__}",
                         "evidence": "dispatch-only; no AtomDef; not a public atom",
                         "runtime_acceptance": "unverified"})
    summary = {backend: dict(sorted(Counter(row["classification"] for row in rows
                                          if row["backend"] == backend).items()))
               for backend in sorted(BACKENDS)}
    return {"format": "atom-backend-support/v1", "evidence_level": "structural_and_emission_only",
            "runtime_acceptance": "unverified", "summary": summary, "atoms": rows,
            "call_paths": CALL_PATHS, "micros": _micro_inventory(), "errors": sorted(errors),
            "identity_conflicts": identity_errors(ALL_ATOMS), "references": _reference_inventory(),
            "reference_limit": "static Python atom invocations/dicts/direct emitters; dynamic bindings unverified"}


def validate_inventory(inventory: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """New/stale debt, unsupported coverage claims and invalid ownership fail."""
    errors = list(inventory["errors"])
    if inventory["runtime_acceptance"] != "unverified":
        errors.append("S0 has no compilation/runtime evidence; acceptance must remain unverified")
    for row in inventory["atoms"]:
        if row["runtime_acceptance"] != "unverified":
            errors.append(f'{row["atom"]}: unsupported runtime coverage claim')
        if row["classification"] != "internal" and len(row["definitions"]) != 1:
            errors.append(f'{row["atom"]}: missing or duplicate definition source')
    if baseline.get("format") != "atom-support-debt/v1":
        errors.append("unsupported debt baseline format")
    for status, section in [("descriptive", "missing_lowerers"), ("internal", "internal_handlers")]:
        actual = {f'{row["backend"]}:{row["atom"]}' for row in inventory["atoms"]
                  if row["classification"] == status}
        recorded = baseline[section]
        for key in sorted(actual - recorded.keys()):
            errors.append(f"unreviewed {section}: {key}")
        for key in sorted(recorded.keys() - actual):
            errors.append(f"stale {section} baseline (ratchet/update required): {key}")
        for key, entry in recorded.items():
            if not entry.get("reason") or not entry.get("next_slice"):
                errors.append(f"{key}: debt needs a reason and next_slice")
    unknown_references = {row["atom"] for row in inventory["references"] if row["resolution"] == "unregistered"}
    recorded_references = baseline["unregistered_references"]
    for name in sorted(unknown_references - recorded_references.keys()):
        errors.append(f"unreviewed unregistered atom reference: {name}")
    for name in sorted(recorded_references.keys() - unknown_references):
        errors.append(f"stale unregistered reference baseline: {name}")
    for name, entry in recorded_references.items():
        if not entry.get("reason") or not entry.get("next_slice"):
            errors.append(f"{name}: unregistered reference needs a reason and next_slice")
    actual_conflicts = set(inventory["identity_conflicts"])
    recorded_conflicts = baseline["identity_conflicts"]
    for conflict in sorted(actual_conflicts - recorded_conflicts.keys()):
        errors.append(f"unreviewed identity conflict: {conflict}")
    for conflict in sorted(recorded_conflicts.keys() - actual_conflicts):
        errors.append(f"stale identity conflict baseline: {conflict}")
    for conflict, entry in recorded_conflicts.items():
        if not entry.get("reason") or not entry.get("next_slice"):
            errors.append(f"{conflict}: conflict needs a reason and next_slice")
    for paths in inventory["call_paths"].values():
        for path in paths:
            for field in ["caller", "template"]:
                if field in path and not (ROOT / path[field].split(":")[0]).is_file():
                    errors.append(f"missing call-path evidence: {path[field]}")
    for micro in inventory["micros"]:
        if len(set(micro["variants"])) != len(micro["variants"]):
            errors.append(f'{micro["kind"]}: duplicate variant registration')
        if micro["runtime_acceptance"] != "unverified":
            errors.append(f'{micro["kind"]}: unsupported runtime coverage claim')
        if micro["ownership"] not in {"template_owned", "asset_pipeline"}:
            errors.append(f'{micro["kind"]}: atom-owned claim lacks migrated runtime evidence')
        if not (ROOT / micro["source"]).is_file():
            errors.append(f'{micro["kind"]}: missing micro source')
    return sorted(set(errors))


def policy_errors(root: Path = ROOT) -> list[str]:
    blocks = []
    errors = []
    for name in ["AGENTS.md", "CLAUDE.md"]:
        source = (root / name).read_text(encoding="utf-8")
        match = re.search(r"<!-- atom-support:start -->\n(.*?)\n<!-- atom-support:end -->", source, re.S)
        if not match:
            errors.append(f"{name}: missing atom support policy")
        else:
            blocks.append(match.group(1))
        if "Atoms are the foundation of all microgens." in source:
            errors.append(f"{name}: unsupported blanket executable coverage claim")
    if len(blocks) == 2 and blocks[0] != blocks[1]:
        errors.append("AGENTS.md and CLAUDE.md atom support policies differ")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="check committed inventory and legacy debt")
    modes.add_argument("--write", action="store_true", help="refresh derived inventory; never approve debt")
    options = parser.parse_args(argv)
    inventory = build_inventory()
    errors = validate_inventory(inventory, json.loads(BASELINE.read_text(encoding="utf-8"))) + policy_errors()
    rendered = json.dumps(inventory, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if options.check and (not INVENTORY.is_file() or INVENTORY.read_text(encoding="utf-8") != rendered):
        errors.append("derived inventory is stale; inspect changes then run --write")
    if errors:
        print("\n".join(errors))
        return 1
    if options.write:
        INVENTORY.write_text(rendered, encoding="utf-8")
    print(json.dumps({"structural_audit": "passed", "runtime_acceptance": "unverified",
                      "summary": inventory["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
