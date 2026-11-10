"""Step 3.5 correction oracle: authored C# judged by Roslyn, never by Python.

R0 moves the Step 3.5 correction checks off the mutating template binder and
onto the selected-source contract.  A proposed payload is rendered by the
SAME production writer that will ship it, overlaid on the selected baseline,
and compiled by the pinned SDK Roslyn against the real Unity reference
profile (`unity_reference_profile`).  Only errors the proposal INTRODUCES —
present in the candidate compilation and absent from the baseline one — go
back to the Pipeline LLM as a correction.

Read-only by construction: nothing here edits a subsystem, a script, or the
schema.  The proposal is judged; applying it stays the caller's decision.

Unverifiable scope is named, not guessed.  Package namespaces (TMPro,
UnityEngine.UI, UnityEngine.InputSystem) compile per project and are absent
from the Editor reference profile unless `component_api.package_assemblies`
supplies them.  A CS0246/CS0234 inside a file that imports a namespace the
baseline itself could not resolve is reported as unverified rather than as
an authoring error the model would be asked to "fix".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

from .unity_component_api import ComponentApi, render_candidate_subsystem
from .unity_component_check import CandidateCheck, check_candidate

# A correction listing forty diagnostics teaches the model less than six.
MAX_CORRECTIONS = 6

_USING_RE = re.compile(r"^\s*using\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
_UNRESOLVED_TYPE_CODES = frozenset({"CS0246", "CS0234"})


@dataclass
class AuthoredFindings:
    """Introduced errors plus the ones this reference profile cannot judge."""

    introduced: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)

    def corrections(self) -> list[str]:
        """Compiler wording, capped, for the Step 3.5 correction prompt."""
        lines = []
        for diagnostic in self.introduced[:MAX_CORRECTIONS]:
            text = str((diagnostic.get("callsite") or {}).get("line_text") or "").strip()
            suffix = f" — `{text}`" if text else ""
            lines.append(f"{diagnostic['code']}: {diagnostic['message']}{suffix}")
        return lines


def _key(diagnostic: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(diagnostic.get("path") or ""), str(diagnostic.get("code") or ""),
            str(diagnostic.get("message") or ""))


def _unresolved_namespaces(check: CandidateCheck) -> set[str]:
    """Namespaces a `using` directive names that the baseline could not bind."""
    names: set[str] = set()
    for diagnostic in check.errors:
        if diagnostic.get("code") not in _UNRESOLVED_TYPE_CODES:
            continue
        line = str((diagnostic.get("callsite") or {}).get("line_text") or "")
        hit = _USING_RE.match(line)
        if hit:
            names.add(hit.group(1))
    return names


def introduced_findings(baseline: CandidateCheck, candidate: CandidateCheck,
                        paths: Sequence[str]) -> AuthoredFindings:
    """Errors in `paths` the candidate adds over the baseline compilation."""
    remaining = Counter(_key(d) for d in baseline.errors)
    unresolved = _unresolved_namespaces(baseline)
    sources = candidate.sources
    wanted = set(paths)
    findings = AuthoredFindings()
    for diagnostic in candidate.errors:
        if diagnostic.get("path") not in wanted:
            continue
        key = _key(diagnostic)
        if remaining[key] > 0:
            remaining[key] -= 1
            continue
        imports = set(_USING_RE.findall(sources.get(str(diagnostic["path"]), "")))
        if diagnostic.get("code") in _UNRESOLVED_TYPE_CODES and imports & unresolved:
            findings.unverified.append(diagnostic)
        else:
            findings.introduced.append(diagnostic)
    return findings


class AuthoredSourceChecker:
    """Judge one Step 3.5 proposal at a time against the selected project.

    `component_api` is the fragment-free selected baseline the pipeline
    already extracted.  Custom scripts are judged in a compilation that also
    holds that baseline, every file the GameContext emitter writes, and every
    Assets/Scripts file Step 6 will write for the project in its CURRENT
    state.  Those script files are not reconstructed here: they come from the
    generator's own `prepare_scripts` / `stub_known_types` /
    `script_render_context` / `render_project_scripts`, applied to a deep copy
    after the same `inject_boilerplate_scripts` and `drop_owned_scripts` Step 5
    applies, so a call into a
    composed micro, GameManager or a sibling binds exactly as it will in the
    generated project.  A script proposal is landed on that copy by
    `unity_micro_code._apply_script`, the function that applies it for real.

    Not in the compilation: the generator's own runtime files that are not
    schema scripts (SceneSetup, SceneAudio, ModelSwap, the atmosphere and
    vehicle helpers, EntitySpawner) and the MeshBuilder `generate()` appends
    when an entity resolves to a procedural model — those need asset
    resolution. A custom script that names one of them is left to the Editor
    gate.
    """

    def __init__(self, *, composer: Any, subsystems: Sequence[Any],
                 component_api: ComponentApi, references: Sequence[Path],
                 schema: Any = None, template_dir: str | Path = "templates/unity"):
        self.composer = composer
        self.subsystems = list(subsystems)
        self.component_api = component_api
        self.references = tuple(references)
        self.schema = schema
        self.template_dir = str(template_dir)
        if getattr(composer, "component_api", None) is None:
            composer.component_api = component_api
        self._baseline_check: CandidateCheck | None = None
        self._units: list[Any] | None = None
        self._context: dict[bool, dict[str, str]] = {}

    # ── composed micros ────────────────────────────────────────────────────
    def _subsystem_baseline(self) -> CandidateCheck:
        if self._baseline_check is None:
            self._baseline_check = check_candidate(self.component_api, {},
                                                   references=self.references)
        return self._baseline_check

    def subsystem(self, sub: Any, payload: Mapping[str, Any]) -> AuthoredFindings:
        """Findings for one subsystem with `payload` merged into its options."""
        index = next(i for i, item in enumerate(self.subsystems) if item is sub)
        candidates = render_candidate_subsystem(self.composer, self.subsystems,
                                                index, payload)
        check = check_candidate(self.component_api, candidates,
                                references=self.references)
        return introduced_findings(self._subsystem_baseline(), check, sorted(candidates))

    # ── custom scripts ─────────────────────────────────────────────────────
    def _plan(self) -> list[Any]:
        if self._units is None:
            self._units = self.composer.plan(self.subsystems)
        return self._units

    def _context_sources(self, has_game_manager: bool) -> dict[str, str]:
        """Every .cs file the GameContext emitter writes (GameContext.cs plus
        companions such as StatModifier.cs that it references)."""
        if has_game_manager not in self._context:
            with tempfile.TemporaryDirectory(prefix="authored-check-context-") as tmp:
                root = Path(tmp)
                self.composer._emit_context(self._plan(), root, has_game_manager, None)
                scripts = root / "context" / "Scripts"
                self._context[has_game_manager] = {
                    f"context/{path.name}": path.read_text(encoding="utf-8")
                    for path in sorted(scripts.glob("*.cs"))}
        return self._context[has_game_manager]

    def _shipped_scripts(self, schema: Any) -> Any:
        """A deep copy of `schema` holding only the scripts Step 6 will ship:
        Step 5's boilerplate injection (idempotent, so a no-op once Step 5 has
        run) and then the same `drop_owned_scripts` compose applies."""
        from .unity_compose import drop_owned_scripts
        from .unity_pipeline import inject_boilerplate_scripts
        project = schema.model_copy(deep=True)
        inject_boilerplate_scripts(project)
        project.scripts = drop_owned_scripts(project.scripts, self._plan())
        return project

    def _project_sources(self, schema: Any) -> dict[str, str]:
        """Assets/Scripts and the context files exactly as Step 6 writes them
        for `schema`, keyed by logical path. `schema` is never mutated."""
        from .unity_generator import (UnityProjectGenerator, prepare_scripts,
                                      script_render_context, stub_known_types)
        units = self._plan()
        project = self._shipped_scripts(schema)
        known = stub_known_types(project, units)
        prepare_scripts(project)
        rendered = UnityProjectGenerator(self.template_dir).render_project_scripts(
            project, script_render_context(project, units), known)
        has_game_manager = any(getattr(s, "type", "") == "game_manager"
                               for s in project.scripts)
        sources = dict(self._context_sources(has_game_manager))
        sources.update({f"scripts/{Path(path).name}": source
                        for path, source in rendered.items()})
        return sources

    def shipped_custom_paths(self) -> list[str]:
        """Logical paths of the custom scripts Step 6 will ship."""
        if self.schema is None:
            return []
        project = self._shipped_scripts(self.schema)
        return sorted(f"scripts/{s.name}.cs" for s in project.scripts
                      if getattr(s, "type", "") == "custom")

    def script(self, script: Any, payload: Mapping[str, Any]) -> AuthoredFindings:
        """Findings for one custom script with `payload` applied to a copy."""
        from .unity_micro_code import _apply_script
        if self.schema is None:
            raise ValueError("[authored-check] script checks need the project schema")
        scripts = list(self.schema.scripts)
        index = next((i for i, item in enumerate(scripts) if item is script),
                     next((i for i, item in enumerate(scripts)
                           if getattr(item, "name", None) == script.name), None))
        if index is None:
            raise ValueError(f"[authored-check] script {script.name!r} is not in "
                             "the project schema")
        # Script sources are NEW logical paths overlaid on the selected micro
        # baseline, so both compilations see the same composed classes.
        base_check = check_candidate(self.component_api,
                                     self._project_sources(self.schema),
                                     references=self.references)
        trial = self.schema.model_copy(deep=True)
        _apply_script(trial.scripts[index], dict(payload), report=_silent)
        check = check_candidate(self.component_api, self._project_sources(trial),
                                references=self.references)
        return introduced_findings(base_check, check, [f"scripts/{script.name}.cs"])

    def project_scripts(self, authored_micro_sources: Mapping[str, str]) -> CandidateCheck:
        """Compile every Assets/Scripts file as it will ship, beside the
        AUTHORED micros, so a script calling an authored public method binds
        exactly as in the generated project. The structured diagnostics that
        replaced the generator's collaborator erasers."""
        if self.schema is None:
            raise ValueError("[authored-check] script checks need the project schema")
        overlay = {path: source for path, source in authored_micro_sources.items()
                   if self.component_api.sources.get(path) != source}
        overlay.update(self._project_sources(self.schema))
        return check_candidate(self.component_api, overlay, references=self.references)


def _silent(_message: str) -> None:
    """`_apply_script` report sink for a proposal that is judged, not applied."""


def resolve_references(config: Mapping[str, Any] | None) -> tuple[tuple[Path, ...], dict]:
    """`(paths, evidence)` for the configured Unity reference profile.

    An unavailable profile yields no paths and evidence naming the remedy; the
    caller decides what abstaining means at its own stage.
    """
    from .unity_reference_profile import ReferenceProfileUnavailable, unity_reference_profile
    try:
        profile = unity_reference_profile(config)
    except ReferenceProfileUnavailable as exc:
        return (), {"status": "unavailable", "error": str(exc)}
    return profile.paths, {"status": "resolved", **profile.describe()}


def final_component_record(composer: Any, subsystems: Sequence[Any],
                           config: Mapping[str, Any] | None,
                           schema: Any = None) -> tuple[dict, list[str]]:
    """The `schema.meta["component_api_final"]` record plus loud warnings.

    Shared by the pipeline and `unity-validate` replay so both prove the final
    selected declarations, and judge the final authored source, identically.
    Never raises: an unavailable SDK or profile is recorded, not hidden.
    """
    from .unity_component_api import check_authored_components, extract_components
    warnings: list[str] = []
    references, profile = resolve_references(config)
    if not references:
        warnings.append(f"{profile['error']} — final authored source is checked "
                        "SDK-only, so every Unity name stays an unverified error")
    try:
        final_component_api = extract_components(composer, subsystems)
        # HUD seams in plan()/emit() read setter availability from this.
        composer.component_api = final_component_api
        authored_check = check_authored_components(composer, subsystems,
                                                   references=references)
    except Exception as exc:  # noqa: BLE001 - record an unavailable required gate
        warnings.append("final selected component contract unavailable "
                        f"({type(exc).__name__}: {exc}); this build is not "
                        "component-API accepted")
        return ({"status": "unavailable", "error": f"{type(exc).__name__}: {exc}",
                 "reference_profile": profile}, warnings)
    scope = ("SDK binding with Unity reference profile; Unity Editor execution unverified"
             if references else
             "SDK core runtime only (no Unity references); Unity Editor execution unverified")
    record = {
        "format": final_component_api.document["format"],
        "fingerprint": final_component_api.fingerprint,
        "selection": final_component_api.document["selection"],
        "status": "verified",
        "reference_profile": profile,
        "authored_source_check": {
            "accepted": authored_check.accepted,
            "diagnostics": authored_check.errors,
            "scope": scope,
        },
    }
    if authored_check.errors:
        warnings.append("final authored component source has "
                        f"{len(authored_check.errors)} SDK diagnostic(s); "
                        "source is preserved for the Unity Editor gate")
    customs = [s for s in (getattr(schema, "scripts", None) or [])
               if getattr(s, "type", "") == "custom"]
    if customs and references:
        try:
            from .unity_component_api import render_authored_sources
            authored_micros, _selection = render_authored_sources(composer, subsystems)
            checker = AuthoredSourceChecker(composer=composer, subsystems=subsystems,
                                            component_api=final_component_api,
                                            references=references, schema=schema)
            script_check = checker.project_scripts(authored_micros)
            # Only the custom scripts' OWN files: template-rendered scripts and
            # the context files are in the compilation so references bind, but
            # their diagnostics are not authored-source findings.
            custom_paths = set(checker.shipped_custom_paths())
            errors = [d for d in script_check.errors
                      if str(d.get("path", "")) in custom_paths]
            record["script_source_check"] = {
                "accepted": not errors, "diagnostics": errors, "scope": scope}
            if errors:
                warnings.append(f"custom script source has {len(errors)} SDK "
                                "diagnostic(s); source is preserved for the Unity "
                                "Editor gate")
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            record["script_source_check"] = {
                "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
            warnings.append(f"custom script source check unavailable ({type(exc).__name__}: {exc})")
    elif customs:
        record["script_source_check"] = {
            "status": "unavailable", "error": profile.get("error", "no Unity references")}
    return record, warnings
