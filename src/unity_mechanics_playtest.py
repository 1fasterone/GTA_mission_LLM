"""Deterministic pre-IR mechanics playtest.

This module is the pure foundation for the mechanics playtest described in
the pipeline plan.  It deliberately does not import Unity, launch a process,
write generated code, or call an LLM.  The adapter that turns ``UnitySchema``
and the resolver/composer plan into :class:`MechanicsPlaytestInput` belongs to
a later slice.

The contract is intentionally engine-neutral at this boundary:

``ATOM_IR``
    A list of registered atom invocations.  ``world_atoms`` and
    ``character_atoms`` are kept separate so findings can point to the right
    design layer.
``RESOLVER_OUTPUT``
    A deterministic mapping containing the combined gameplay graph.  The
    evaluator consumes the optional keys ``objectives``, ``entities``,
    ``transitions``, ``rewards``, ``feedback``, ``affordances``, ``controls``,
    and ``dependencies``.  ``src/unity_mechanics_adapter.py`` populates these
    from existing schema/composer data; this module does not add fields to
    ``UnitySchema``.

The evaluator is conservative.  Missing evidence becomes a finding or an
explicitly ungraded score; it is never silently treated as a passing mechanic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .atoms import get_atom


SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITY_BLOCKER = "blocker"

_SEVERITY_ORDER = {
    SEVERITY_BLOCKER: 0,
    SEVERITY_HIGH: 1,
    SEVERITY_MEDIUM: 2,
    SEVERITY_LOW: 3,
}

_TARGETED_OBJECTIVES = frozenset({"reach", "collect", "defeat", "escort"})
_ACTION_OBJECTIVES = frozenset({"reach", "collect", "defeat", "escort", "survive", "timer"})
_PROGRESS_WORDS = frozenset({
    "advance", "complete", "collect", "damage", "defeat", "finish", "kill",
    "progress", "reach", "reward", "survive", "unlock",
})
_FEEDBACK_WORDS = frozenset({
    "animate", "animation", "audio", "feedback", "hud", "particle", "sfx",
    "sound", "ui", "vfx", "visual",
})
_AFFORDANCE_WORDS = frozenset({
    "command", "control", "cursor", "hint", "hud", "input", "interact",
    "pointer", "prompt", "trigger", "waypoint",
})


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _clean(value).lower()


def _words(value: Any) -> frozenset[str]:
    text = _lower(value).replace("_", " ").replace("-", " ")
    return frozenset(part for part in text.split() if part)


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a list")
    return list(value)


@dataclass(frozen=True)
class MechanicsAtom:
    """One normalized atom invocation supplied to the preflight."""

    name: str
    category: str = "unknown"
    args: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""
    owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom": self.name,
            "category": self.category,
            "args": dict(self.args),
            "label": self.label,
            "owner": self.owner,
        }


def normalize_atoms(raw: Any, *, owner: str) -> tuple[MechanicsAtom, ...]:
    """Normalize atom records without inventing unknown atom types.

    Aliases are canonicalized through the existing atom registry.  Unknown
    names remain visible as ``category='unknown'`` so the evaluator can fail
    loudly instead of dropping them.
    """
    records: list[MechanicsAtom] = []
    for index, item in enumerate(_as_list(raw, f"{owner} atoms")):
        if isinstance(item, str):
            name = item.strip()
            args: Mapping[str, Any] = {}
            label = ""
        elif isinstance(item, Mapping):
            name = _clean(item.get("atom") or item.get("name"))
            args = _as_dict(item.get("args"), f"{owner} atoms[{index}].args")
            label = _clean(item.get("label"))
        else:
            raise ValueError(f"{owner} atoms[{index}] must be a string or object")

        if not name:
            name = f"<missing:{owner}:{index}>"
        definition = get_atom(name)
        if definition is not None:
            name = definition.name
            category = definition.category
        else:
            category = "unknown"
        records.append(MechanicsAtom(
            name=name,
            category=category,
            args=dict(args),
            label=label,
            owner=owner,
        ))
    return tuple(records)


@dataclass(frozen=True)
class MechanicsPlaytestInput:
    """Normalized pre-IR input.

    ``resolver_output`` is copied at construction so later caller mutation
    cannot change a report after it has been evaluated.
    """

    atom_ir: tuple[MechanicsAtom, ...]
    resolver_output: Mapping[str, Any]
    world_atoms: tuple[MechanicsAtom, ...]
    character_atoms: tuple[MechanicsAtom, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MechanicsPlaytestInput":
        if not isinstance(raw, Mapping):
            raise ValueError("mechanics playtest input must be an object")
        resolver_output = _as_dict(raw.get("RESOLVER_OUTPUT", raw.get("resolver_output")),
                                   "RESOLVER_OUTPUT")
        for key in (
            "objectives", "entities", "transitions", "rewards", "feedback",
            "affordances", "controls", "dependencies",
        ):
            if key in resolver_output:
                _as_list(resolver_output[key], f"RESOLVER_OUTPUT.{key}")
        project_atoms = normalize_atoms(
            raw.get("ATOM_IR", raw.get("atom_ir")), owner="ATOM_IR")
        world_atoms = normalize_atoms(
            raw.get("WORLD_ATOMS", raw.get("world_atoms")), owner="WORLD_ATOMS")
        character_atoms = normalize_atoms(
            raw.get("CHARACTER_ATOMS", raw.get("character_atoms")), owner="CHARACTER_ATOMS")
        return cls(
            atom_ir=project_atoms,
            resolver_output=resolver_output,
            world_atoms=world_atoms,
            character_atoms=character_atoms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ATOM_IR": [atom.to_dict() for atom in self.atom_ir],
            "RESOLVER_OUTPUT": dict(self.resolver_output),
            "WORLD_ATOMS": [atom.to_dict() for atom in self.world_atoms],
            "CHARACTER_ATOMS": [atom.to_dict() for atom in self.character_atoms],
        }


@dataclass(frozen=True)
class MechanicsFinding:
    check: str
    severity: str
    summary: str
    affected_atoms: tuple[str, ...] = ()
    source: str = ""
    recommended_correction: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "summary": self.summary,
            "affected_atoms": list(self.affected_atoms),
            "source": self.source,
            "recommended_correction": self.recommended_correction,
        }


@dataclass(frozen=True)
class ScoreSection:
    score: int
    evidence: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    graded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "evidence": list(self.evidence),
            "risks": list(self.risks),
            "graded": self.graded,
        }


@dataclass(frozen=True)
class MechanicsPlayabilityReport:
    """Structured ``PLAYABILITY_REPORT`` emitted by the evaluator."""

    fun_score: ScoreSection
    friction_score: ScoreSection
    pacing_score: ScoreSection
    difficulty_score: ScoreSection
    readability_score: ScoreSection
    systemic_coherence_score: ScoreSection
    dead_loop_detector: tuple[dict[str, Any], ...]
    reward_loop_validation: tuple[dict[str, Any], ...]
    missing_feedback_detector: tuple[dict[str, Any], ...]
    missing_affordance_detector: tuple[dict[str, Any], ...]
    mission_flow_validation: tuple[dict[str, Any], ...]
    playability_fixes: tuple[MechanicsFinding, ...]
    unity_ir_impact: tuple[dict[str, Any], ...]
    final_decision: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return exactly the requested top-level report sections."""
        return {
            "FUN_SCORE": self.fun_score.to_dict(),
            "FRICTION_SCORE": self.friction_score.to_dict(),
            "PACING_SCORE": self.pacing_score.to_dict(),
            "DIFFICULTY_SCORE": self.difficulty_score.to_dict(),
            "READABILITY_SCORE": self.readability_score.to_dict(),
            "SYSTEMIC_COHERENCE_SCORE": self.systemic_coherence_score.to_dict(),
            "DEAD_LOOP_DETECTOR": list(self.dead_loop_detector),
            "REWARD_LOOP_VALIDATION": list(self.reward_loop_validation),
            "MISSING_FEEDBACK_DETECTOR": list(self.missing_feedback_detector),
            "MISSING_AFFORDANCE_DETECTOR": list(self.missing_affordance_detector),
            "MISSION_FLOW_VALIDATION": list(self.mission_flow_validation),
            "PLAYABILITY_FIXES": [finding.to_dict() for finding in self.playability_fixes],
            "UNITY_IR_IMPACT": list(self.unity_ir_impact),
            "FINAL_DECISION": dict(self.final_decision),
        }

    def render(self) -> str:
        """Stable human-readable summary for logs and review artifacts."""
        decision = _clean(self.final_decision.get("decision"))
        lines = [
            "PLAYABILITY_REPORT",
            f"  FINAL_DECISION: {decision}",
            "  SCORES: "
            + ", ".join([
                f"fun={self.fun_score.score}/10",
                f"friction={self.friction_score.score}/10",
                f"pacing={self.pacing_score.score}/10",
                f"difficulty={self.difficulty_score.score}/10",
                f"readability={self.readability_score.score}/10",
                f"systemic_coherence={self.systemic_coherence_score.score}/10",
            ]),
            f"  FIXES: {len(self.playability_fixes)}",
        ]
        for finding in sorted(
            self.playability_fixes,
            key=lambda item: (_SEVERITY_ORDER.get(item.severity, 99), item.check, item.summary),
        ):
            lines.append(f"  {finding.severity.upper():8} {finding.check}: {finding.summary}")
        return "\n".join(lines)


def _records(output: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    raw = _as_list(output.get(key), f"RESOLVER_OUTPUT.{key}")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            result.append({"name": item.strip()})
        elif isinstance(item, Mapping):
            result.append(dict(item))
        else:
            raise ValueError(f"RESOLVER_OUTPUT.{key}[{index}] must be a string or object")
    return result


def _record_text(record: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "id", "name", "type", "action", "event", "target", "target_ref",
        "objective_id", "source", "destination", "state", "kind",
    ):
        value = record.get(key)
        if value is not None:
            values.append(_clean(value))
    for key in ("produces", "consumes", "feedback", "affordances", "actions"):
        value = record.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            values.extend(_clean(entry) for entry in value)
        elif value is not None:
            values.append(_clean(value))
    return " ".join(value for value in values if value)


def _record_matches(record: Mapping[str, Any], *needles: str) -> bool:
    haystack = _lower(_record_text(record))
    return any(_lower(needle) and _lower(needle) in haystack for needle in needles)


def _atom_signal(atoms: Sequence[MechanicsAtom], *needles: str) -> bool:
    wanted = [_lower(needle) for needle in needles if _lower(needle)]
    for atom in atoms:
        haystack = " ".join([
            _lower(atom.name),
            _lower(atom.category),
            _lower(atom.label),
            " ".join(_lower(value) for value in atom.args.values()),
        ])
        if any(needle in haystack for needle in wanted):
            return True
    return False


def _all_atoms(playtest_input: MechanicsPlaytestInput) -> tuple[MechanicsAtom, ...]:
    return (
        *playtest_input.atom_ir,
        *playtest_input.world_atoms,
        *playtest_input.character_atoms,
    )


def _named_entities(playtest_input: MechanicsPlaytestInput, output: Mapping[str, Any]) -> set[str]:
    names = {
        _clean(item.get("name") or item.get("id"))
        for item in _records(output, "entities")
        if _clean(item.get("name") or item.get("id"))
    }
    for atom in (*playtest_input.world_atoms, *playtest_input.character_atoms):
        for key in ("entity", "target", "name", "id"):
            value = _clean(atom.args.get(key))
            if value:
                names.add(value)
    return names


def _objective_records(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    objectives: list[dict[str, Any]] = []
    for index, item in enumerate(_records(output, "objectives")):
        record = dict(item)
        record.setdefault("id", f"objective_{index + 1}")
        record.setdefault("type", "reach")
        record.setdefault("count", 1)
        record.setdefault("optional", False)
        objectives.append(record)
    return objectives


def _target_required(objective: Mapping[str, Any]) -> bool:
    return _lower(objective.get("type")) in _TARGETED_OBJECTIVES


def _objective_transition(objective: Mapping[str, Any], transition: Mapping[str, Any]) -> bool:
    objective_id = _clean(objective.get("id"))
    transition_id = _clean(transition.get("objective_id"))
    if objective_id and transition_id and objective_id == transition_id:
        return True
    target = _clean(objective.get("target_ref"))
    objective_type = _lower(objective.get("type"))
    text = _lower(_record_text(transition))
    if target and target.lower() in text:
        return True
    return bool(objective_type and objective_type in text)


def _has_completion(transition: Mapping[str, Any]) -> bool:
    if bool(transition.get("completion")):
        return True
    text = _lower(_record_text(transition))
    return bool({"complete", "completed", "finish", "finished", "success", "won"} & _words(text))


def _has_progress(transition: Mapping[str, Any]) -> bool:
    if bool(transition.get("progress")):
        return True
    text = _lower(_record_text(transition))
    return bool(_PROGRESS_WORDS & _words(text))


def _has_feedback(record: Mapping[str, Any], feedback: Sequence[Mapping[str, Any]]) -> bool:
    explicit = record.get("feedback")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes, bytearray)):
        if any(_clean(item) for item in explicit):
            return True
    elif _clean(explicit):
        return True
    return any(
        _record_matches(item, _clean(record.get("id")), _clean(record.get("objective_id")),
                       _clean(record.get("action")), _clean(record.get("event")))
        for item in feedback
    )


def _has_affordance(objective: Mapping[str, Any], affordances: Sequence[Mapping[str, Any]]) -> bool:
    target = _clean(objective.get("target_ref"))
    objective_type = _clean(objective.get("type"))
    return any(_record_matches(item, target, objective_type, _clean(objective.get("id")))
               for item in affordances)


def _graph_cycles(transitions: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """Return strongly-connected components that contain a cycle."""
    graph: dict[str, set[str]] = {}
    for index, transition in enumerate(transitions):
        source = _clean(transition.get("source")) or f"<source:{index}>"
        target = _clean(transition.get("destination") or transition.get("target"))
        if not target:
            continue
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())

    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index_counter
        indices[node] = index_counter
        lowlink[node] = index_counter
        index_counter += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in indices:
                visit(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])
        if lowlink[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        component = sorted(component)
        if len(component) > 1 or component[0] in graph.get(component[0], set()):
            components.append(tuple(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components)


def _score(base: int, bonuses: int, penalties: int) -> int:
    return max(0, min(10, int(base + bonuses - penalties)))


def _score_section(score: int, evidence: list[str], risks: list[str]) -> ScoreSection:
    return ScoreSection(
        score=max(0, min(10, int(score))),
        evidence=tuple(sorted(set(evidence))),
        risks=tuple(sorted(set(risks))),
        graded=True,
    )


def _finding(
    check: str,
    severity: str,
    summary: str,
    *,
    affected_atoms: Sequence[str] = (),
    source: str = "",
    correction: str = "",
) -> MechanicsFinding:
    return MechanicsFinding(
        check=check,
        severity=severity,
        summary=summary,
        affected_atoms=tuple(sorted(set(_clean(atom) for atom in affected_atoms if _clean(atom)))),
        source=source,
        recommended_correction=correction,
    )


def evaluate_mechanics(
    playtest_input: MechanicsPlaytestInput,
    *,
    max_seconds: int = 300,
) -> MechanicsPlayabilityReport:
    """Run the deterministic pre-IR mechanics playtest.

    The virtual clock is currently represented by the bounded simulation
    horizon and objective/transition checks.  Later slices can add richer
    state transitions without changing this report contract.
    """
    output = dict(playtest_input.resolver_output)
    all_atoms = _all_atoms(playtest_input)
    objectives = _objective_records(output)
    transitions = _records(output, "transitions")
    rewards = _records(output, "rewards")
    feedback = _records(output, "feedback")
    affordances = _records(output, "affordances")
    dependencies = _records(output, "dependencies")
    entities = _named_entities(playtest_input, output)
    findings: list[MechanicsFinding] = []

    unknown_atoms = sorted({atom.name for atom in all_atoms if atom.category == "unknown"})
    for atom_name in unknown_atoms:
        findings.append(_finding(
            "atom_validation",
            SEVERITY_BLOCKER,
            f"atom '{atom_name}' is not registered, so its mechanic cannot be simulated",
            affected_atoms=[atom_name],
            source="ATOM_IR / WORLD_ATOMS / CHARACTER_ATOMS",
            correction="Use an atom from the registry or add a reviewed atom definition before code generation.",
        ))

    unresolved_dependencies = [
        item for item in dependencies
        if _lower(item.get("status")) in {"missing", "unresolved", "broken", "false"}
        or item.get("resolved") is False
    ]
    for dependency in unresolved_dependencies:
        label = _record_text(dependency) or "unnamed dependency"
        findings.append(_finding(
            "systemic_coherence",
            SEVERITY_HIGH,
            f"resolver dependency is unresolved: {label}",
            source="RESOLVER_OUTPUT.dependencies",
            correction="Resolve the dependency deterministically or abstain before Unity IR generation.",
        ))

    mission_results: list[dict[str, Any]] = []
    for objective in objectives:
        objective_id = _clean(objective.get("id"))
        objective_type = _lower(objective.get("type"))
        target = _clean(objective.get("target_ref"))
        target_exists = not _target_required(objective) or target in entities
        matching = [transition for transition in transitions if _objective_transition(objective, transition)]
        completes = any(_has_completion(transition) for transition in matching)
        has_affordance = _has_affordance(objective, affordances)
        result = {
            "objective_id": objective_id,
            "type": objective_type,
            "target_ref": target,
            "target_exists": target_exists,
            "transition_count": len(matching),
            "completable": completes,
            "affordance_present": has_affordance,
        }
        mission_results.append(result)

        if _target_required(objective) and not target:
            findings.append(_finding(
                "mission_flow",
                SEVERITY_BLOCKER,
                f"required {objective_type} objective '{objective_id}' has no target",
                source=f"RESOLVER_OUTPUT.objectives[{objective_id}]",
                correction="Bind the objective to a declared entity or make the objective type self-driven.",
            ))
        elif _target_required(objective) and not target_exists:
            findings.append(_finding(
                "mission_flow",
                SEVERITY_BLOCKER,
                f"objective '{objective_id}' targets undeclared entity '{target}'",
                source=f"RESOLVER_OUTPUT.objectives[{objective_id}].target_ref",
                correction="Declare the target in the world/character inputs and place it in the mission's active scope.",
            ))
        if not matching:
            findings.append(_finding(
                "mission_flow",
                SEVERITY_HIGH if not bool(objective.get("optional")) else SEVERITY_MEDIUM,
                f"objective '{objective_id}' has no resolver transition that can advance it",
                affected_atoms=[objective_type],
                source=f"RESOLVER_OUTPUT.objectives[{objective_id}]",
                correction="Add a deterministic event source and bind it to the objective.",
            ))
        elif not completes:
            findings.append(_finding(
                "mission_flow",
                SEVERITY_HIGH if not bool(objective.get("optional")) else SEVERITY_MEDIUM,
                f"objective '{objective_id}' has transitions but no completion path",
                affected_atoms=[_clean(transition.get("action")) for transition in matching],
                source=f"RESOLVER_OUTPUT.transitions -> {objective_id}",
                correction="Add an explicit success transition and mission-state update.",
            ))
        if matching and not any(_has_feedback(transition, feedback) for transition in matching):
            findings.append(_finding(
                "missing_feedback",
                SEVERITY_MEDIUM,
                f"objective '{objective_id}' advances without explicit player feedback",
                affected_atoms=[_clean(transition.get("action")) for transition in matching],
                source=f"RESOLVER_OUTPUT.transitions -> {objective_id}",
                correction="Bind objective progress/completion to HUD, audio, VFX, or animation feedback.",
            ))
        if matching and not has_affordance:
            findings.append(_finding(
                "missing_affordance",
                SEVERITY_MEDIUM,
                f"objective '{objective_id}' has no explicit affordance cue",
                affected_atoms=[objective_type],
                source=f"RESOLVER_OUTPUT.affordances -> {objective_id}",
                correction="Expose the available action through a control, prompt, waypoint, command, or HUD cue.",
            ))

    if not objectives and not any(_has_completion(transition) for transition in transitions):
        findings.append(_finding(
            "mission_flow",
            SEVERITY_BLOCKER,
            "the mechanics input has neither an objective nor a completion transition",
            source="RESOLVER_OUTPUT.objectives / transitions",
            correction="Declare a player goal with a deterministic completion path.",
        ))

    controls = _records(output, "controls")
    has_control = bool(controls) or _atom_signal(all_atoms, "controller", "input", "move", "interact", "command")
    if not has_control:
        findings.append(_finding(
            "missing_affordance",
            SEVERITY_BLOCKER,
            "no player control or interaction path is declared",
            source="RESOLVER_OUTPUT.controls / ATOM_IR",
            correction="Declare the control surface and bind it to at least one playable action.",
        ))

    cycles = _graph_cycles(transitions)
    dead_loops: list[dict[str, Any]] = []
    for component in cycles:
        members = set(component)
        inside = [
            transition for transition in transitions
            if _clean(transition.get("source")) in members
            and _clean(transition.get("destination") or transition.get("target")) in members
        ]
        has_exit = any(
            _clean(transition.get("source")) in members
            and _clean(transition.get("destination") or transition.get("target")) not in members
            for transition in transitions
        )
        has_progress = any(_has_progress(transition) for transition in inside)
        has_reward = any(bool(transition.get("reward")) for transition in inside)
        has_completion = any(_has_completion(transition) for transition in inside)
        if not has_exit and not has_progress and not has_reward and not has_completion:
            finding = _finding(
                "dead_loop",
                SEVERITY_BLOCKER,
                f"cycle {', '.join(component)} has no exit, progress, reward, or completion",
                source="RESOLVER_OUTPUT.transitions",
                correction="Add an exit condition, progress edge, reward, or terminal completion state.",
            )
            findings.append(finding)
            dead_loops.append({
                "cycle": list(component),
                "severity": finding.severity,
                "has_exit": False,
                "has_progress": False,
                "has_reward": False,
                "has_completion": False,
                "finding": finding.summary,
            })
        else:
            dead_loops.append({
                "cycle": list(component),
                "severity": SEVERITY_LOW,
                "has_exit": has_exit,
                "has_progress": has_progress,
                "has_reward": has_reward,
                "has_completion": has_completion,
                "finding": "cycle has a declared progression or exit path",
            })

    reward_results: list[dict[str, Any]] = []
    for reward in rewards:
        has_consumer = bool(reward.get("consumer") or reward.get("consumed_by"))
        if not has_consumer:
            findings.append(_finding(
                "reward_loop",
                SEVERITY_MEDIUM,
                f"reward '{_record_text(reward) or 'unnamed'}' has no declared consumer or progression effect",
                source="RESOLVER_OUTPUT.rewards",
                correction="Bind the reward to an unlock, stat, inventory, currency, or subsequent objective.",
            ))
        reward_results.append({
            "reward": _record_text(reward) or "unnamed",
            "consumer_present": has_consumer,
            "status": "validated" if has_consumer else "missing_consumer",
        })
    if objectives and not rewards:
        reward_results.append({
            "reward": "",
            "consumer_present": False,
            "status": "no_explicit_reward_or_progression",
        })
        findings.append(_finding(
            "reward_loop",
            SEVERITY_MEDIUM,
            "objectives have no explicit reward or progression effect",
            source="RESOLVER_OUTPUT.rewards",
            correction="Declare a reward or explicitly mark the loop as self-sufficient rather than implying progression.",
        ))

    feedback_results: list[dict[str, Any]] = []
    feedback_signal = bool(feedback) or _atom_signal(all_atoms, *_FEEDBACK_WORDS)
    if transitions and not feedback_signal:
        finding = _finding(
            "missing_feedback",
            SEVERITY_HIGH,
            "state transitions exist but no feedback channel is declared",
            source="RESOLVER_OUTPUT.feedback / ATOM_IR",
            correction="Add deterministic HUD, audio, VFX, or animation feedback for state changes.",
        )
        findings.append(finding)
        feedback_results.append({"severity": finding.severity, "finding": finding.summary})
    elif feedback_signal:
        feedback_results.append({"severity": SEVERITY_LOW, "finding": "feedback channel declared"})

    affordance_results: list[dict[str, Any]] = []
    affordance_signal = bool(affordances) or _atom_signal(all_atoms, *_AFFORDANCE_WORDS)
    if objectives and not affordance_signal:
        finding = _finding(
            "missing_affordance",
            SEVERITY_HIGH,
            "objectives exist but no affordance channel is declared",
            source="RESOLVER_OUTPUT.affordances / ATOM_IR",
            correction="Add a deterministic control, prompt, command, waypoint, or HUD cue.",
        )
        findings.append(finding)
        affordance_results.append({"severity": finding.severity, "finding": finding.summary})
    elif affordance_signal:
        affordance_results.append({"severity": SEVERITY_LOW, "finding": "affordance channel declared"})

    mission_flow = [
        {
            **result,
            "status": "pass" if result["target_exists"] and result["completable"] else "fail",
        }
        for result in mission_results
    ]

    all_checks = [finding.check for finding in findings]
    blocker_count = sum(finding.severity == SEVERITY_BLOCKER for finding in findings)
    high_count = sum(finding.severity == SEVERITY_HIGH for finding in findings)
    medium_count = sum(finding.severity == SEVERITY_MEDIUM for finding in findings)
    objective_count = len(objectives)
    completed_count = sum(result["completable"] for result in mission_results)
    valid_objectives = objective_count > 0 and completed_count == objective_count

    fun = _score(
        4,
        int(has_control) + int(valid_objectives) + int(bool(rewards)) + int(feedback_signal),
        blocker_count * 2 + high_count,
    )
    # Friction is a burden score, not a quality score: zero means no detected
    # friction and higher values mean more frustration risk. This direction is
    # part of the improvement-loop contract (FRICTION_SCORE <= 4).
    friction = min(10, blocker_count * 2 + high_count + medium_count)
    pacing = _score(
        4,
        int(len(transitions) >= max(1, objective_count)) + int(bool(cycles)),
        int(not transitions) + blocker_count + high_count,
    )
    difficulty = _score(
        5,
        int(any(_lower(objective.get("type")) in {"defeat", "survive", "timer"} for objective in objectives)),
        int(not transitions) + blocker_count + int(not any(_lower(objective.get("type")) in _ACTION_OBJECTIVES for objective in objectives)),
    )
    readability = _score(
        4,
        int(has_control) + int(bool(objectives)) + int(affordance_signal) + int(feedback_signal),
        blocker_count + high_count + medium_count,
    )
    coherence = _score(
        5,
        int(valid_objectives) + int(not unresolved_dependencies) + int(bool(transitions)),
        blocker_count * 2 + high_count + medium_count,
    )

    fixes = tuple(sorted(
        findings,
        key=lambda item: (_SEVERITY_ORDER.get(item.severity, 99), item.check, item.summary),
    ))
    impact = tuple(
        {
            "severity": finding.severity,
            "source_check": finding.check,
            "impact": _unity_impact(finding.check),
            "correction": finding.recommended_correction,
        }
        for finding in fixes
        if finding.severity in {SEVERITY_BLOCKER, SEVERITY_HIGH}
    )
    decision = "FAIL" if blocker_count or high_count else "PASS"
    justification = (
        f"{decision}: simulated {max(0, int(max_seconds))} seconds; "
        f"{objective_count} objective(s), {completed_count} completable; "
        f"{blocker_count} blocker(s), {high_count} high-risk finding(s), "
        f"{medium_count} medium finding(s)."
    )

    risk_text = [finding.summary for finding in fixes if finding.severity != SEVERITY_LOW]
    evidence = [
        f"simulated_horizon={max(0, int(max_seconds))}s",
        f"objectives={objective_count}",
        f"transitions={len(transitions)}",
    ]
    return MechanicsPlayabilityReport(
        fun_score=_score_section(fun, evidence + (["control path declared"] if has_control else []), risk_text),
        friction_score=_score_section(friction, evidence, risk_text),
        pacing_score=_score_section(pacing, evidence + (["transition graph present"] if transitions else []), risk_text),
        difficulty_score=_score_section(difficulty, evidence, risk_text),
        readability_score=_score_section(readability, evidence, risk_text),
        systemic_coherence_score=_score_section(coherence, evidence, risk_text),
        dead_loop_detector=tuple(dead_loops),
        reward_loop_validation=tuple(reward_results),
        missing_feedback_detector=tuple(feedback_results),
        missing_affordance_detector=tuple(affordance_results),
        mission_flow_validation=tuple(mission_flow),
        playability_fixes=fixes,
        unity_ir_impact=impact,
        final_decision={
            "decision": decision,
            "justification": justification,
            "simulation_seconds": max(0, int(max_seconds)),
            "checks": sorted(set(all_checks)),
        },
    )


def _unity_impact(check: str) -> str:
    impacts = {
        "atom_validation": "Unity IR cannot faithfully represent an unregistered mechanic.",
        "mission_flow": "Generated objective trackers or event wiring may be unwinnable.",
        "dead_loop": "Generated state logic can trap the player without a completion edge.",
        "reward_loop": "Generated progression can complete without changing future play.",
        "missing_feedback": "Generated state changes can occur without readable player confirmation.",
        "missing_affordance": "Generated interactions can exist without a discoverable player action.",
        "systemic_coherence": "Generated components may reference systems that are absent or unresolved.",
    }
    return impacts.get(check, "The mechanics issue may survive into Unity IR and require manual repair.")


__all__ = [
    "MechanicsAtom",
    "MechanicsFinding",
    "MechanicsPlayabilityReport",
    "MechanicsPlaytestInput",
    "ScoreSection",
    "evaluate_mechanics",
    "normalize_atoms",
    "SEVERITY_LOW",
    "SEVERITY_MEDIUM",
    "SEVERITY_HIGH",
    "SEVERITY_BLOCKER",
]
