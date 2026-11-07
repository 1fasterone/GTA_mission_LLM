"""S3 mechanic descriptor connection and ownership closure audit.

This audit is structural only.  It traces existing descriptor producers to the
deterministic code that consumes them, then classifies a representative set of
runtime routes without changing schemas, atom vocabulary, generated gameplay,
or template ownership.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .backends import unity
from .behavior_map import MICROGEN_ATOM_MAP, expand_behavior
from .support_audit import BASELINE

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs/mechanic_ownership_inventory.json"

CONNECTED_AND_OWNED = "connected_and_owned"
CONNECTED_BUT_NOT_ATTACHED = "connected_but_not_attached"
AMBIGUOUS_OWNER = "ambiguous_owner"
MISSING_PROVIDER = "missing_provider"
CONNECTION_STATES = frozenset({
    CONNECTED_AND_OWNED,
    CONNECTED_BUT_NOT_ATTACHED,
    AMBIGUOUS_OWNER,
    MISSING_PROVIDER,
})


class OwnershipEvidenceError(ValueError):
    """Raised when source evidence cannot identify even a candidate owner."""


def _source_ref(obj: Any) -> str:
    path = inspect.getsourcefile(obj)
    if path is None:
        raise OwnershipEvidenceError(f"no source file for {obj!r}")
    _, line = inspect.getsourcelines(obj)
    relative = Path(path).resolve().relative_to(ROOT).as_posix()
    return f"{relative}:{line}"


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _assignment_ref(relative: str, name: str) -> str:
    for node in ast.walk(_tree(relative)):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return f"{relative}:{node.lineno}"
    raise OwnershipEvidenceError(f"missing assignment {relative}:{name}")


def _class_field_ref(relative: str, class_name: str, field_name: str) -> str:
    for node in _tree(relative).body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            if any(isinstance(item, ast.Name) and item.id == field_name for item in targets):
                return f"{relative}:{statement.lineno}"
    raise OwnershipEvidenceError(f"missing field {relative}:{class_name}.{field_name}")


def _token_ref(relative: str, token: str) -> str:
    for line_number, line in enumerate((ROOT / relative).read_text(encoding="utf-8").splitlines(), 1):
        if token in line:
            return f"{relative}:{line_number}"
    raise OwnershipEvidenceError(f"missing source evidence {relative}:{token}")


def _literal_call_refs(relative: str, call_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.walk(_tree(relative)):
        if not isinstance(node, ast.Call):
            continue
        function_name = ""
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        if function_name != call_name or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            result[first.value] = f"{relative}:{node.lineno}"
    return dict(sorted(result.items()))


def _has_hosted_script_binding(template: str, guid_expression: str) -> bool:
    expected_script = (
        "m_Script: {fileID: 11500000, guid: "
        f"{{{{ {guid_expression} }}}}, type: 3}}"
    )
    for block in template.split("--- !u!114"):
        lines = tuple(line.strip() for line in block.splitlines())
        if "MonoBehaviour:" not in lines or expected_script not in lines:
            continue
        for line in lines:
            prefix = "m_GameObject: {fileID: "
            if line.startswith(prefix) and line.endswith("}"):
                file_id = line.removeprefix(prefix).removesuffix("}").strip()
                if file_id and file_id != "0":
                    return True
    return False


def _scene_component_attachment_ref(
    relative: str,
    guid_expression: str,
) -> str | None:
    template = (ROOT / relative).read_text(encoding="utf-8")
    if not _has_hosted_script_binding(template, guid_expression):
        return None
    token = (
        "m_Script: {fileID: 11500000, guid: "
        f"{{{{ {guid_expression} }}}}, type: 3}}"
    )
    return _token_ref(relative, token)


def _path_from_ref(reference: str) -> Path:
    return ROOT / reference.rsplit(":", 1)[0]


def classify_connection(
    *,
    provider: str | None,
    owner_candidates: Iterable[str],
    lifecycle_seam: str | None,
) -> str:
    """Classify one route from explicit structural evidence.

    A present provider with no candidate owner is not silently converted into a
    missing-provider result: the route is unknown and the audit must stop.
    """
    candidates = tuple(sorted(set(owner_candidates)))
    if provider is None:
        return MISSING_PROVIDER
    if not candidates:
        raise OwnershipEvidenceError(
            "provider exists but owner route is unknown; candidates=[]; "
            f"missing_seam={lifecycle_seam or '<none>'}"
        )
    if len(candidates) > 1:
        return AMBIGUOUS_OWNER
    if lifecycle_seam is None:
        return CONNECTED_BUT_NOT_ATTACHED
    return CONNECTED_AND_OWNED


def _connection(
    *,
    descriptor: str,
    mode: str,
    selection: str,
    provider: str | None,
    owner_candidates: Iterable[str],
    generated_host: str | None,
    lifecycle_seam: str | None,
    missing_seam: str | None,
    provenance: Iterable[str],
    next_slice: str | None = None,
    template_owner_preserved: str | None = None,
) -> dict[str, Any]:
    candidates = sorted(set(owner_candidates))
    classification = classify_connection(
        provider=provider,
        owner_candidates=candidates,
        lifecycle_seam=lifecycle_seam,
    )
    if classification == CONNECTED_AND_OWNED:
        diagnostic = (
            f"connected and owned by {candidates[0]} through {lifecycle_seam}"
        )
    elif classification == CONNECTED_BUT_NOT_ATTACHED:
        diagnostic = (
            f"provider emits {candidates[0]}, but no generated attachment is proven; "
            f"missing seam: {missing_seam or '<unspecified>'}"
        )
    elif classification == AMBIGUOUS_OWNER:
        diagnostic = (
            "ambiguous owner candidates: " + ", ".join(candidates)
            + f"; missing seam: {missing_seam or lifecycle_seam or '<unspecified>'}"
        )
    else:
        diagnostic = (
            f"missing provider for {descriptor}; later migration: "
            f"{next_slice or '<unspecified>'}"
        )
    return {
        "descriptor": descriptor,
        "mode": mode,
        "selection": selection,
        "provider": provider,
        "owner_candidates": candidates,
        "generated_host": generated_host,
        "lifecycle_seam": lifecycle_seam,
        "missing_seam": missing_seam,
        "classification": classification,
        "diagnostic": diagnostic,
        "next_slice": next_slice,
        "template_owner_preserved": template_owner_preserved,
        "provenance": sorted(set(provenance)),
        "runtime_acceptance": "unverified",
    }


def _descriptor_producers() -> list[dict[str, Any]]:
    from ..capability_resolver import resolve_capabilities, resolve_microgens
    from ..micro.gta5_base import GTA5MicroPipeline
    from ..micro.unity_base import UnityMicroPipeline
    from ..unity_compose import MicroComposer, Subsystem
    from ..unity_generator import UnityProjectGenerator
    from ..unity_resolver import UnityResolver

    wiring_calls = _literal_call_refs("src/unity_compose.py", "unity_atom_snippet")
    return [
        {
            "producer": "UnitySchema.atoms",
            "source": _class_field_ref("src/unity_schema.py", "UnitySchema", "atoms"),
            "consumers": [
                _source_ref(resolve_capabilities),
                _source_ref(resolve_microgens),
                _source_ref(UnityProjectGenerator.generate),
            ],
        },
        {
            "producer": "MicrogensSpec.atoms",
            "source": _class_field_ref("src/unity_schema.py", "MicrogensSpec", "atoms"),
            "consumers": [_source_ref(resolve_microgens), _source_ref(UnityProjectGenerator.generate)],
        },
        {
            "producer": "EntitySpec.atoms and behavior map",
            "source": _class_field_ref("src/unity_schema.py", "EntitySpec", "atoms"),
            "descriptor_source": _assignment_ref("src/atoms/behavior_map.py", "_BEHAVIOR_MAP"),
            "consumers": [_source_ref(UnityResolver.expand_behaviors), _source_ref(UnityProjectGenerator.generate)],
        },
        {
            "producer": "UnityScript.atoms",
            "source": _class_field_ref("src/unity_schema.py", "UnityScript", "atoms"),
            "consumers": [_source_ref(UnityResolver.expand_behaviors), _source_ref(UnityProjectGenerator.generate)],
        },
        {
            "producer": "Unity micro default atom descriptors",
            "source": _assignment_ref("src/atoms/behavior_map.py", "MICROGEN_ATOM_MAP"),
            "consumers": [_source_ref(UnityMicroPipeline._after_write)],
        },
        {
            "producer": "GTA5 micro default atom descriptors",
            "source": _assignment_ref("src/atoms/behavior_map.py", "GTA5_MICROGEN_ATOM_MAP"),
            "consumers": [_source_ref(GTA5MicroPipeline._after_write)],
        },
        {
            "producer": "Unity wiring atom calls",
            "source": _source_ref(MicroComposer._emit_wiring),
            "descriptors": sorted(wiring_calls),
            "consumers": [_token_ref("templates/unity/compose/GameWiring.cs.j2", "private void Awake()")],
        },
        {
            "producer": "Unity composed subsystem descriptors",
            "source": _source_ref(Subsystem),
            "consumers": [
                _source_ref(MicroComposer._resolve),
                _source_ref(MicroComposer.compose),
                _token_ref("templates/unity/scene.unity.j2", "Composed micros, attached per target"),
            ],
        },
    ]


def _representative_connections(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    from ..micro import REGISTRY
    from ..micro.controller import ControllerPipeline
    from ..micro.unity_base import UnityMicroPipeline
    from ..unity_compose import MicroComposer
    from ..unity_generator import UnityProjectGenerator
    from ..unity_resolver import UnityResolver

    wait_provider = _source_ref(unity._SNIPPETS["Wait"])
    move_provider = _source_ref(unity._SNIPPETS["MoveTo"])
    wiring_provider = _source_ref(unity._SNIPPETS["WireInputToController"])
    sequencer_attachment = _scene_component_attachment_ref(
        "templates/unity/scene.unity.j2",
        "game_sequencer_guid",
    )
    companion_attachment = _scene_component_attachment_ref(
        "templates/unity/scene.unity.j2",
        "controller_default_behavior_guid",
    )
    controller_atoms = {entry["atom"] for entry in MICROGEN_ATOM_MAP["controller"]}
    if "Move" not in controller_atoms:
        raise OwnershipEvidenceError("controller default descriptor no longer contains Move")
    patrol_atoms = {atom.atom for atom in expand_behavior("patrol")}
    if "MoveTo" not in patrol_atoms:
        raise OwnershipEvidenceError("patrol descriptor no longer resolves MoveTo")
    if REGISTRY.get("controller") is not ControllerPipeline:
        raise OwnershipEvidenceError("controller registry no longer selects ControllerPipeline")
    shoot_debt = baseline["missing_lowerers"].get("unity:Shoot", {})

    return [
        _connection(
            descriptor="unity:UnitySchema.atoms:Wait",
            mode="schema_level_atom",
            selection=_class_field_ref("src/unity_schema.py", "UnitySchema", "atoms"),
            provider=wait_provider,
            owner_candidates=["GameSequencer"],
            generated_host="GameSequencer",
            lifecycle_seam=sequencer_attachment,
            missing_seam=None if sequencer_attachment else "scene or prefab component attachment",
            provenance=[
                _source_ref(UnityProjectGenerator.generate),
                _token_ref("templates/unity/GameSequencer.cs.j2", "private IEnumerator Start()"),
            ],
        ),
        _connection(
            descriptor="unity:behavior_map:patrol:MoveTo",
            mode="entity_attachment",
            selection=_source_ref(UnityResolver.expand_behaviors),
            provider=move_provider,
            owner_candidates=["EntityBehavior"],
            generated_host="spawned entity GameObject",
            lifecycle_seam="EntitySpawner.AddComponent -> EntityBehavior.Start coroutine",
            missing_seam=None,
            provenance=[
                _assignment_ref("src/atoms/behavior_map.py", "_BEHAVIOR_MAP"),
                _token_ref("src/unity_resolver.py", "e.behavior = _pascal(e.name) + \"Behavior\""),
                _token_ref("templates/unity/EntitySpawner.cs.j2", "go.AddComponent(behaviorType)"),
                _token_ref("templates/unity/EntityBehavior.cs.j2", "private void Start()"),
            ],
        ),
        _connection(
            descriptor="unity:wiring:WireInputToController",
            mode="unity_wiring_seam",
            selection=_literal_call_refs("src/unity_compose.py", "unity_atom_snippet")[
                "WireInputToController"
            ],
            provider=wiring_provider,
            owner_candidates=["GameWiring"],
            generated_host="_Systems",
            lifecycle_seam="scene component -> GameWiring.Awake subscription",
            missing_seam=None,
            provenance=[
                _source_ref(MicroComposer.plan),
                _token_ref("templates/unity/scene.unity.j2", "set sys_units = composed_units"),
                _token_ref("templates/unity/compose/GameWiring.cs.j2", "private void Awake()"),
            ],
        ),
        _connection(
            descriptor="unity:MICROGEN_ATOM_MAP:controller:Move",
            mode="standalone_companion",
            selection=_assignment_ref("src/atoms/behavior_map.py", "MICROGEN_ATOM_MAP"),
            provider=_source_ref(UnityMicroPipeline._after_write),
            owner_candidates=["ControllerDefaultBehavior"],
            generated_host="ControllerDefaultBehavior",
            lifecycle_seam=companion_attachment,
            missing_seam=(
                None
                if companion_attachment
                else "generated scene component or runtime AddComponent"
            ),
            provenance=[
                _source_ref(UnityMicroPipeline._after_write),
                _source_ref(MicroComposer.compose),
                _token_ref("src/unity_compose.py", "pipeline.write_files(schema, variant, kind_dir)"),
            ],
            template_owner_preserved="ControllerPipeline",
        ),
        _connection(
            descriptor="unity:Subsystem:controller",
            mode="template_owned_mechanic",
            selection=_source_ref(MicroComposer._resolve),
            provider=_source_ref(ControllerPipeline.write_files),
            owner_candidates=["ControllerPipeline"],
            generated_host="Player",
            lifecycle_seam="MicroComposer.plan GUID -> scene player component",
            missing_seam=None,
            provenance=[
                _assignment_ref("src/micro/__init__.py", "REGISTRY"),
                _source_ref(MicroComposer.plan),
                _token_ref("templates/unity/scene.unity.j2", "for u in player_units"),
            ],
            template_owner_preserved="ControllerPipeline",
        ),
        _connection(
            descriptor="unity:AtomDef:Shoot",
            mode="missing_provider",
            selection=_assignment_ref("src/atoms/controller.py", "CONTROLLER_ATOMS"),
            provider=(
                _source_ref(unity._SNIPPETS["Shoot"])
                if "Shoot" in unity._SNIPPETS else None
            ),
            owner_candidates=[],
            generated_host=None,
            lifecycle_seam=None,
            missing_seam="Unity atom lowerer and typed runtime binding",
            provenance=[
                _token_ref("src/atoms/controller.py", 'name="Shoot"'),
                _token_ref("lookups/atoms/support_baseline.json", '"unity:Shoot"'),
            ],
            next_slice=shoot_debt.get("next_slice"),
            template_owner_preserved="controller/projectile templates",
        ),
    ]


def _protected_template_owners() -> list[dict[str, Any]]:
    from ..micro import REGISTRY

    result = []
    for mechanic in ("controller", "board", "rules"):
        pipeline = REGISTRY[mechanic]
        result.append({
            "mechanic": mechanic,
            "ownership": "template_owned",
            "owner": pipeline.__name__,
            "source": _source_ref(pipeline),
            "migrated": False,
            "runtime_acceptance": "unverified",
        })
    result.append({
        "mechanic": "projectile",
        "ownership": "template_owned",
        "owner": "ControllerPipeline controller/projectile templates",
        "source": _token_ref(
            "templates/unity/micro/controller/ProjectileController.cs.j2",
            "class ProjectileController",
        ),
        "attachment": _token_ref(
            "templates/unity/micro/controller/Controller.cs.j2",
            "owner.AddComponent<ProjectilePool>()",
        ),
        "migrated": False,
        "runtime_acceptance": "unverified",
    })
    return result


def build_report() -> dict[str, Any]:
    """Build the deterministic S3 report from live source and registries."""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    representatives = _representative_connections(baseline)
    summary = dict(sorted(Counter(row["classification"] for row in representatives).items()))
    return {
        "format": "mechanic-ownership/v1",
        "evidence_level": "structural_source_only",
        "runtime_acceptance": "unverified",
        "descriptor_producers": _descriptor_producers(),
        "representative_connections": representatives,
        "protected_template_owners": _protected_template_owners(),
        "summary": summary,
        "scope_limit": (
            "Representative S3 structural closure only; no typed S4 binding persistence, "
            "runtime migration, compilation claim, or advancing runtime acceptance."
        ),
    }


def validate_report(report: dict[str, Any]) -> list[str]:
    """Reject unsupported ownership claims and incomplete abstentions."""
    errors: list[str] = []
    if report.get("format") != "mechanic-ownership/v1":
        errors.append("unsupported mechanic ownership report format")
    if report.get("runtime_acceptance") != "unverified":
        errors.append("S3 structural report cannot claim runtime acceptance")

    producers = report.get("descriptor_producers", [])
    producer_names = [row.get("producer") for row in producers]
    if len(producer_names) != len(set(producer_names)):
        errors.append("duplicate mechanic descriptor producer")
    for producer in producers:
        references = [producer.get("source"), producer.get("descriptor_source")]
        references.extend(producer.get("consumers", []))
        for reference in [item for item in references if item]:
            if not _path_from_ref(reference).is_file():
                errors.append(f"missing descriptor call-path evidence: {reference}")

    representatives = report.get("representative_connections", [])
    descriptors = [row.get("descriptor") for row in representatives]
    if len(descriptors) != len(set(descriptors)):
        errors.append("duplicate representative mechanic descriptor")
    required_modes = {
        "schema_level_atom", "entity_attachment", "unity_wiring_seam",
        "standalone_companion", "template_owned_mechanic",
    }
    actual_modes = {row.get("mode") for row in representatives}
    for mode in sorted(required_modes - actual_modes):
        errors.append(f"missing representative ownership mode: {mode}")
    for row in representatives:
        descriptor = row.get("descriptor", "<missing>")
        state = row.get("classification")
        candidates = row.get("owner_candidates", [])
        if state not in CONNECTION_STATES:
            errors.append(f"{descriptor}: unknown ownership classification {state!r}")
            continue
        if row.get("runtime_acceptance") != "unverified":
            errors.append(f"{descriptor}: unsupported runtime acceptance claim")
        for reference in [row.get("selection"), *row.get("provenance", [])]:
            if not reference or not _path_from_ref(reference).is_file():
                errors.append(f"{descriptor}: missing provenance {reference!r}")
        if state == CONNECTED_AND_OWNED:
            if not row.get("provider") or len(candidates) != 1:
                errors.append(f"{descriptor}: connected ownership needs one provider and owner")
            if not row.get("generated_host") or not row.get("lifecycle_seam"):
                errors.append(f"{descriptor}: connected ownership lacks host or lifecycle seam")
        elif state == CONNECTED_BUT_NOT_ATTACHED:
            if not row.get("provider") or len(candidates) != 1:
                errors.append(f"{descriptor}: unattached route needs one emitted owner candidate")
            if row.get("lifecycle_seam") or not row.get("missing_seam"):
                errors.append(f"{descriptor}: emission cannot claim attachment without a seam")
        elif state == AMBIGUOUS_OWNER:
            if not row.get("provider") or len(candidates) < 2:
                errors.append(f"{descriptor}: ambiguous route must name multiple owner candidates")
            for candidate in candidates:
                if candidate not in row.get("diagnostic", ""):
                    errors.append(f"{descriptor}: ambiguity diagnostic omits {candidate}")
            if not row.get("missing_seam"):
                errors.append(f"{descriptor}: ambiguous route must name the missing seam")
        elif state == MISSING_PROVIDER:
            if row.get("provider") is not None:
                errors.append(f"{descriptor}: missing-provider route unexpectedly has a provider")
            if not row.get("next_slice"):
                errors.append(f"{descriptor}: missing provider needs a later migration slice")
        if not row.get("diagnostic"):
            errors.append(f"{descriptor}: missing deterministic diagnostic")

    expected_summary = dict(sorted(Counter(
        row.get("classification") for row in representatives
    ).items()))
    if report.get("summary") != expected_summary:
        errors.append("mechanic ownership summary does not match representative connections")

    protected = report.get("protected_template_owners", [])
    if {row.get("mechanic") for row in protected} != {"controller", "board", "rules", "projectile"}:
        errors.append("protected template-owner set must remain controller, board, rules, projectile")
    for row in protected:
        mechanic = row.get("mechanic", "<missing>")
        if row.get("ownership") != "template_owned" or row.get("migrated") is not False:
            errors.append(f"{mechanic}: S3 cannot replace template ownership")
        if row.get("runtime_acceptance") != "unverified":
            errors.append(f"{mechanic}: S3 cannot claim runtime acceptance")
        if not _path_from_ref(row.get("source", "")).is_file():
            errors.append(f"{mechanic}: missing template-owner source")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="check committed S3 report")
    modes.add_argument("--write", action="store_true", help="refresh the derived S3 report")
    options = parser.parse_args(argv)
    report = build_report()
    errors = validate_report(report)
    rendered = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if options.check and (not REPORT.is_file() or REPORT.read_text(encoding="utf-8") != rendered):
        errors.append("derived mechanic ownership report is stale; inspect changes then run --write")
    if errors:
        print("\n".join(sorted(set(errors))))
        return 1
    if options.write:
        REPORT.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "mechanic_ownership_audit": "passed",
        "runtime_acceptance": "unverified",
        "summary": report["summary"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
