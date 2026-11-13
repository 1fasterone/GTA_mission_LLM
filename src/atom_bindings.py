"""Typed atom-route bindings for Unity generation (R2, S4).

An atom sequence in the schema runs only when a generated MonoBehaviour lowers
it, a scene object hosts that class, and a lifecycle phase starts it. This
module derives that route for every atom-carrying schema field: the class that
lowers the atoms, the scene object that hosts it, the phase and trigger that
start it, the scenes whose load reaches it, and the lowerer (provider) each
atom reaches. The record is persisted in ``schema.meta["atom_bindings"]``.

Derivation is a pure function of the schema state Step 6 renders, so the
generator and a saved-schema replay compute it with the same code at the same
point, and the generator places hosts from the record rather than from a second
opinion. The persisted record is the replay contract: a replay re-derives and
compares, and announces a version it cannot read or a drift from the saved
record instead of silently trusting either side.

Every atom NAME is a dynamic reference. It comes from schema data, not source,
so the static support audit cannot see it, and it is validated here per build
against the registry and the Unity lowerer table. Unity dispatches on the raw
name (``unity_atom_snippet``), so an alias, an unregistered name, a descriptive
atom with no lowerer or an atom excluded from Unity lowers to a ``// TODO``
comment. The record names each one rather than letting it read like code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import firing_provider
from .atoms import ATOM_REGISTRY, get_atom
from .atoms.base import AtomInvocation
from .atoms.behavior_map import behavior_loops
from .unity_schema import UnitySchema

BINDINGS_KEY = "atom_bindings"
FORMAT_VERSION = 1

GAME_SEQUENCER = "GameSequencer"
GAME_SEQUENCER_HOST = "_GameSequencer"
ENTITY_SPAWNER_HOST = "EntitySpawner.AddComponent"

# Scene object per UnityScript.attach_to, the same table scene.unity.j2 uses
# (`cs_hosts`) to place each custom script's MonoBehaviour.
CUSTOM_SCRIPT_HOSTS: dict[str, str] = {
    "camera": "Main Camera",
    "game_manager": "GameManager",
    "player": "Player",
    "systems": "_CustomScripts",
}

AtomStatus = Literal["lowered", "descriptive", "unsupported", "alias", "unregistered"]
Phase = Literal["start_coroutine_once", "start_coroutine_loop"]
Trigger = Literal["scene_load", "entity_spawn"]

_STATUS_WHY: dict[str, str] = {
    "descriptive": "is registered for Unity but has no Unity lowerer",
    "unsupported": "is registered but excluded from Unity (AtomDef.engines)",
    "alias": "is an alias; Unity dispatches on the canonical name only",
    "unregistered": "is not a registered atom",
}

# Hosts the scene template builds with nothing but a Transform and the class
# that lowers the route, so an atom that depends on its host provably reaches
# no gameplay object there. Custom-script hosts are not listed: another script
# on the same object may legitimately carry the component an atom reads.
EMPTY_HOSTS: frozenset[str] = frozenset({GAME_SEQUENCER_HOST})

# Host dependence is read from the C# the atom actually lowers to, with the
# same args generation passes, never from a hand-kept table.
_STRING_LITERAL = re.compile(r'@?"(?:\\.|[^"\\])*"')
_LINE_COMMENT = re.compile(r"//[^\n]*")
# `"<target>" == "Player" ? gameObject : GameObject.Find(...)` reaches the host
# only when the bound target literally equals the compared name.
_HOST_TERNARY = re.compile(r'"((?:\\.|[^"\\])*)" == "((?:\\.|[^"\\])*)" \? gameObject : ')
# A component the snippet adds itself is not one the host must already carry.
_SELF_PROVISIONED = re.compile(
    r"(?:GetComponent<(\w+)>\(\) \?\? )?gameObject\.AddComponent<\w+>\(\)")
_HOST_MEMBER = re.compile(
    r"(?<![\w.?])(transform|gameObject|GetComponents?(?:InChildren|InParent)?|"
    r"SendMessage(?:Upwards)?|BroadcastMessage)\b")


def host_members(code: str) -> list[str]:
    """The host members a lowered snippet depends on (empty: host-independent).

    ``transform`` and ``gameObject`` are the host itself; a bare
    ``GetComponent*`` needs a component already on it; a bare ``SendMessage``
    needs a receiver on it. Members reached through another object
    (``_ft.transform``, ``Camera.main?.GetComponent``) are not the host's.
    """
    code = _HOST_TERNARY.sub(
        lambda match: "gameObject " if match.group(1) == match.group(2) else "", code)
    code = _SELF_PROVISIONED.sub("", code)
    code = _LINE_COMMENT.sub("", _STRING_LITERAL.sub('""', code))
    return sorted(set(_HOST_MEMBER.findall(code)))


class RuntimeProvider(BaseModel):
    """A scene contract a lowered atom's C# resolves at runtime (R3, S5).

    ``implementer`` is the composed class that implements ``contract`` in this
    build. None means nothing does: the atom still compiles, against the
    contract alone, and refuses at runtime with a named reason.
    """

    model_config = ConfigDict(extra="forbid")

    contract: str
    host: str
    implementer: Optional[str] = None


# Each contract a lowered snippet can resolve, keyed by the one call that
# resolves it. Read from the exact C# generation emits, like host_members.
_RUNTIME_CONTRACTS: dict[str, tuple[str, str]] = {
    firing_provider.RESOLVER_CALL: (firing_provider.CONTRACT, firing_provider.HOST),
}
# What makes a build implement each contract, named when none does.
_CONTRACT_REMEDY: dict[str, str] = {
    firing_provider.CONTRACT: firing_provider.REMEDY,
}


def runtime_provider(code: str,
                     implementers: Mapping[str, str]) -> Optional[RuntimeProvider]:
    """The runtime contract a lowered snippet resolves, or None."""
    stripped = _LINE_COMMENT.sub("", _STRING_LITERAL.sub('""', code))
    for call, (contract, host) in _RUNTIME_CONTRACTS.items():
        if call in stripped:
            return RuntimeProvider(contract=contract, host=host,
                                   implementer=implementers.get(contract))
    return None


class AtomBinding(BaseModel):
    """One atom invocation and the lowerer it reaches."""

    model_config = ConfigDict(extra="forbid")

    index: int
    atom: str
    status: AtomStatus
    provider: Optional[str] = None
    canonical: Optional[str] = None
    # Lowered atoms only: the host members its emitted C# depends on.
    host_members: list[str] = Field(default_factory=list)
    # Lowered atoms only (R3): the scene contract its C# resolves at runtime.
    runtime_provider: Optional[RuntimeProvider] = None
    # Lowered atoms only (R3): args an atom that declares no parameters was
    # given. Its lowerer reads none of them, so they change nothing.
    ignored_args: list[str] = Field(default_factory=list)

    @property
    def host_dependent(self) -> bool:
        return bool(self.host_members)


class AtomRoute(BaseModel):
    """One atom-carrying schema field and the runtime route that starts it."""

    model_config = ConfigDict(extra="forbid")

    source: str
    host_class: str
    host: Optional[str]
    phase: Phase
    trigger: Trigger
    scenes: list[str]
    atoms: list[AtomBinding]
    finding: Optional[str] = None

    @property
    def attached(self) -> bool:
        return self.host is not None


class AtomBindings(BaseModel):
    """The versioned record persisted in ``schema.meta["atom_bindings"]``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    backend: Literal["unity"] = "unity"
    routes: list[AtomRoute]

    def route(self, source: str) -> Optional[AtomRoute]:
        return next((route for route in self.routes if route.source == source), None)


def gameplay_scenes(schema: UnitySchema) -> list[str]:
    """Scenes that host gameplay objects: the template's `"menu"` exclusion."""
    return [scene for scene in schema.scenes if "menu" not in scene.lower()]


def _provider(handler: object) -> str:
    module = getattr(handler, "__module__", "") or ""
    name = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "")
    path = module.replace(".", "/") + ".py" if module else "<unknown>"
    return f"{path}:{name}"


def bind_atom(index: int, invocation: AtomInvocation,
              implementers: Optional[Mapping[str, str]] = None) -> AtomBinding:
    """Validate one dynamic atom reference against the registry and lowerers.

    ``implementers`` maps a runtime contract to the composed class that
    implements it in this build (``{"IFiringProvider": "PlayerController"}``).
    """
    from .atoms.backends.unity import _SNIPPETS

    name = invocation.atom
    handler = _SNIPPETS.get(name)
    definition = ATOM_REGISTRY.get(name)
    if handler is not None:
        args = dict(invocation.args or {})
        # The exact call unity_atom_snippet makes during generation.
        code = handler(dict(args))
        ignored = (sorted(args) if definition is not None and not definition.params
                   else [])
        return AtomBinding(index=index, atom=name, status="lowered",
                           provider=_provider(handler), host_members=host_members(code),
                           runtime_provider=runtime_provider(code, implementers or {}),
                           ignored_args=ignored)
    if definition is not None:
        status: AtomStatus = "descriptive" if "unity" in definition.engines else "unsupported"
        return AtomBinding(index=index, atom=name, status=status)
    owner = get_atom(name)
    if owner is not None:
        return AtomBinding(index=index, atom=name, status="alias", canonical=owner.name)
    return AtomBinding(index=index, atom=name, status="unregistered")


def _bind_all(atoms: list[AtomInvocation],
              implementers: Mapping[str, str]) -> list[AtomBinding]:
    return [bind_atom(index, invocation, implementers)
            for index, invocation in enumerate(atoms)]


def _entity_scenes(schema: UnitySchema, entity_name: str) -> list[str]:
    specs = {spec.name: spec for spec in schema.scene_specs}
    hosted = []
    for scene in gameplay_scenes(schema):
        spec = specs.get(scene)
        if spec is not None and spec.entities is not None and entity_name not in spec.entities:
            continue
        hosted.append(scene)
    return hosted


def _firing_controllers(composed_units: Optional[list]) -> list:
    """Composed controllers whose rendered class implements IFiringProvider.

    Read after ``default_fix`` (``ComposedUnit.abilities`` is the survived
    list), through the same predicate the controller micro emits the contract
    with, so the record and the emitted files agree.
    """
    from .micro.controller import provides_firing

    return [unit for unit in composed_units or []
            if getattr(unit, "kind", "") == "controller"
            and provides_firing(list(getattr(unit, "abilities", []) or []),
                                str(getattr(unit, "variant", "")))]


def contract_implementers_for(composed_units: Optional[list] = None) -> dict[str, str]:
    """The composed class that implements each runtime contract on its host (R3, S5).

    A class implements the contract for the lowered C# only where that C#
    looks: ``FiringProviders.FirePlayer()`` resolves the object tagged Player,
    which hosts exactly the units on the ``player`` target. A shooting
    controller on another target is not the implementer, so the build warns
    instead of recording a provider ``Shoot`` can never reach. The compose
    graph places at most one player controller per project.
    """
    implementers: dict[str, str] = {}
    for unit in _firing_controllers(composed_units):
        if getattr(unit, "target", "") == firing_provider.HOST_TARGET:
            implementers.setdefault(firing_provider.CONTRACT, unit.class_name)
    return implementers


def firing_contract_emitted(composed_units: Optional[list] = None) -> bool:
    """True when a composed controller micro already emits FiringProvider.cs.

    Independent of the target: the micro writes the contract beside any
    implementing class, so a second, contract-only copy would be a duplicate
    type (CS0101) even when that class sits off the Player.
    """
    return bool(_firing_controllers(composed_units))


def derive_atom_bindings(
    schema: UnitySchema,
    entity_model_paths: Optional[Mapping[str, str]] = None,
    contract_implementers: Optional[Mapping[str, str]] = None,
) -> AtomBindings:
    """Derive every Unity atom route from the schema state Step 6 renders.

    Call it after the generator's entity-behavior reconciliation and script
    preparation, because both decide which class a route reaches. Pure and
    deterministic: the same schema yields an equal record every time.

    ``contract_implementers`` names, per runtime contract, the composed class
    that implements it (``contract_implementers_for``); a contract absent from
    it has no implementer in this build.
    """
    from .unity_generator import entity_behavior_class_name

    implementers = dict(contract_implementers or {})
    model_paths = entity_model_paths or {}
    scenes = gameplay_scenes(schema)
    routes: list[AtomRoute] = []

    if schema.atoms:
        routes.append(AtomRoute(
            source="atoms",
            host_class=GAME_SEQUENCER,
            host=GAME_SEQUENCER_HOST if scenes else None,
            phase="start_coroutine_once",
            trigger="scene_load",
            scenes=scenes,
            atoms=_bind_all(schema.atoms, implementers),
            finding=None if scenes else (
                "no gameplay scene to host GameSequencer (every scene is a menu); "
                "schema.atoms is not emitted"),
        ))

    for entity in schema.entities:
        if not entity.atoms:
            continue
        host_class = entity_behavior_class_name(entity.name)
        hosted = _entity_scenes(schema, entity.name)
        finding = None
        if entity.behavior != host_class:
            finding = (f"entity.behavior is {entity.behavior!r}, so EntitySpawner never "
                       f"adds {host_class}")
        elif str(model_paths.get(entity.name, "")).startswith("skip:"):
            finding = "the entity group is skipped at spawn, so nothing adds its behavior"
        elif not hosted:
            finding = "no gameplay scene hosts this entity group"
        routes.append(AtomRoute(
            source=f"entities[{entity.name}].atoms",
            host_class=host_class,
            host=None if finding else ENTITY_SPAWNER_HOST,
            # EntityBehavior.cs.j2 restarts Run() unless the sequence despawns.
            phase="start_coroutine_loop" if behavior_loops(entity.atoms) else "start_coroutine_once",
            trigger="entity_spawn",
            scenes=[] if finding else hosted,
            atoms=_bind_all(entity.atoms, implementers),
            finding=finding,
        ))

    for script in schema.scripts:
        if script.type != "custom" or not script.atoms:
            continue
        host = CUSTOM_SCRIPT_HOSTS.get(script.attach_to)
        finding = None
        if host is None:
            finding = f"attach_to {script.attach_to!r} names no scene host"
        elif not scenes:
            finding = "no gameplay scene hosts custom scripts"
        routes.append(AtomRoute(
            source=f"scripts[{script.name}].atoms",
            host_class=script.name,
            host=None if finding else host,
            phase="start_coroutine_once",
            trigger="scene_load",
            scenes=[] if finding else scenes,
            atoms=_bind_all(script.atoms, implementers),
            finding=finding,
        ))

    return AtomBindings(routes=routes)


def _route_messages(record: AtomBindings) -> list[str]:
    messages: list[str] = []
    for route in record.routes:
        if route.finding:
            messages.append(f"atom bindings: {route.source} does not run — {route.finding}")
        for binding in route.atoms:
            if binding.status == "lowered":
                if binding.host_dependent and route.host in EMPTY_HOSTS:
                    messages.append(
                        f"atom bindings: {route.source}[{binding.index}] {binding.atom!r} "
                        f"acts on its own host ({', '.join(binding.host_members)}), but "
                        f"{route.host} is an empty scene object (a Transform and "
                        f"{route.host_class}), so it changes no gameplay object; author "
                        "it in the target entity's atoms instead")
                provider = binding.runtime_provider
                if provider is not None and provider.implementer is None:
                    messages.append(
                        f"atom bindings: {route.source}[{binding.index}] {binding.atom!r} "
                        f"resolves {provider.contract} on the {provider.host} at runtime, "
                        f"but no composed class implements {provider.contract} in this "
                        "build; it compiles against the contract alone and refuses every "
                        f"call at runtime (a logged error); {_CONTRACT_REMEDY[provider.contract]}")
                if binding.ignored_args:
                    messages.append(
                        f"atom bindings: {route.source}[{binding.index}] {binding.atom!r} "
                        "declares no parameters, so arg(s) "
                        f"{', '.join(binding.ignored_args)} change nothing in the "
                        "emitted C#")
                continue
            why = _STATUS_WHY[binding.status]
            if binding.canonical:
                why += f" ({binding.canonical!r})"
            messages.append(
                f"atom bindings: {route.source}[{binding.index}] {binding.atom!r} {why}; "
                "it lowers to a // TODO comment and nothing runs")
    return messages


def _saved_record(meta: Mapping[str, object]) -> tuple[Optional[AtomBindings], Optional[str]]:
    if BINDINGS_KEY not in meta:
        return None, None
    raw = meta[BINDINGS_KEY]
    version = raw.get("version") if isinstance(raw, dict) else None
    try:
        return AtomBindings.model_validate(raw), None
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<record>"
        return None, (
            f"atom bindings: saved record is not replayable by this compiler "
            f"(version {version!r}, expected {FORMAT_VERSION}; {location}: "
            f"{first.get('msg', 'invalid')}); re-derived from the schema")


@dataclass(frozen=True)
class BindingReport:
    """What one stamp found: informational notes and loud warnings."""

    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def stamp_atom_bindings(schema: UnitySchema, record: AtomBindings) -> BindingReport:
    """Persist ``record`` and report what a replay of the saved one found.

    The schema's own atom lists are canonical. A saved record that disagrees
    is reported route by route and replaced; an unreadable one is announced
    and replaced. Neither is trusted silently, and neither aborts the build.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    saved, unreadable = _saved_record(meta)
    report = BindingReport()
    if unreadable:
        report.warnings.append(unreadable)
    elif saved is not None:
        if saved == record:
            report.notes.append(
                f"atom bindings: saved record replayed identically ({len(record.routes)} route(s))")
        else:
            before = {route.source: route for route in saved.routes}
            after = {route.source: route for route in record.routes}
            changed = sorted(source for source in before.keys() | after.keys()
                             if before.get(source) != after.get(source))
            report.warnings.append(
                "atom bindings: replay drift — the saved record differs from this build "
                f"for {', '.join(changed)}; the schema's atoms are canonical, re-stamped")
    else:
        attached = sum(route.attached for route in record.routes)
        report.notes.append(
            f"atom bindings: stamped {len(record.routes)} route(s), {attached} attached")
    report.warnings.extend(_route_messages(record))
    meta[BINDINGS_KEY] = record.model_dump(mode="json")
    schema.meta = meta
    return report


def saved_atom_bindings(schema: UnitySchema) -> Optional[AtomBindings]:
    """The persisted record, validated; None when there is none to read."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    record, _ = _saved_record(meta)
    return record
