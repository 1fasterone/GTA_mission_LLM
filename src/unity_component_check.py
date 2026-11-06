"""Source-preserving shadow binding prerequisite for the S2 consumer cutover.

Checks one candidate source set against an immutable selected baseline. All Roslyn
errors survive, including missing references. This module neither rewrites schema
nor changes production consumers. SDK success does not establish Unity execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from .ident import fhash
from .unity_component_api import (
    ComponentApi, ComponentApiError, ComponentApiUnavailable, _preserve, _tool,
    canonical_json, digest, extract_sources,
)


@dataclass(frozen=True, init=False)
class CandidateCheck:
    _report_json: str
    _sources_json: str

    def __init__(self, report: Mapping[str, Any], sources: Mapping[str, str]):
        object.__setattr__(self, "_report_json", canonical_json(report))
        object.__setattr__(self, "_sources_json", canonical_json(sources))

    @property
    def report(self) -> dict[str, Any]:
        return json.loads(self._report_json)

    @property
    def sources(self) -> dict[str, str]:
        return json.loads(self._sources_json)

    @property
    def accepted(self) -> bool:
        return self.report["accepted"]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [d for d in self.report["diagnostics"] if d["severity"] == "error"]


def _logical(path: str) -> None:
    # Source names are portable logical paths, never absolute host locations.
    logical = PurePosixPath(path)
    if (not path or "\\" in path or ":" in path or logical.is_absolute()
            or ".." in logical.parts or logical.as_posix() != path):
        raise ValueError(f"[component-api] invalid logical source path: {path!r}")


def check_candidate(baseline: ComponentApi, candidates: Mapping[str, str], *,
                    references: Sequence[Path] = (), output_dir: Path | None = None) -> CandidateCheck:
    """Overlay one proposal on the existing baseline, without approving it.

    Replacement keys refer to exact baseline logical paths. New keys may add a
    custom source; duplicate type declarations remain compiler errors. Callers
    validate proposals independently, never union all unapproved collaborators.
    Only approve_candidate can produce the refreshed contract after a clean check.
    """
    if output_dir is not None and output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("[component-api] evidence directory is not empty; use a new attempt directory")
    original = baseline.sources
    sources = {**original, **dict(candidates)}
    for path in sources:
        _logical(path)
    sources = dict(sorted(sources.items()))
    identity: dict[str, Any] = {
        "format": "unity-component-check/v1", "baseline": baseline.fingerprint,
        "candidate_paths": sorted(candidates), "defines": baseline.document["defines"],
        "source_identity": digest(sources),
        "accepted": False,
    }
    try:
        supplied = sorted({Path(p).resolve() for p in references}, key=str)
        identity["references"] = [{"name": p.name, "content_hash": fhash(p)} for p in supplied]
        binary, toolchain = _tool(sources)
        identity["toolchain"] = toolchain
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            raise ComponentApiUnavailable("pinned .NET SDK disappeared before check", sources)
        request = {
            "action": "check", "sources": [{"path": p, "text": s} for p, s in sources.items()],
            "defines": identity["defines"], "references": [str(p) for p in supplied],
        }
        process = subprocess.run([dotnet, str(binary)], input=canonical_json(request),
                                 capture_output=True, text=True, timeout=300)
        if process.returncode not in (0, 1):
            raise ComponentApiUnavailable(f"consumer check failed: {process.stderr}", sources)
        raw = json.loads(process.stdout)
        if not isinstance(raw, dict) or raw.get("format") != "csharp-consumer-check/v1":
            raise ComponentApiError("unsupported consumer-check report", sources, {"raw_response": raw})
        diagnostics = raw.get("diagnostics")
        if (not isinstance(diagnostics, list) or type(raw.get("accepted")) is not bool
                or raw.get("language") != "CSharp9" or raw.get("defines") != identity["defines"]
                or not isinstance(raw.get("roslyn"), str)
                or not isinstance(raw.get("core_runtime"), dict)
                or any(not isinstance(d, dict) or not isinstance(d.get("severity"), str)
                       or d.get("severity") not in {"error", "warning", "info", "hidden"}
                       or not isinstance(d.get("code"), str) or not isinstance(d.get("message"), str)
                       or not isinstance(d.get("path"), str) or d.get("path") not in {*sources, ""}
                       for d in diagnostics)):
            raise ComponentApiError("malformed consumer-check report", sources, {"raw_response": raw})
        errors = [d for d in diagnostics if d["severity"] == "error"]
        accepted = not errors and process.returncode == 0
        if raw["accepted"] != accepted:
            raise ComponentApiError("consumer-check exit/report mismatch", sources, raw)
        report = {**identity, **raw, "format": "unity-component-check/v1", "accepted": accepted}
        # Preserve the original baseline too: an overlay must not destroy the
        # evidence against which a proposed extension was judged.
        _preserve(output_dir, sources, report)
        if output_dir is not None:
            _preserve(output_dir / "baseline", original, baseline.document)
        return CandidateCheck(report, sources)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, ComponentApiError) as exc:
        detail = exc.report if isinstance(exc, ComponentApiError) else {}
        report = {**detail, **identity, "accepted": False, "failure": str(exc)}
        _preserve(output_dir, sources, report)
        if output_dir is not None:
            _preserve(output_dir / "baseline", original, baseline.document)
        if isinstance(exc, ComponentApiError):
            raise type(exc)(str(exc), sources, report) from exc
        raise ComponentApiUnavailable(f"consumer check unavailable: {exc}", sources, report) from exc


def approve_candidate(baseline: ComponentApi, candidates: Mapping[str, str], *,
                      references: Sequence[Path] = (), output_dir: Path | None = None) -> ComponentApi:
    """Extend only after this proposal binds against the prior source snapshot.

    Compiler diagnostics never remove the failed proposal. A later consumer may
    use the returned snapshot; the old contract remains exactly unchanged.
    """
    if len(candidates) != 1:
        raise ValueError("[component-api] approve one source owner at a time; do not union unapproved collaborators")
    check = check_candidate(baseline, candidates, references=references, output_dir=output_dir)
    if not check.accepted:
        raise ComponentApiError("candidate rejected; original source and compiler diagnostics retained",
                                check.sources, check.report)
    extracted = extract_sources(check.sources, selection=baseline.document["selection"],
                                defines=baseline.document["defines"])
    document = extracted.document
    document["approved_additions"] = [*baseline.document.get("approved_additions", []), {
        "candidate_paths": sorted(candidates), "check_identity": digest(check.report),
        "scope": "SDK binding against provided sources/references; Unity Editor unverified",
    }]
    approved = ComponentApi(document, check.sources)
    _preserve(output_dir / "approved" if output_dir is not None else None, check.sources, approved.document)
    return approved
