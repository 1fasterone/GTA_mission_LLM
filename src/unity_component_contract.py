"""Read-only presentation helpers for selected Unity component contracts.

The Roslyn extractor owns declaration truth.  Consumers use this module to
present that truth without recreating a Python member table or parsing C#.
"""

from __future__ import annotations

from typing import Any, Iterable

from .unity_component_api import ComponentApi


def _parameter_text(parameter: dict[str, Any]) -> str:
    modifier = str(parameter.get("modifier") or "").strip()
    prefix = f"{modifier} " if modifier else ""
    optional = " = default" if parameter.get("optional") else ""
    params = "params " if parameter.get("params") else ""
    return f"{params}{prefix}{parameter.get('type', '?')} {parameter.get('name', '?')}{optional}"


def _member_text(member: dict[str, Any]) -> str:
    accessibility = str(member.get("accessibility") or "private")
    static = " static" if member.get("is_static") else ""
    kind = str(member.get("kind") or "member")
    name = str(member.get("name") or "?")
    declared_type = str(member.get("type") or "void")
    if kind in {"method", "constructor"}:
        parameters = ", ".join(_parameter_text(p) for p in member.get("parameters", []))
        generic = "<...>" if member.get("generic_arity") else ""
        return f"{accessibility}{static} {declared_type} {name}{generic}({parameters})"
    if kind == "event":
        return f"{accessibility}{static} event {declared_type} {name}"
    return f"{accessibility}{static} {declared_type} {name}"


def public_surface(contract: ComponentApi, type_name: str) -> list[str]:
    """Return the selected type's real public declarations in stable order."""
    component_type = contract.type(type_name)
    return [
        _member_text(member)
        for member in component_type.get("members", [])
        if member.get("accessibility") == "public"
    ]


def own_class_surface(contract: ComponentApi, type_name: str) -> list[str]:
    """Return every declaration the selected class may use internally."""
    component_type = contract.type(type_name)
    return [_member_text(member) for member in component_type.get("members", [])]


def render_surface(contract: ComponentApi, type_name: str, *, prefix: str = "") -> str:
    """Render one selected class without implying declarations not in Roslyn."""
    entries = public_surface(contract, type_name)
    if not entries:
        return f"{prefix}{type_name}: no public declarations beyond its real base types"
    return f"{prefix}{type_name}: " + "; ".join(entries)


def render_own_class_surface(contract: ComponentApi, type_name: str, *, prefix: str = "") -> str:
    """Render a class's complete declared surface for code authored in that class."""
    entries = own_class_surface(contract, type_name)
    if not entries:
        return f"{prefix}{type_name}: no declarations beyond its real base types"
    return f"{prefix}{type_name}: " + "; ".join(entries)


def render_surfaces(contract: ComponentApi, type_names: Iterable[str], *, prefix: str = "") -> list[str]:
    """Render only types present in the selected-source manifest."""
    available = {item.get("name") for item in contract.document.get("types", [])}
    return [render_surface(contract, name, prefix=prefix)
            for name in sorted({str(name) for name in type_names if str(name) in available})]
