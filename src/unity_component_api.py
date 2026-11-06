"""S1 shadow extraction of selected rendered components through SDK Roslyn.

No Python C# parser, copied member lists or production consumer cutover. Missing
extraction tooling is an explicit failure, never an accepted skipped contract.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

TOOL_DIR = Path(__file__).resolve().parents[1] / "tools/csharp_component_api"
FORMAT = "unity-component-api/v1"

from .ident import chash
from .ident.tags import COMPONENT_API, COMPONENT_API_SOURCE


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def digest(value: Any) -> str:
    # Encode the exact canonical JSON as text: schema floats are preserved,
    # while the shared identity substrate still owns all digest derivation.
    return chash(COMPONENT_API, {"json": canonical_json(value)})


class ComponentApiError(RuntimeError):
    def __init__(self, message: str, sources: Mapping[str, str], report: dict | None = None):
        self.sources = dict(sources)
        self.report = deepcopy(report or {})
        super().__init__(f"[component-api] {message}")


class ComponentApiUnavailable(ComponentApiError):
    """Required pinned toolchain could not be built or run."""


@dataclass(frozen=True, init=False)
class ComponentApi:
    # Canonical serialized snapshots prevent mutation through nested public dicts.
    _document_json: str
    _sources_json: str

    def __init__(self, document: Mapping[str, Any], sources: Mapping[str, str]):
        object.__setattr__(self, "_document_json", canonical_json(document))
        object.__setattr__(self, "_sources_json", canonical_json(sources))

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self._document_json)

    @property
    def sources(self) -> dict[str, str]:
        return json.loads(self._sources_json)

    def to_json(self) -> str:
        return self._document_json

    @property
    def fingerprint(self) -> str:
        return digest(self.document)

    def type(self, name: str) -> dict[str, Any]:
        matches = [t for t in self.document["types"] if t["id"] == name or t["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"[component-api] {name!r} resolves to {len(matches)} project types")
        return deepcopy(matches[0])


def _tool(sources: Mapping[str, str]) -> tuple[Path, dict[str, str]]:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise ComponentApiUnavailable("pinned .NET SDK unavailable; install tools/csharp_component_api/global.json SDK", sources)
    try:
        pin = json.loads((TOOL_DIR / "global.json").read_text())["sdk"]["version"]
        tool_hash = digest({name: (TOOL_DIR / name).read_bytes().decode("utf-8")
                            for name in ("Program.cs", "ConsumerCheck.cs", "ComponentApi.csproj", "global.json")})
    except (OSError, ValueError, KeyError) as exc:
        raise ComponentApiUnavailable(f"extractor sources/pin unavailable: {exc}", sources) from exc
    try:
        version = subprocess.run([dotnet, "--version"], cwd=TOOL_DIR, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ComponentApiUnavailable(f"SDK discovery failed: {exc}", sources) from exc
    if version.returncode or version.stdout.strip() != pin:
        raise ComponentApiUnavailable(f"SDK {pin} required: {version.stderr.strip() or version.stdout.strip()}", sources)
    output = TOOL_DIR / "bin/api" / tool_hash
    binary = output / "ComponentApi.dll"
    if not binary.is_file():
        command = [dotnet, "build", str(TOOL_DIR / "ComponentApi.csproj"), "--configuration", "Release",
                   "--nologo", "-v", "quiet", f"-p:OutputPath={output}/",
                   f"-p:BaseIntermediateOutputPath={TOOL_DIR / 'obj/api' / tool_hash}/"]
        try:
            build = subprocess.run(command, cwd=TOOL_DIR, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComponentApiUnavailable(f"extractor build failed: {exc}", sources) from exc
        if build.returncode or not binary.is_file():
            raise ComponentApiUnavailable(f"extractor build failed:\n{build.stdout}\n{build.stderr}", sources)
    return binary, {"sdk": pin, "extractor_hash": tool_hash}


def _preserve(output_dir: Path | None, sources: Mapping[str, str], report: dict) -> None:
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for logical, source in sources.items():
        path = Path(logical)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"invalid logical source path {logical!r}")
        target = output_dir / "sources" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.encode("utf-8"))
    (output_dir / "component-api.json").write_text(canonical_json(report), encoding="utf-8")


def extract_sources(sources: Mapping[str, str], *, selection: Sequence[dict] = (),
                    defines: Sequence[str] = (), output_dir: Path | None = None) -> ComponentApi:
    """Exact rendered-source/selection/toolchain identity; no class-only cache."""
    ordered = dict(sorted(sources.items()))
    identity = {"format": FORMAT, "selection": deepcopy(list(selection)), "sources": [
        {"path": p, "content_hash": chash(COMPONENT_API_SOURCE, {"utf8": s})} for p, s in ordered.items()]}
    try:
        binary, toolchain = _tool(ordered)
        dotnet = shutil.which("dotnet")
        assert dotnet is not None
        request = {"sources": [{"path": p, "text": s} for p, s in ordered.items()], "defines": sorted(set(defines))}
        try:
            process = subprocess.run([dotnet, str(binary)], input=canonical_json(request),
                                     cwd=TOOL_DIR, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ComponentApiUnavailable(f"extractor could not run: {exc}", ordered) from exc
        if process.returncode not in (0, 1):
            raise ComponentApiUnavailable(f"extractor invocation failed: {process.stderr}", ordered)
        try:
            raw = json.loads(process.stdout)
        except (ValueError, TypeError) as exc:
            raise ComponentApiUnavailable(f"invalid extractor response: {process.stdout}", ordered) from exc
        if raw.get("format") != "csharp-declarations/v1":
            raise ComponentApiError("unsupported extractor manifest", ordered, raw)
        document = {**identity, "toolchain": {**toolchain, "roslyn": raw["roslyn"], "language": raw["language"]},
                    "defines": raw["defines"], "types": raw["types"], "diagnostics": raw["diagnostics"],
                    "semantic_compilation": raw["semantic_compilation"]}
        if process.returncode or raw["diagnostics"]:
            detail = "; ".join(f"{d['path']}({d['line']},{d['column']}): {d['code']}: {d['message']}"
                               for d in raw["diagnostics"])
            raise ComponentApiError(detail or "extractor rejected source", ordered, document)
        ids = [t["id"] for t in document["types"]]
        if len(ids) != len(set(ids)):
            raise ComponentApiError("ambiguous duplicate project type declarations", ordered, document)
        _preserve(output_dir, ordered, document)
        return ComponentApi(document, ordered)
    except ComponentApiError as exc:
        _preserve(output_dir, ordered, {**identity, **exc.report, "failure": str(exc)})
        raise


# Schema authoring channels, not a copied C# API. Remove before default-fix so
# fragments cannot define their own judged declarations or inferred abilities.
_AUTHORING_CHANNELS = frozenset({
    "extra_usings", "extra_fields", "extra_methods", "awake_extra", "update_extra", "fixed_update_extra",
    "extensions_fields", "extensions_methods", "extensions_metadata",
})


def render_baseline(composer: Any, subsystems: Sequence[Any]) -> tuple[dict[str, str], list[dict]]:
    """Clone selection and use the production fixed-schema C# writer."""
    sources: dict[str, str] = {}
    selections: list[dict] = []
    production_paths: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="component-api-render-") as temporary:
        for index, original in enumerate(subsystems):
            sub = original.model_copy(deep=True)
            for channel in _AUTHORING_CHANNELS:
                sub.options.pop(channel, None)
            from .micro import REGISTRY
            pipeline_cls = REGISTRY.get(sub.kind)
            prepare = getattr(pipeline_cls, "api_baseline_options", None)
            if prepare is not None:
                sub.options = prepare(sub.options)
            pipeline, schema, variant, class_name, _guid = composer._resolve(sub)
            out = Path(temporary) / str(index)
            pipeline.write_api_sources(schema, variant, out)
            for path in sorted(out.rglob("*.cs")):
                production = f"{sub.kind}/{path.relative_to(out).as_posix()}"
                logical = f"{index:03d}-{sub.kind}/{path.relative_to(out).as_posix()}"
                sources[logical] = path.read_bytes().decode("utf-8")
                if production in production_paths:
                    raise ComponentApiError(f"selected writers collide at Micros/{production}", sources)
                production_paths.add(production)
            selections.append({"kind": sub.kind, "variant": variant, "class_name": class_name,
                               "target": sub.target, "schema_hash": digest(schema.model_dump(mode="json"))})
    return sources, selections


def extract_components(composer: Any, subsystems: Sequence[Any], *, output_dir: Path | None = None,
                       project_sources: Mapping[str, str] | None = None) -> ComponentApi:
    """Explicit S1 shadow entry; production consumers remain unchanged."""
    try:
        sources, selection = render_baseline(composer, subsystems)
    except ComponentApiError as exc:
        _preserve(output_dir, exc.sources, {"format": FORMAT, "failure": str(exc), **exc.report})
        raise
    for path, source in (project_sources or {}).items():
        if path in sources:
            raise ValueError(f"[component-api] duplicate project source path: {path}")
        sources[path] = source
    return extract_sources(sources, selection=selection, output_dir=output_dir)
