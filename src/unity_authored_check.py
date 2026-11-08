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
    holds that baseline, the rendered GameContext locator, and every sibling
    script in its CURRENT state, so a call into a composed micro or a sibling
    binds exactly as it will in the generated project.
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
        self._context_source: str | None = None

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
    def _gamecontext(self) -> str:
        if self._context_source is None:
            units = self.composer.plan(self.subsystems)
            has_game_manager = any(getattr(s, "type", "") == "game_manager"
                                   for s in getattr(self.schema, "scripts", []) or [])
            with tempfile.TemporaryDirectory(prefix="authored-check-context-") as tmp:
                root = Path(tmp)
                self.composer._emit_context(units, root, has_game_manager, None)
                self._context_source = (root / "context" / "Scripts" / "GameContext.cs"
                                        ).read_text(encoding="utf-8")
        return self._context_source

    def _render_script(self, script: Any) -> str:
        """Render exactly what Step 6 will: the same lint, then the template."""
        from .micro.code_fields import lint_extensible
        from .unity_generator import UnityProjectGenerator
        script = script.model_copy(deep=True)
        lint_extensible(script, reserved_fields=set(), template_usings={"UnityEngine"},
                        hoist_field_inits=True)
        generator = UnityProjectGenerator(self.template_dir)
        known = frozenset(getattr(s, "name", "") for s in self.schema.scripts) | frozenset(
            row.get("class_name", "") for row in self.component_api.document.get("selection", []))
        content, _path = generator._generate_script(
            self.schema, script,
            {"render_pipeline": getattr(self.schema, "render_pipeline", "URP"),
             "climbing_contract": False},
            known)
        return content

    def _script_sources(self, override: Any = None) -> dict[str, str]:
        sources: dict[str, str] = {"scripts/GameContext.cs": self._gamecontext()}
        for script in self.schema.scripts:
            if getattr(script, "type", "") != "custom":
                continue
            current = override if override is not None and override.name == script.name else script
            sources[f"scripts/{script.name}.cs"] = self._render_script(current)
        return sources

    def script(self, script: Any, payload: Mapping[str, Any]) -> AuthoredFindings:
        """Findings for one custom script with `payload` applied to a copy."""
        if self.schema is None:
            raise ValueError("[authored-check] script checks need the project schema")
        # Script sources are NEW logical paths overlaid on the selected micro
        # baseline, so both compilations see the same composed classes.
        base_check = check_candidate(self.component_api, self._script_sources(),
                                     references=self.references)
        proposed = type(script).model_validate({**script.model_dump(), **{
            key: value for key, value in payload.items() if key in type(script).model_fields}})
        check = check_candidate(self.component_api, self._script_sources(proposed),
                                references=self.references)
        return introduced_findings(base_check, check, [f"scripts/{script.name}.cs"])

    def project_scripts(self, authored_micro_sources: Mapping[str, str]) -> CandidateCheck:
        """Compile every custom script as it will ship, beside the AUTHORED
        micros, so a script calling an authored public method binds exactly as
        in the generated project. The structured diagnostics that replaced the
        generator's collaborator erasers."""
        if self.schema is None:
            raise ValueError("[authored-check] script checks need the project schema")
        overlay = {path: source for path, source in authored_micro_sources.items()
                   if self.component_api.sources.get(path) != source}
        overlay.update(self._script_sources())
        return check_candidate(self.component_api, overlay, references=self.references)


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
            errors = [d for d in script_check.errors
                      if str(d.get("path", "")).startswith("scripts/")]
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
