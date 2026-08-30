"""
Step 3.5 — LLM-authored per-micro code fields for --compose.

Why this exists
---------------
`MicroComposer` builds each micro's schema directly from `Subsystem.options`
(see unity_compose._resolve) and never calls the LLM, so every composed micro
is a pure function of (kind, variant, structural options). Two different
prompts that classify to the same genre produce byte-identical
`Assets/Micros/**/*.cs` — the concept never reaches the subsystem layer.

The standalone micro pipelines don't have this problem: their Step 3 asks the
LLM to author typed C# fragments (`extra_fields`, `extra_methods`,
`update_extra`, ...) which the templates render at named injection points.
The fragments live IN the schema, so Step 6 stays a pure function of it.

This module carries that same mechanism into compose. Two suppliers, one sink:

  B (primary)  `enrich_subsystems` — one focused LLM call per subsystem,
               reusing the micro's own extras contract. Each micro gets a
               dedicated prompt window seeded with the project concept, which
               is what actually recreates the brief.

  A (fallback) `merge_authored_code` — reads `schema.microgens[].input`, an
               already-LLM-authored, previously-unconsumed slot on the project
               schema (only `.atoms` was read). Zero extra LLM calls; fills
               any subsystem B left empty (B disabled, call failed, or the
               model returned nothing for that kind).

And the same step for the OTHER sink, at the bottom of this file:

  `enrich_scripts` — Step 3.5a, for `schema.scripts`. A `type: custom` script's
               template is a bare shell (it renders extra_fields and the extra_*
               bodies and declares nothing else), so with no supplier it emitted
               a class of fields with no behaviour and attached it to a live
               GameObject. Measured on CodeBranchSim: 14 authored method blocks
               across the micros, 0 across its four custom scripts, because
               everything here only ever walked subsystems. Unlike the two
               above it runs whether or not `--compose` is on, since scripts are
               generated either way.

Determinism contract
--------------------
Nothing here touches codegen. Both suppliers only ever write into
`Subsystem.options`, which is schema input — Steps 4-6 stay a pure function of
their inputs, and byte-identical reproducibility for a fixed subsystem list is
preserved (tests/test_unity_golden_reproducibility.py).

Critically, enrichment can never move a GUID or a scene fileID: those derive
from (project_name, kind, class_name) and the subsystem's ordinal, and
`class_name` comes from `schema.name`. `CODE_KEYS` deliberately excludes
`name`/`variant`/`target` so a model can never desynchronise
`MicroComposer.plan()` from `emit()`. The wiring stays deterministic; only the
bodies gain prompt-specific content.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

from pydantic import ValidationError
from rich.console import Console

from .micro import REGISTRY
from pathlib import Path

from .micro.code_fields import (UNITY_EXTRA_INSTRUCTIONS, bind_authored_code,
                                build_missing_fields, find_unbound_identifiers,
                                lint_method, lint_unbound_identifiers,
                                sanitize_default, template_members,
                                _promote_list_default_to_array,
                                template_method_arities, template_usings,
                                _KNOWN_SYMBOLS, _MONO_INHERITED, _method_name,
                                _signature_param_names,
                                _code_brace_depth, _code_paren_depth)

# ── What may be written ───────────────────────────────────────────────────────

# The ONLY keys enrichment may merge into Subsystem.options. Everything else a
# model returns is dropped. See the determinism contract above for why
# name/variant/target are not — and must never be — in this tuple.
CODE_KEYS: tuple[str, ...] = (
    "extra_usings",
    "extra_fields",
    "awake_extra",
    "update_extra",
    "fixed_update_extra",
    "extra_methods",
)

# Not every micro supports the full set: `CodeExtensibleSchema` subclasses take
# all six, while animator/lighting/shader/statemachine/trigger/vfx declare only
# the three declaration-level keys (no lifecycle bodies), and input/model take
# none. Asking a model for a key its schema would reject just burns tokens and
# produces a validation drop, so the vocabulary is computed per kind.


def code_keys_for(kind: str) -> tuple[str, ...]:
    """The subset of CODE_KEYS this micro's schema actually declares.

    Derived from the live schema rather than a hardcoded table, so a micro that
    gains injection points later is picked up with no change here.
    """
    cls = REGISTRY.get(kind)
    schema_cls = getattr(cls, "schema_cls", None)
    if schema_cls is None:
        return ()
    fields = getattr(schema_cls, "model_fields", {})
    return tuple(k for k in CODE_KEYS if k in fields)


def _has_code(options: dict) -> bool:
    """True when a subsystem already carries authored code (so the A fallback
    leaves it alone). An empty list/string counts as absent — a supplier that
    returned nothing usable should not block the other one."""
    return any(options.get(k) for k in CODE_KEYS)


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(kind: str, variant: str, name: str, base: dict,
              candidate: dict) -> tuple[dict, list[str]]:
    """Keep only the candidate keys that survive the micro's own schema.

    A model that emits `extra_fields` as a dict, or a `CodeMethod` missing its
    signature, would otherwise raise ValidationError inside
    `MicroComposer._resolve` and abort the whole build. Trying the batch first
    keeps the common (all-valid) path to a single construction; on failure each
    key is retried alone so one bad fragment costs only itself.

    Note this validates but does not lint — `default_fix`/`lint_extensible`
    still run downstream in `_resolve`, which is where prose, Python-shaped
    fragments and duplicate methods get stripped (and logged).
    """
    cls = REGISTRY.get(kind)
    schema_cls = getattr(cls, "schema_cls", None)
    if schema_cls is None or not candidate:
        return {}, []

    fields = getattr(schema_cls, "model_fields", {})
    fixed: dict[str, Any] = {}
    if "name" in fields and name:
        fixed["name"] = name
    if "variant" in fields and variant:
        fixed["variant"] = variant

    def _build(extra: dict) -> None:
        schema_cls(**fixed, **base, **extra)

    try:
        _build(candidate)
        return dict(candidate), []
    except (ValidationError, TypeError, ValueError):
        pass

    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in candidate.items():
        try:
            _build({**kept, key: value})
            kept[key] = value
        except (ValidationError, TypeError, ValueError):
            dropped.append(key)
    msgs = ([f"micro-code: dropped invalid {kind} key(s) {', '.join(dropped)}"]
            if dropped else [])
    return kept, msgs


def _filter_keys(kind: str, raw: object) -> dict:
    """Narrow an arbitrary LLM/JSON payload to this micro's code vocabulary."""
    if not isinstance(raw, dict):
        return {}
    allowed = code_keys_for(kind)
    return {k: v for k, v in raw.items() if k in allowed and v not in (None, "", [], {})}


# ── Unresolvable collaborator types (CS0246) ─────────────────────────────────
# Asked for concept-specific behavior, a model reaches for a collaborator class
# that does not exist. Confirmed live on a "deep-sea diver / oxygen" brief: the
# composed controller came back with
#   [SerializeField] private OxygenManager _oxygenManager;
# and an extra_method calling `_oxygenManager.Consume(drain)`. Nothing named
# OxygenManager exists, so the micro fails CS0246 and takes the assembly with it.
#
# unity_generator solves this for prompt-authored scripts by backfilling an empty
# stub class (_collect_custom_type_stubs) — but it deliberately refuses to stub
# `*Manager`/`*System`/`*Controller`/`*Handler` names, since those are meant to be
# real separate scripts, which is exactly the shape a model reaches for here. The
# micro templates emit no stubs at all, so for this path the field has to go.
#
# Deliberately narrow: only the fields whose declared TYPE cannot resolve are
# dropped, then only the lines referencing those now-undeclared names. That has
# no false positives — the identifiers were removed moments earlier in the same
# pass — unlike guessing at undeclared identifiers in general, which the
# incomplete per-micro `_RESERVED_FIELD_NAMES` sets cannot support.
from .micro.code_fields import _neutralize_lines
from .micro.code_fields import lint_underscore_alias_references
from .micro.code_fields import required_usings_for
from .micro.code_fields import jagged_rank_corrections
from .micro.code_fields import correct_qualified_type
from .micro.code_fields import lint_invalid_member_uses
from .micro.code_fields import (template_method_params,
                                template_field_types)
from .unity_generator import (_CS_BUILTIN_TYPES, _COMPOSED_CLASS_API,
                              _extract_base_type, _make_composed_call_stripper,
                              _strip_invented_constructors, _strip_builtin_call_arity)

_FRAGMENT_KEYS = ("awake_extra", "update_extra", "fixed_update_extra")


def _neutralize_refs(text: str, names: set[str]) -> str:
    """Comment out statements referencing a dropped field. Reuses the lint
    layer's own shared walk (_neutralize_lines) rather than a second copy: a
    bare `if (...)` header whose brace opens on a later line must have its
    CONDITION disabled, never be commented out, or the guarded block silently
    becomes unconditional -- and if that header's body is itself the very
    next (brace-less) line and also references a dropped name, that body must
    not be blanked to a bare comment, or the disabled header is left with no
    statement at all (CS1525/CS1002).
    """
    if not text or not names:
        return text
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")

    def make_comment(_names: list, line: str) -> str:
        return (f"// AUDIT: removed reference to unresolvable "
                f"collaborator type -- {line.strip()}")

    result, _flagged, _skipped = _neutralize_lines(
        text, lambda _i, line: pattern.findall(line), make_comment)
    return result


# A method SIGNATURE names types too, and the field-only check above walked
# straight past them. Hit live on a TurboToys run, EnemySpawner.cs(106,44) and
# (116,34):
#
#     private void ApplyRubberBandLogic(List<EnemyController> rivals, float p)
#     private void OnRivalFinished(EnemyController rival)
#
# — CS0246 twice for a collaborator class the project never generates. Same
# defect as the field case, same remedy, one declaration site further along:
# the parameter and return types are declarations exactly like a field's, and
# neither has a repair (inventing the class would ship a stub nothing drives).
# Matched against the same two allowlists, so a signature naming only real
# Unity/BCL types or a real sibling micro is never touched.
_SIG_PARAMS_RE = re.compile(r"\((.*)\)", re.DOTALL)
# Modifiers that precede the return type; never types themselves.
_SIG_MODIFIERS = frozenset({
    "public", "private", "protected", "internal", "static", "virtual",
    "override", "sealed", "async", "extern", "new", "unsafe", "partial",
    "readonly", "event", "ref", "out", "in", "params", "this",
})


def _split_params(params: str) -> list[str]:
    """Split a parameter list on the commas that SEPARATE parameters — the ones
    inside `Dictionary<string, int>` belong to the type, and splitting on them
    leaves two fragments that look like neither a type nor a name."""
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return out


def _signature_type_names(signature: str) -> set[str]:
    """Every type a method signature declares — return type and parameters.

    Deliberately shallow: a token that is not plainly `<type> <name>` is skipped
    rather than guessed at, so this can flag a type that is genuinely absent but
    never invent one that is not there.
    """
    if not signature:
        return set()
    params_match = _SIG_PARAMS_RE.search(signature)
    params = params_match.group(1) if params_match else ""
    head = signature[: params_match.start()] if params_match else signature

    names: set[str] = set()
    # The declared name is always the LAST token; everything before it, once the
    # modifiers are off, is the type. Joining rather than taking one token keeps
    # `Dictionary<string, EnemyController>` in one piece — a whitespace split
    # cuts it at the generic's own comma and loses the type that matters.
    head_tokens = [t for t in re.split(r"\s+", head.strip()) if t]
    while head_tokens and head_tokens[0].lower() in _SIG_MODIFIERS:
        head_tokens.pop(0)
    if len(head_tokens) >= 2:
        names.add(_extract_base_type(" ".join(head_tokens[:-1])))
    for raw in _split_params(params):
        tokens = [t for t in re.split(r"\s+", raw.strip()) if t
                  and t.lower() not in _SIG_MODIFIERS]
        if len(tokens) >= 2:                     # "<type> <name>"
            names.add(_extract_base_type(" ".join(tokens[:-1])))
    return {n for n in names if n and re.match(r"^[A-Za-z_]\w*$", n)}


def _drop_unresolvable_types(kind: str, candidate: dict,
                             known_types: frozenset[str]) -> tuple[dict, list[str]]:
    """Strip fields and methods whose declared types cannot resolve, plus every
    reference to what was removed."""
    fields = candidate.get("extra_fields")
    if not isinstance(fields, list):
        fields = []

    kept_fields: list = []
    dropped: set[str] = set()
    for f in fields:
        decl = (f.get("type") if isinstance(f, dict) else getattr(f, "type", "")) or ""
        name = (f.get("name") if isinstance(f, dict) else getattr(f, "name", "")) or ""
        base = _extract_base_type(str(decl))
        if base and base not in known_types and base not in _CS_BUILTIN_TYPES:
            dropped.add(str(name))
            continue
        kept_fields.append(f)

    # Methods whose signature names a type nothing in the project defines.
    methods_in = candidate.get("extra_methods")
    kept_methods: list = []
    bad_types: set[str] = set()
    if isinstance(methods_in, list):
        for m in methods_in:
            sig = (m.get("signature") if isinstance(m, dict)
                   else getattr(m, "signature", "")) or ""
            unresolved = {t for t in _signature_type_names(str(sig))
                          if t not in known_types and t not in _CS_BUILTIN_TYPES}
            if unresolved:
                bad_types |= unresolved
                name_m = re.search(r"\b([A-Za-z_]\w*)\s*\(", str(sig))
                if name_m:
                    # The method is gone; calls to it would be CS0103 next.
                    dropped.add(name_m.group(1))
                continue
            kept_methods.append(m)

    if not dropped:
        return candidate, []

    out = dict(candidate)
    if fields:
        out["extra_fields"] = kept_fields
    if isinstance(methods_in, list):
        out["extra_methods"] = kept_methods
    for key in _FRAGMENT_KEYS:
        if isinstance(out.get(key), str):
            out[key] = _neutralize_refs(out[key], dropped)
    methods = out.get("extra_methods")
    if isinstance(methods, list):
        surviving = []
        for m in methods:
            if not isinstance(m, dict):
                surviving.append(m)
                continue
            body = _neutralize_refs(str(m.get("body") or ""), dropped)
            # A method left with nothing but AUDIT comments is dead weight.
            if any(ln.strip() and not ln.lstrip().startswith("//")
                   for ln in body.splitlines()):
                surviving.append({**m, "body": body})
        out["extra_methods"] = surviving
    detail = (f" ({', '.join(sorted(bad_types))})" if bad_types else "")
    return out, [f"micro-code: dropped {kind} member(s) {', '.join(sorted(dropped))} "
                 f"— unresolvable collaborator type{detail} (CS0246)"]


def _apply(sub, candidate: dict, known_types: frozenset[str] = frozenset()) -> list[str]:
    """Validate `candidate` against `sub`'s schema and merge what survives."""
    if not candidate:
        return []
    candidate, msgs = _drop_unresolvable_types(sub.kind, candidate, known_types)
    kept, validation_msgs = _validate(sub.kind, sub.variant or "", sub.name or "",
                                      dict(sub.options), candidate)
    if kept:
        sub.options.update(kept)
    return msgs + validation_msgs


def _would_not_bind(sub, payload: dict) -> list[str]:
    """What the binder WOULD cut out of this payload, without cutting it.

    The diagnostics already exist and are precise — `bind_authored_code` reports
    `OnObjectiveComplete(0 args, needs 1..1)` — they have simply never been shown
    to the author. Run the same pass over a throwaway copy so the model can be
    told what is wrong while it can still fix it.

    Returns [] when everything binds. Never raises: a checker that can fail the
    build is worse than one that occasionally has nothing to say.
    """
    tdir = _template_dir_for(sub)
    if tdir is None:
        return []
    cls = REGISTRY.get(sub.kind)
    schema_cls = getattr(cls, "schema_cls", None)
    if schema_cls is None:
        return []
    try:
        # A REAL schema instance, not a bare carrier: `bind_authored_code` reads
        # `.signature`/`.body` off CodeMethod objects, and handing it the raw
        # JSON dicts makes every check silently inspect nothing and report a
        # clean bill of health. It mutates what it is given, which is exactly
        # why this is a throwaway built from the payload rather than `sub`.
        fields = getattr(schema_cls, "model_fields", {})
        fixed: dict[str, Any] = {}
        if "name" in fields and sub.name:
            fixed["name"] = sub.name
        if "variant" in fields and sub.variant:
            fixed["variant"] = sub.variant
        probe = schema_cls(**fixed, **dict(sub.options), **payload)
        return [m for m in bind_authored_code(probe, tdir)
                if "neutralized" in m or "dropped" in m]
    except Exception:                       # noqa: BLE001
        return []


def _project_types(subsystems: Sequence[Any]) -> frozenset[str]:
    """Class names this project generates — a field typed after a sibling micro
    (e.g. `DiverHealth`) resolves at compile time and must not be dropped."""
    return frozenset(str(s.name) for s in subsystems if getattr(s, "name", ""))


# ── Supplier A — schema.microgens[].input (no extra LLM calls) ────────────────

def merge_authored_code(subsystems: Sequence[Any],
                        microgens: Iterable[Any] | None) -> list[str]:
    """Fill empty subsystems from `schema.microgens[].input`, matched by kind.

    `MicrogensSpec.input` is a free-form `dict[str, Any]` the LLM already fills
    during the project schema step; until now only `.atoms` was consumed
    (capability_resolver.resolve_microgens). Entries whose `kind` is not a
    micro (e.g. "intro_sequence") are ignored here — they remain valid atom
    bundles.

    Only subsystems with no authored code yet are touched, so this genuinely
    acts as a fallback behind `enrich_subsystems` rather than overwriting it.
    """
    if not microgens:
        return []

    by_kind: dict[str, dict] = {}
    for entry in microgens:
        kind = str(getattr(entry, "kind", "") or "").strip().lower()
        if kind not in REGISTRY:
            continue
        payload = _filter_keys(kind, getattr(entry, "input", None))
        if payload:
            by_kind.setdefault(kind, payload)

    if not by_kind:
        return []

    msgs: list[str] = []
    filled = 0
    known = _project_types(subsystems)
    for sub in subsystems:
        authored = by_kind.get(sub.kind)
        if not authored or _has_code(sub.options):
            continue
        applied = _apply(sub, authored, known)
        msgs.extend(applied)
        if _has_code(sub.options):
            filled += 1
    if filled:
        msgs.insert(0, f"micro-code: filled {filled} subsystem(s) from "
                       f"schema.microgens[].input (fallback, no LLM call)")
    return msgs


def sync_microgens_from_subsystems(subsystems: Sequence[Any],
                                   microgens: list) -> list[str]:
    """Write each subsystem's current CODE_KEYS content back into `microgens`
    (a `schema.microgens` list), matched by kind — the write half of the loop
    `merge_authored_code` reads.

    Call this AFTER `enrich_subsystems`/`merge_authored_code` have both run,
    so whatever code ended up on `Subsystem.options` (LLM-authored this run,
    or carried over from a prior `schema.microgens[].input`) gets persisted
    into the schema that `write()` stamps as `<project>.schema.json`.
    `Subsystem.options` itself is never serialized — `enrich_subsystems`
    mutates it in memory only (see this module's docstring) — so without this,
    a later no-LLM recompile (`unity-validate --compose`) can only recover
    whatever `schema.microgens` held BEFORE this call, which is the
    schema-authoring step's output, not `enrich_subsystems`'.

    Keyed by kind, same granularity `merge_authored_code`'s own `by_kind`
    lookup uses — two subsystems that share a kind share one `microgens`
    entry, same limitation the read side already has.
    """
    by_kind = {m.kind: m for m in microgens}
    msgs: list[str] = []
    synced = 0
    for sub in subsystems:
        code = {k: sub.options[k] for k in CODE_KEYS if sub.options.get(k)}
        if not code:
            continue
        existing = by_kind.get(sub.kind)
        if existing is not None:
            existing.input = {**existing.input, **code}
        else:
            from .unity_schema import MicrogensSpec
            entry = MicrogensSpec(kind=sub.kind, input=code)
            microgens.append(entry)
            by_kind[sub.kind] = entry
        synced += 1
    if synced:
        msgs.append(f"micro-code: synced {synced} subsystem(s)' authored code "
                    f"into schema.microgens[].input (round-trip for "
                    f"unity-validate --compose)")
    return msgs


def sanitize_subsystem_composed_calls(subsystems: Sequence[Any],
                                      composed_units: list | None) -> list[str]:
    """Strip calls a subsystem's authored code makes to an invalid member of a
    composed sibling class — the same check `_sanitize_composed_component_calls`
    (src/unity_generator.py) already applies to `schema.scripts` (`type:
    custom`), extended to cover `schema.microgens`.

    A subsystem's extra_methods/awake_extra/update_extra/fixed_update_extra
    are authored by `enrich_subsystems` in complete isolation — that LLM call
    never sees the real generated source of any other micro, only its own
    kind's prompt. It can still reach a composed sibling exactly like a custom
    script does (`GameContext.Instance.Get<PlayerHealth>().Die()`), and
    nothing validated that path before this: `lint_extensible`
    (src/micro/code_fields.py) checks a subsystem's authored code against its
    OWN template's members, never against another composed class's verified
    public API.

    Must run AFTER `composer.plan(subsystems)` produces `composed_units` (the
    class names are not known before that) and BEFORE
    `sync_microgens_from_subsystems` persists `Subsystem.options` into
    `schema.microgens[].input` — otherwise the schema written to disk would
    disagree with what actually got rendered.

    `Subsystem.options` is a plain dict of JSON-shaped values (not the typed
    CodeMethod/CodeField models `schema.scripts` uses — see `_apply`/
    `_validate`'s docstrings), so `extra_methods` here is a list of dicts with
    a `"body"` key, not objects with a `.body` attribute.
    """
    strip = _make_composed_call_stripper(composed_units)
    if strip is None:
        return []
    flagged: set[str] = set()
    touched = 0
    for sub in subsystems:
        opts = sub.options
        changed = False
        for key in _FRAGMENT_KEYS:
            before = opts.get(key)
            if not isinstance(before, str) or not before:
                continue
            after = strip(before)
            if after != before:
                opts[key] = after
                changed = True
        methods = opts.get("extra_methods")
        if isinstance(methods, list):
            new_methods = []
            for m in methods:
                if not isinstance(m, dict):
                    new_methods.append(m)
                    continue
                body = m.get("body")
                if isinstance(body, str) and body:
                    stripped = strip(body)
                    if stripped != body:
                        m = {**m, "body": stripped}
                        changed = True
                new_methods.append(m)
            if changed:
                opts["extra_methods"] = new_methods
        if changed:
            touched += 1
            flagged.add(sub.kind)
    msgs: list[str] = []
    if touched:
        msgs.append(f"micro-code: stripped statement(s) referencing invalid "
                    f"composed-sibling-class member(s) from {touched} "
                    f"subsystem(s) (CS1061/CS0122): {', '.join(sorted(flagged))}")
    return msgs


def sanitize_subsystem_invented_constructors(subsystems: Sequence[Any],
                                             composed_units: list | None) -> list[str]:
    """Strip a subsystem's `new T(...)` construction of a type nobody
    declares anywhere — the same check `_sanitize_invented_constructor_calls`
    (src/unity_generator.py) already applies to `schema.scripts` (`type:
    custom`), extended to cover `schema.microgens` for the same reason
    `sanitize_subsystem_composed_calls` does: `enrich_subsystems` authors each
    subsystem's extra_* fields in isolation, with no upstream check against
    what types actually exist in this project.

    `known_types` here is deliberately narrower than the custom-script call
    site's (composed micro class names only, no sibling `schema.scripts`
    names) — the same scope `sanitize_subsystem_composed_calls` already uses
    for the identical reason: a subsystem cannot address a custom script by
    name any other way either.
    """
    known_types = frozenset(u.class_name for u in (composed_units or []))
    flagged: set[str] = set()
    touched = 0
    for sub in subsystems:
        opts = sub.options
        changed = False
        for key in _FRAGMENT_KEYS:
            before = opts.get(key)
            if not isinstance(before, str) or not before:
                continue
            after = _strip_invented_constructors(before, known_types)
            if after != before:
                opts[key] = after
                changed = True
        methods = opts.get("extra_methods")
        if isinstance(methods, list):
            new_methods = []
            for m in methods:
                if not isinstance(m, dict):
                    new_methods.append(m)
                    continue
                body = m.get("body")
                if isinstance(body, str) and body:
                    stripped = _strip_invented_constructors(body, known_types)
                    if stripped != body:
                        m = {**m, "body": stripped}
                        changed = True
                new_methods.append(m)
            if changed:
                opts["extra_methods"] = new_methods
        if changed:
            touched += 1
            flagged.add(sub.kind)
    msgs: list[str] = []
    if touched:
        msgs.append(f"micro-code: stripped statement(s) constructing an invented "
                    f"type from {touched} subsystem(s) (CS0246): "
                    f"{', '.join(sorted(flagged))}")
    return msgs


def sanitize_subsystem_builtin_call_arity(subsystems: Sequence[Any]) -> list[str]:
    """Strip a subsystem's GetComponent-family call (or a sibling —
    GetComponents/*InChildren/*InParent/TryGetComponent) made with an
    argument count that method's real overloads don't have — the same check
    `_sanitize_builtin_component_call_arity` (src/unity_generator.py) already
    applies to `schema.scripts` (`type: custom`), extended to cover
    `schema.microgens` for the same isolation reason
    `sanitize_subsystem_composed_calls` documents: `enrich_subsystems`
    authors each subsystem in isolation, so nothing upstream ever checked
    this path either. See `_MB_METHOD_ARITY` for the overload shapes."""
    flagged: set[str] = set()
    touched = 0
    for sub in subsystems:
        opts = sub.options
        changed = False
        for key in _FRAGMENT_KEYS:
            before = opts.get(key)
            if not isinstance(before, str) or not before:
                continue
            after = _strip_builtin_call_arity(before)
            if after != before:
                opts[key] = after
                changed = True
        methods = opts.get("extra_methods")
        if isinstance(methods, list):
            new_methods = []
            for m in methods:
                if not isinstance(m, dict):
                    new_methods.append(m)
                    continue
                body = m.get("body")
                if isinstance(body, str) and body:
                    stripped = _strip_builtin_call_arity(body)
                    if stripped != body:
                        m = {**m, "body": stripped}
                        changed = True
                new_methods.append(m)
            if changed:
                opts["extra_methods"] = new_methods
        if changed:
            touched += 1
            flagged.add(sub.kind)
    msgs: list[str] = []
    if touched:
        msgs.append(f"micro-code: stripped GetComponent-family call(s) with an "
                    f"invalid argument count from {touched} subsystem(s) "
                    f"(CS1501): {', '.join(sorted(flagged))}")
    return msgs


# ── Supplier B — one focused LLM call per subsystem ───────────────────────────

# UNITY_EXTRA_INSTRUCTIONS is written for str.format() (its JSON braces are
# doubled). Nothing here is formatted — the concept text would break it — so
# unescape once at import.
_EXTRAS_CONTRACT = UNITY_EXTRA_INSTRUCTIONS.replace("{{", "{").replace("}}", "}")

_SYSTEM = (
    "You are a senior Unity gameplay engineer. You are handed ONE subsystem of "
    "a larger generated project and asked for the small set of C# fragments "
    "that make it specific to this project's concept. "
    "Output ONLY valid JSON — no explanation, no markdown fences."
)

# Options that describe generated content rather than tuning; showing them
# wastes context (some carry hundreds of pre-extracted dialog lines or cues).
_NOISY_OPTIONS = {"meta", "cues", "lines", "clips", "palette_rgb", "lights"}


def _structural_summary(options: dict, limit: int = 700) -> str:
    lean = {k: v for k, v in options.items()
            if k not in _NOISY_OPTIONS and k not in CODE_KEYS}
    try:
        text = json.dumps(lean, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(lean)
    return text[:limit]


def _template_dir_for(sub) -> "Path | None":
    """Where this subsystem's own templates live, or None."""
    cls = REGISTRY.get(sub.kind)
    subdir = getattr(cls, "template_subdir", None) if cls else None
    if not subdir:
        return None
    d = Path("templates/unity/micro") / str(subdir)
    return d if d.is_dir() else None


def _contract_for(sub, *, tags: Sequence[str] = ()) -> str:
    """What this class ACTUALLY offers the author.

    The compiler has always computed this — `template_members`,
    `template_method_arities`, `template_usings` — and has only ever used it to
    JUDGE the model's output. The prompt meanwhile asserted the opposite:

        "Do not assume a property such as IsSprinting, CurrentSpeed or
         IsGrounded exists on this class."

    while Controller.cs.j2 declares `CurrentSpeed` and `IsGrounded` outright.
    The model was told the class was empty, wrote code accordingly, and had
    anything it did reach for deleted for not binding. Showing it the same table
    the judge uses is the whole of this fix.
    """
    tdir = _template_dir_for(sub)
    if tdir is None:
        return ""
    try:
        arities  = template_method_arities(str(tdir))
        members  = template_members(str(tdir))
        usings   = template_usings(str(tdir))
        # Ground truth the compiler has always had and never showed the author.
        # Names and arity alone leave every TYPE to be guessed, and the guesses
        # are wrong in the ordinary way: `SetScore(1 arg)` says nothing about
        # `long`, so the model passes a float (CS1503), and a bare field list
        # says nothing about `SaveTime` being a string, so it subtracts a
        # string from a float (CS0019). Both hit live on the same build.
        params   = template_method_params(str(tdir))
        ftypes   = template_field_types(str(tdir))
    except Exception:                       # noqa: BLE001 — never block authoring
        return ""
    if not members:
        return ""

    callable_names = sorted(arities)
    plain = sorted(m for m in members if m not in arities)

    lines = ["", "WHAT THIS CLASS ALREADY HAS — you may call these directly"]
    if callable_names:
        lines.append("  callable (exact signature — argument COUNT and TYPE "
                     "both have to match):")
        for n in callable_names[:40]:
            sig = params.get(n)
            if sig is not None:
                lines.append(f"    {n}({', '.join(sig)})")
                continue
            lo, hi = arities[n]
            span = str(lo) if lo == hi else f"{lo}..{hi}"
            lines.append(f"    {n}({span} arg{'' if hi == 1 else 's'})")
        lines.append("    Calling one of these with the wrong number of "
                     "arguments will not compile — an event declared "
                     "`Action<string>` needs exactly one. Passing the wrong "
                     "TYPE will not compile either: pass a long where a long "
                     "is declared, and never do arithmetic on a string field.")
    if plain:
        lines.append("  fields / properties (name: declared type):")
        lines.append("    " + ", ".join(
            f"{m}: {ftypes[m]}" if m in ftypes else m for m in plain[:40]))
    if usings:
        lines.append("  namespaces already imported: " + ", ".join(sorted(usings)))
    if tags:
        lines.append("  tags THIS PROJECT defines (no others exist — "
                     "GameObject.FindGameObjectWithTag THROWS on an undefined "
                     "tag): " + ", ".join(sorted(tags)))
    return "\n".join(lines)


def _user_prompt_for(sub, *, concept: str, user_prompt: str,
                     keys: tuple[str, ...], contract: str = "") -> str:
    cls = REGISTRY.get(sub.kind)
    role = getattr(cls, "display_name", sub.kind.title())
    return f"""\
PROJECT CONCEPT
{concept.strip()[:1800]}

ORIGINAL BRIEF
{user_prompt.strip()[:900]}

THE SUBSYSTEM YOU ARE EXTENDING
  kind:       {sub.kind} ({role})
  variant:    {sub.variant or "default"}
  class name: {sub.name or sub.kind.title()}
  structural settings already decided: {_structural_summary(sub.options)}
{contract}

The baseline behavior for this variant is ALREADY generated by the compiler —
do not restate movement, input plumbing, or lifecycle scaffolding. Author only
what makes THIS subsystem belong to THIS project, and only where it is this
subsystem's job: the {sub.kind} micro owns its own concern, not the whole game.

THE CODE MUST COMPILE. Everything available to you is listed above:
- Call the members shown under WHAT THIS CLASS ALREADY HAS, with the argument
  counts given. Anything NOT listed there you must declare yourself in
  extra_fields before you use it.
- Every type you name must be a Unity or C# built-in (float, Vector3,
  AudioSource, ParticleSystem, Light, List<T>, ...) or a class listed above.
  A helper class of your own (OxygenManager, StatsSystem) has nothing to define
  it — model that state with plain fields on this class instead.
- Use only the namespaces listed as already imported, or name what you need in
  extra_usings.
{_EXTRAS_CONTRACT}

Return ONE JSON object containing only these keys: {", ".join(keys)}
"""


# Per-call completion ceiling for a micro-code request. The answer is a small
# JSON of C# fragments — a few fields and methods — so a few thousand tokens is
# generous. It must NOT inherit the client-wide budget (config sets 50000 for
# the schema step): a model that runs away then generates until the 2600s client
# timeout instead of until a cap, and this loop retries `attempts` times, so one
# bad subsystem costs three full timeouts. Measured live on
# qwopus3.5-9b-coder-exp: 1h54m burned on a single subsystem, silently.
_MAX_TOKENS = 6000

# Live progress channel. `msgs` is only printed AFTER this function returns, so
# a per-attempt failure appended there stays invisible for exactly as long as
# the stall it is reporting.
console = Console()


def enrich_subsystems(subsystems: Sequence[Any], *, llm, concept: str,
                      user_prompt: str = "", temperature: float = 0.35,
                      attempts: int = 3, tags: Sequence[str] = ()) -> list[str]:
    """Ask the LLM for prompt-specific C# fragments, one subsystem at a time.

    One focused call per subsystem rather than one bloated call for all of them:
    the local model gets the same narrow window the standalone micro pipelines
    give it (which is the path that demonstrably produces prompt-shaped code),
    and a truncated or refused response costs one subsystem instead of the whole
    project.

    MUST be called while the LLM is still warm — ahead of any Blender/Chromium
    phase — or LM Studio's idle-unload turns every call into a swallowed
    failure. See the Step 6.5 ordering note in unity_pipeline.run().

    Never raises: a failed subsystem is logged and left for `merge_authored_code`
    to fill.
    """
    if not concept:
        return ["micro-code: no concept available — skipped LLM enrichment"]

    msgs: list[str] = []
    enriched = 0
    skipped_kinds: list[str] = []
    known = _project_types(subsystems)

    for sub in subsystems:
        keys = code_keys_for(sub.kind)
        if not keys:
            skipped_kinds.append(sub.kind)
            continue

        contract = _contract_for(sub, tags=tags)
        payload: dict = {}
        correction = ""
        for attempt in range(attempts):
            try:
                raw = llm.chat(
                    system=_SYSTEM,
                    user=_user_prompt_for(sub, concept=concept,
                                          user_prompt=user_prompt, keys=keys,
                                          contract=contract) + correction,
                    temperature=temperature if attempt == 0 else min(temperature, 0.15),
                    thinking=False,
                    max_tokens=_MAX_TOKENS,
                )
                candidate = _filter_keys(sub.kind, llm.extract_json(raw))
                if not candidate:
                    continue
                payload = candidate

                # Close the loop. Anything the binder would cut is handed BACK
                # as a diagnostic while the author can still fix it — the whole
                # point being that a mechanic the model meant to write survives
                # as working code instead of as an AUDIT comment. Only the last
                # attempt is allowed to reach the binder uncorrected. Structural
                # errors (unbalanced braces/parens) get the same treatment — a
                # method can be fully bound (every name it uses is declared) and
                # still fail to parse, which the binder alone never catches.
                if attempt + 1 >= attempts:
                    break
                cuts = _would_not_bind(sub, payload)
                struct_errors = _method_structural_errors(payload)
                if not cuts and not struct_errors:
                    break
                sections = []
                if cuts:
                    sections.append(
                        "The compiler reports:\n"
                        + "\n".join("  - " + c for c in cuts) +
                        "\nExpress it with the members listed above, declaring "
                        "anything else you need in extra_fields.")
                if struct_errors:
                    sections.append(
                        "These methods do not parse as valid C# (unbalanced "
                        "braces/parentheses):\n"
                        + "\n".join("  - " + e for e in struct_errors) +
                        "\nRewrite them with EVERY if/else/while/for header you "
                        "intend, one opening brace per closing brace.")
                correction = (
                    "\n\nYOUR PREVIOUS ANSWER DOES NOT COMPILE AS WRITTEN.\n"
                    + "\n\n".join(sections) +
                    "\nRewrite the SAME behaviour so it binds and parses — keep "
                    "what you were trying to do. Do not drop the feature."
                )
                msgs.append(f"micro-code: {sub.kind} re-authored after "
                            f"{len(cuts) + len(struct_errors)} "
                            f"binding/structural error(s)")
            except Exception as exc:  # noqa: BLE001 — one micro must not abort the build
                # Logged on EVERY attempt, not just the last. A silent retry
                # loop is how a stalled subsystem became invisible: three
                # timeouts in a row printed nothing for 1h54m, so the run
                # looked hung rather than failing. Name the attempt as it dies.
                console.print(f"  [yellow]micro-code: {sub.kind} attempt "
                              f"{attempt + 1}/{attempts} failed[/] "
                              f"({type(exc).__name__}: {str(exc)[:160]})")
                if attempt + 1 >= attempts:
                    msgs.append(f"micro-code: {sub.kind} enrichment failed "
                                f"({type(exc).__name__}: {exc}) — fallback applies")

        if not payload:
            continue
        msgs.extend(_apply(sub, payload, known))
        if _has_code(sub.options):
            enriched += 1

    if enriched:
        msgs.insert(0, f"micro-code: LLM authored prompt-specific C# for "
                       f"{enriched}/{len(subsystems)} subsystem(s)")
    else:
        msgs.insert(0, "micro-code: LLM authored nothing usable — "
                       "composed micros stay at their deterministic baseline")
    if skipped_kinds:
        msgs.append("micro-code: no code-field support in "
                    + ", ".join(sorted(set(skipped_kinds))))
    return msgs


# ── Prompt-authored SCRIPTS (schema.scripts) ─────────────────────────────────
#
# `enrich_subsystems` above authors bodies for composed MICROS. Nothing ever did
# the same for `schema.scripts`, and a `type: custom` script's template
# (templates/unity/scripts/CustomBehaviour.cs.j2) is a bare shell — it renders
# `extra_fields` and every `extra_*` body and declares nothing else. So a custom
# script with empty bodies emits a class with fields and NO behaviour, attached
# to a live GameObject.
#
# Measured on CodeBranchSim: 14 prompt-specific method blocks across the micros,
# 0 across the four custom scripts. Their schema entries came back
# `"awake_extra": "", "extra_methods": []` — the generator was faithful, the
# fields simply had no producer. Step 3 declares the class; there was no Step
# 3.5 for it.
#
# Only `custom` is enriched. Every other script type renders a template that
# already carries its behaviour (GameManager.cs.j2, SceneAudio.cs.j2, ...), and
# injecting an `update_extra` into a class that has its own Update loop is a
# different and riskier change than filling an empty shell.
#
# Containment is NOT rebuilt here: unity_generator already runs a battery of
# sanitizers over `script.awake_extra` / `update_extra` / `fixed_update_extra` /
# `extra_methods[].body` (_sanitize_project_service_calls,
# _strip_undeclared_foreach, _strip_undeclared_self_reference, ...). This adds
# the supplier those passes have always been waiting for.

_SCRIPT_SYSTEM = (
    "You are a senior Unity gameplay engineer. You are handed ONE script from a "
    "larger generated project — a bare MonoBehaviour with nothing in it but the "
    "fields listed — and asked for the C# that makes it do its job. "
    "Output ONLY valid JSON — no explanation, no markdown fences."
)


# BEHAVIOUR, not declarations. `_has_code` tests all of CODE_KEYS, which is
# right for a micro because its options dict is empty until something authors
# it. A script is different: Step 3 fills `extra_fields`/`extra_usings` for
# nearly every custom script and leaves every body empty — the exact shape this
# step exists to fix — so testing the full vocabulary would skip precisely the
# scripts that need enriching. Kept in step with
# unity_playability._SCRIPT_BEHAVIOUR_KEYS
# (tests/test_script_code_enrichment.py pins the two together).
_BEHAVIOUR_KEYS: tuple[str, ...] = _FRAGMENT_KEYS + ("extra_methods",)


def _script_has_code(script) -> bool:
    """True when this script already has a BODY, so enrichment leaves it alone.
    Declared fields and usings are not behaviour — a class of fields that does
    nothing is the defect, not evidence against it.

    Checks for actual non-whitespace content, not just a truthy container —
    a Step 3 schema-fill that hands a script `extra_methods=[{"body": ""}]`
    (a signature with an empty/whitespace body) reads as "has code" under a
    plain truthiness check even though it authors nothing. That script then
    skips this pass's LLM-authoring + bind-retry loop entirely, and if a
    downstream dead-method sweep later drops the empty method, the script
    reaches codegen as a silent hollow shell — enrichment never got the
    chance to give it real behaviour, and nothing named it on the way there.
    Live: VectorCore's SurfaceModifier (locomotion-mode stress prompt)."""
    for key in _FRAGMENT_KEYS:
        if (getattr(script, key, "") or "").strip():
            return True
    return any((getattr(m, "body", "") or "").strip()
               for m in (getattr(script, "extra_methods", None) or []))


def _script_contract(script, siblings: Sequence[Any] = (),
                     composed_subsystems: Sequence[Any] = ()) -> str:
    """What this script really has, and what it may really reach for.

    The same lesson as `_contract_for`: the compiler has always known this and
    only ever used it to JUDGE the answer. A custom script's surface is small
    and exact — MonoBehaviour, its own serialized fields, the PUBLIC methods of
    its sibling custom scripts (the only cross-script access
    `unity_generator._sanitize_project_service_calls` lets through — a
    sibling's extra_fields always render `[SerializeField] private`, so
    reaching one is CS0122 from anywhere else), and the verified public API of
    whichever composed micros this project actually has (`_COMPOSED_CLASS_API`
    — the same maximal-per-kind table `_sanitize_project_service_calls` already
    uses to strip a call it can't verify). Confirmed live: OxygenManager called
    `Get<PlayerHealth>().Die()` (private, CS0122), InventoryManager called
    `Get<WorldInteractable>().CanPickup()`/`.ItemName` (neither exists, CS1061
    x2) — both classes the model could see existed but never the shape of.
    """
    lines = ["", "WHAT THIS CLASS ALREADY HAS"]
    fields = [f"{getattr(f, 'type', '?')} {getattr(f, 'name', '?')}"
              for f in (getattr(script, "extra_fields", None) or [])]
    lines.append("  serialized fields: " + (", ".join(fields) if fields
                                            else "none — declare what you need"))
    lines.append("  everything MonoBehaviour gives you (transform, gameObject, "
                 "GetComponent<T>, StartCoroutine, Instantiate, Destroy, ...)")

    reachable: list[str] = []
    for sib in siblings:
        if sib is script or getattr(sib, "type", "") != "custom":
            continue
        public = [str(getattr(m, "signature", "") or "").strip()
                  for m in (getattr(sib, "extra_methods", None) or [])
                  if str(getattr(m, "signature", "") or "").strip().startswith("public")]
        if public:
            reachable.append(f"  GameContext.Instance.Get<{sib.name}>() -> "
                             + "; ".join(public))
    if reachable:
        lines.append("")
        lines.append("OTHER SCRIPTS YOU MAY CALL (public methods only)")
        lines.extend(reachable)

    # Only classes this project actually composes, and only the ones with a
    # verified table entry — an unlisted composed kind (e.g. EnemyAI, VFX) has
    # no ground truth here and stays unmentioned rather than guessed at, same
    # restraint `_composed_member_ok` applies at judge time.
    composed_names = sorted({str(getattr(s, "name", "") or "")
                             for s in composed_subsystems} & _COMPOSED_CLASS_API.keys())
    if composed_names:
        lines.append("")
        lines.append("COMPOSED PROJECT SYSTEMS YOU MAY CALL (verified public API "
                     "only — anything not listed here does not exist on that "
                     "class and will be stripped)")
        for name in composed_names:
            members = sorted(_COMPOSED_CLASS_API[name])
            surface = ", ".join(members) if members else \
                "no public members beyond MonoBehaviour basics"
            lines.append(f"  GameContext.Instance.Get<{name}>() -> {surface}")
    return "\n".join(lines)


def _brief_job_for(script, user_prompt: str) -> str:
    """The brief's OWN description of this script, in full.

    `script.description` is a twice-shortened echo by the time it reaches here:
    the Step-3 checklist showed a capped slice of the brief, and Step 3 then
    wrote its own paraphrase of that. Meanwhile the "ORIGINAL BRIEF" block below
    is a blind head-slice of the prompt, and a brief states its SCRIPTS near the
    END — so on the Gridfall brief the author saw the title and part of the
    style guide and never the section describing the mechanic it was told to
    implement. It wrote nothing, and the built game had no game in it.

    Returns "" when the brief declares no such script (a vibe prompt), leaving
    the schema's description as the only spec, exactly as before.
    """
    try:
        from .unity_concept_scripts import extract_named_scripts
        name = str(getattr(script, "name", "") or "").strip().lower()
        for s in extract_named_scripts(user_prompt or ""):
            if str(s.get("name", "")).strip().lower() == name:
                text = " ".join(str(s.get("description") or "").split())
                # The bullet's own title/description separator survives the
                # parse, and "its job: - owns the grid" reads as a fragment.
                return text.lstrip("-–— ").strip()
    except Exception:                      # noqa: BLE001 — never block authoring
        pass
    return ""


def _script_user_prompt(script, *, concept: str, user_prompt: str,
                        keys: tuple[str, ...], contract: str) -> str:
    brief_job = _brief_job_for(script, user_prompt)
    job = brief_job or script.description
    return f"""\
PROJECT CONCEPT
{concept.strip()[:1800]}

ORIGINAL BRIEF
{user_prompt.strip()[:900]}

THE SCRIPT YOU ARE WRITING
  class name:  {script.name}
  its job:     {job}
  attached to: {getattr(script, "attach_to", "systems")}
{contract}

This class is EMPTY apart from the fields above — there is no baseline behaviour
to avoid restating. Write what the description says it does, and nothing that is
another script's job.

THE CODE MUST COMPILE. Everything available to you is listed above:
- Anything not listed you must declare yourself in extra_fields before using it.
- Every type you name must be a Unity or C# built-in (float, Vector3,
  AudioSource, Light, List<T>, ...) or a script listed above. A helper class of
  your own has nothing to define it — model that state with plain fields here.
- Use only UnityEngine, or name what you need in extra_usings.
{_EXTRAS_CONTRACT}

Return ONE JSON object containing only these keys: {", ".join(keys)}
"""


def _script_would_not_bind(script, payload: dict) -> list[str]:
    """Identifiers in this payload the generator's sanitizers would strip.

    A custom script has no micro template to check against, so the scope is
    built directly: MonoBehaviour, the known-symbol allowlist, the fields it
    declares (existing and proposed), and its own method names.
    """
    def _get(row, key):
        return str((row or {}).get(key, "") if isinstance(row, dict)
                   else getattr(row, key, "") or "")

    try:
        known = set(_MONO_INHERITED) | set(_KNOWN_SYMBOLS) | {script.name}
        for f in (getattr(script, "extra_fields", None) or []):
            known.add(str(getattr(f, "name", "") or ""))
        for f in (payload.get("extra_fields") or []):
            known.add(_get(f, "name"))
        methods = payload.get("extra_methods") or []
        sigs = [_get(m, "signature") for m in methods]
        for sig in sigs:
            hit = re.search(r"\b([A-Za-z_]\w*)\s*\(", sig)
            if hit:
                known.add(hit.group(1))

        cuts: list[str] = []
        for key in _FRAGMENT_KEYS:
            body = str(payload.get(key) or "")
            if body:
                for names in find_unbound_identifiers(body, known=known).values():
                    cuts.extend(names)
        for row, sig in zip(methods, sigs):
            body = _get(row, "body")
            if body:
                # A method's own parameters are in scope for its own body —
                # `signature` alone only feeds argument TYPE inference.
                for names in find_unbound_identifiers(
                        body, known=known | _signature_param_names(sig),
                        signature=sig).values():
                    cuts.extend(names)
        # A name whose underscored twin IS declared is not a correction for the
        # model to make — `_bind_script` rewrites `grid` to `_grid`
        # deterministically. Asking anyway burns a retry on a solved problem.
        cuts = [n for n in cuts if f"_{n}" not in known]
        # Deduplicated and capped: a correction listing forty names teaches the
        # model less than one listing six.
        return sorted(set(cuts))[:6]
    except Exception:                      # noqa: BLE001 — never block authoring
        return []


def _method_structural_errors(payload: dict) -> list[str]:
    """Candidate `extra_methods` bodies that would fail `lint_method`'s
    structural checks (unbalanced parens, or more '}' than '{') -- checked
    here, ahead of the SAME check `_apply_script` runs at the very end, so a
    structurally broken candidate gets a correction round like a binding
    error does, instead of going straight to a silent drop. Confirmed live:
    GossipHub's OnGossipSelected had zero unbound identifiers (every name it
    used was validly declared) but was missing two `if`/`else if` headers,
    orphaning their bodies and a closing brace -- `_script_would_not_bind`
    saw nothing wrong, so the retry loop accepted it on the first try and
    the only recourse left was `_apply_script` cutting the whole method.
    """
    from .unity_schema import CodeMethod
    errors: list[str] = []
    for row in payload.get("extra_methods") or []:
        try:
            m = row if isinstance(row, CodeMethod) else CodeMethod(**row)
        except (ValidationError, TypeError):
            continue
        body = m.body or ""
        if not body.strip():
            continue
        pdepth = _code_paren_depth(body)
        if pdepth != 0:
            errors.append(f"{m.signature}: unbalanced parentheses ({pdepth:+d})")
            continue
        bdepth = _code_brace_depth(body)
        if bdepth < 0:
            errors.append(f"{m.signature}: more closing '}}' than opening '{{' "
                          f"— likely a missing if/else/while/for header whose "
                          f"body and closing brace you kept")
    return errors[:6]


def _apply_script(script, payload: dict) -> bool:
    """Assign the surviving payload onto the script.

    Pydantic validates on assignment, but that only checks field SHAPE (is
    `body` a string?) — not C# validity. An `extra_methods` body is additionally
    run through `lint_method`, the same structural lint `bind_authored_code`
    already applies to every micro subsystem: it rejects an over-closed
    fragment (more `}` than `{`) outright rather than let it reach codegen,
    because a surplus `}` closes the enclosing class early and takes every
    member after it down (CS8803/CS0106/CS1022). Confirmed live: GossipHub.cs
    on a grandma run shipped exactly that shape — `lint_code_fragment` already
    rejects it, this script path just never called it, unlike the micro path.
    """
    from .unity_schema import CodeField, CodeMethod

    wrote = False
    for key in _FRAGMENT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            setattr(script, key, value)
            wrote = True
    lint_taken: set[str] = set()
    for key, model in (("extra_fields", CodeField), ("extra_methods", CodeMethod)):
        rows = payload.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        built = []
        for row in rows:
            try:
                candidate = row if isinstance(row, model) else model(**row)
            except (ValidationError, TypeError):
                continue
            if key == "extra_methods":
                assert isinstance(candidate, CodeMethod)  # model was CodeMethod in this branch
                linted = lint_method(candidate, taken_names=lint_taken)
                if linted is None:
                    continue
                candidate = linted
            else:
                # extra_fields never routed through sanitize_default the way
                # a micro's lint_fields does — confirmed live,
                # DamageReceiver.cs(11,142): `private Vector3 _knockbackForce
                # = 0f, 0f, 0f;` (CS1001/CS1003, C# reads the bare comma list
                # as three separate declarators). Custom scripts (schema.scripts)
                # never call lint_fields at all, so this was the only gap left.
                assert isinstance(candidate, CodeField)  # model was CodeField in this branch
                # A list of literals in a SCALAR field's initializer is an
                # array the model typed as one value — retyped rather than
                # dropped, so all of its lines survive. Same defect class as
                # the Vector3 case above, one step further: four fields of
                # intercom dialogue came through as
                # `private string _introLines = "…","…","…";` and stopped the
                # build at the syntax gate (live: QuartermasterBrain.cs).
                _promote_list_default_to_array(candidate)
                fixed = sanitize_default(candidate.type, candidate.default)
                if fixed != candidate.default:
                    if fixed is None:
                        print(f"  FIX  dropped uncompilable initializer "
                              f"{candidate.default!r} on `{candidate.type} "
                              f"{candidate.name}` (custom script)")
                    else:
                        print(f"  FIX  repaired initializer on `{candidate.type} "
                              f"{candidate.name}`: {candidate.default!r} -> "
                              f"{fixed!r} (custom script)")
                    candidate.default = fixed or ""
            built.append(candidate)
        if not built:
            continue
        existing = list(getattr(script, key, None) or [])
        taken = {getattr(x, "name", None) or getattr(x, "signature", None)
                 for x in existing}
        fresh = [b for b in built
                 if (getattr(b, "name", None) or getattr(b, "signature", None))
                 not in taken]
        if fresh:
            setattr(script, key, existing + fresh)
            wrote = True
    usings = payload.get("extra_usings")
    if isinstance(usings, list) and usings:
        have = list(getattr(script, "extra_usings", None) or [])
        script.extra_usings = have + [u for u in usings
                                      if isinstance(u, str) and u not in have]
    return wrote


def _bind_script(script) -> list[str]:
    """Post-apply binding pass for a custom script: DECLARE any identifier the
    author used but never defined (`build_missing_fields`), then NEUTRALIZE
    whatever is still unbound after that — the same two-tier "build before you
    cut" contract `bind_authored_code` already runs for every micro
    (`schema.microgens`), just never extended to `schema.scripts`.

    The retry loop's own bind-check (`_script_would_not_bind`) only ever runs
    on attempts BEFORE the last (`if attempt + 1 >= attempts: break` happens
    first) — there is nowhere left to retry to, so the final attempt is
    accepted unconditionally, unbound identifiers included. `_apply_script`
    only checks STRUCTURAL validity (`lint_method`/brace-balance), never
    identifier binding, so nothing catches this before codegen. Confirmed
    live: TeaBrewing.cs assigned `_onComplete = onComplete;` in `StartBrew()`
    — no `_onComplete` field was ever declared (a near-miss of the real one,
    `_brewCompleteEvent`) — CS0103, silently shipped.

    A custom script's template (`CustomBehaviour.cs.j2`) is a bare shell with
    no variant branches, so unlike `bind_authored_code` this needs no
    `template_scope_members` call — the known-symbol universe is just
    MonoBehaviour + `_KNOWN_SYMBOLS` + the script's own declared fields and
    method names, the same set `_script_would_not_bind` already computes for
    the retry loop. Returns fixer-log messages, same shape as
    `bind_authored_code`'s."""
    known = set(_MONO_INHERITED) | set(_KNOWN_SYMBOLS) | {script.name}
    for f in (getattr(script, "extra_fields", None) or []):
        known.add(str(getattr(f, "name", "") or ""))
    methods = list(getattr(script, "extra_methods", None) or [])
    for m in methods:
        known.add(_method_name(getattr(m, "signature", "") or ""))

    blob = "\n".join(
        [str(getattr(script, key, "") or "") for key in _FRAGMENT_KEYS]
        + [str(getattr(m, "body", "") or "") for m in methods]
    )
    wanted: set[str] = set()
    for key in _FRAGMENT_KEYS:
        for names in find_unbound_identifiers(
                getattr(script, key, "") or "", known=known).values():
            wanted |= names
    for m in methods:
        sig = getattr(m, "signature", "") or ""
        for names in find_unbound_identifiers(
                getattr(m, "body", "") or "",
                known=known | _signature_param_names(sig),
                signature=sig).values():
            wanted |= names

    # Rebind `grid` -> `_grid` BEFORE building or cutting anything. The author
    # declared the field and then referred to it without the underscore; the
    # other two repairs available here are both wrong on content — neutralizing
    # deletes the behaviour, and declaring a second `grid` field leaves a
    # collaborator that is never wired in the scene, so the script compiles and
    # does nothing. See lint_underscore_alias_references.
    rebound_all: set[str] = set()
    for key in _FRAGMENT_KEYS:
        before = getattr(script, key, "") or ""
        if not before:
            continue
        after, rebound = lint_underscore_alias_references(
            before, unbound=wanted, declared=known)
        if rebound:
            setattr(script, key, after)
            rebound_all |= rebound
    for m in methods:
        before = getattr(m, "body", "") or ""
        if not before:
            continue
        after, rebound = lint_underscore_alias_references(
            before, unbound=wanted, declared=known)
        if rebound:
            m.body = after
            rebound_all |= rebound
    if rebound_all:
        wanted -= rebound_all

    built = build_missing_fields(script, wanted, blob)
    if built:
        known |= {b.split()[-1] for b in built}

    # Invented members on real engine types (CS0117/CS1061). A custom script
    # has no template to check against, but its own extra_fields declare the
    # types, which is all this needs. No surface built -> no opinion.
    from .unity_api_surface import default_api_surface
    api_surface = default_api_surface()
    declared_types = {str(getattr(f, "name", "") or ""): str(getattr(f, "type", "") or "")
                      for f in (getattr(script, "extra_fields", None) or [])}

    flagged: set[str] = set()
    for key in _FRAGMENT_KEYS:
        before = getattr(script, key, "") or ""
        if not before:
            continue
        after, names = lint_unbound_identifiers(before, known=known)
        after, mnames = lint_invalid_member_uses(
            after, field_types=declared_types, surface=api_surface)
        names |= mnames
        if names:
            setattr(script, key, after)
            flagged |= names
    for m in methods:
        before = getattr(m, "body", "") or ""
        if not before:
            continue
        sig = getattr(m, "signature", "") or ""
        after, names = lint_unbound_identifiers(
            before, known=known | _signature_param_names(sig), signature=sig)
        after, mnames = lint_invalid_member_uses(
            after, field_types=declared_types, surface=api_surface)
        names |= mnames
        if names:
            m.body = after
            flagged |= names

    # Add the namespaces the authored body USES but never imported. Nothing
    # else does this: lint_usings only ever filters what the model asked for,
    # so `Array.Fill(...)` shipped without `using System;` and failed as
    # CS0103 — which reads like an invented symbol, not a missing import.
    added_usings: list[str] = []
    try:
        blob_all = "\n".join(
            [str(getattr(script, key, "") or "") for key in _FRAGMENT_KEYS]
            + [str(getattr(m, "body", "") or "") for m in methods]
        )
        have = {str(u).strip().rstrip(";").replace("using ", "")
                for u in (getattr(script, "extra_usings", None) or [])}
        for ns in required_usings_for(blob_all):
            if ns not in have:
                script.extra_usings = list(
                    getattr(script, "extra_usings", None) or []) + [ns]
                have.add(ns)
                added_usings.append(ns)
    except Exception:                      # noqa: BLE001 — never block codegen
        added_usings = []

    # The author declared a jagged array one rank deeper than their own code
    # then used it — see jagged_rank_corrections for why only the explicit
    # local-declaration signal is trusted.
    rank_fixes: dict[str, str] = {}
    try:
        declared_pairs = [(str(getattr(f, "name", "") or ""),
                           str(getattr(f, "type", "") or ""))
                          for f in (getattr(script, "extra_fields", None) or [])]
        rank_fixes = jagged_rank_corrections(declared_pairs, blob_all)
        for f in (getattr(script, "extra_fields", None) or []):
            fixed = rank_fixes.get(str(getattr(f, "name", "") or ""))
            if fixed:
                f.type = fixed
    except Exception:                      # noqa: BLE001 — never block codegen
        rank_fixes = {}

    # `UnityEngine.Touchscreen` -> `Touchscreen` + the namespace it really
    # lives in. See correct_qualified_type.
    ns_fixes: list[str] = []
    try:
        for f in (getattr(script, "extra_fields", None) or []):
            was = str(getattr(f, "type", "") or "")
            fixed, need_ns = correct_qualified_type(was)
            if not need_ns:
                continue
            f.type = fixed
            ns_fixes.append(f"{was} -> {fixed}")
            have_ns = {str(u).strip().rstrip(";").replace("using ", "")
                       for u in (getattr(script, "extra_usings", None) or [])}
            if need_ns not in have_ns:
                script.extra_usings = list(
                    getattr(script, "extra_usings", None) or []) + [need_ns]
    except Exception:                      # noqa: BLE001 — never block codegen
        ns_fixes = []

    msgs: list[str] = []
    if ns_fixes:
        msgs.append(f"script-code: {script.name} — REQUALIFIED type(s) written "
                    f"with the wrong namespace: " + ", ".join(ns_fixes))
    if rank_fixes:
        msgs.append(f"script-code: {script.name} — CORRECTED array rank on "
                    + ", ".join(f"{n} -> {ty}" for n, ty in sorted(rank_fixes.items()))
                    + " (the body's own declaration disagreed with the field's)")
    if added_usings:
        msgs.append(f"script-code: {script.name} — ADDED using(s) the body "
                    f"needs: {', '.join(added_usings)}")
    if rebound_all:
        msgs.append(f"script-code: {script.name} — REBOUND {len(rebound_all)} "
                    f"reference(s) to the field already declared for them: "
                    + ", ".join(f"{n} -> _{n}" for n in sorted(rebound_all)))
    if built:
        msgs.append(f"script-code: {script.name} — BUILT {len(built)} "
                    f"field(s) used but not declared: "
                    f"{', '.join(sorted(set(built)))}")
    if flagged:
        msgs.append(f"script-code: {script.name} neutralized "
                    f"{len(flagged)} unbindable reference(s): "
                    f"{', '.join(sorted(flagged))}")
    return msgs


def enrich_scripts(scripts: Sequence[Any], *, llm, concept: str,
                   user_prompt: str = "", temperature: float = 0.35,
                   attempts: int = 3, subsystems: Sequence[Any] = ()) -> list[str]:
    """Author bodies for `type: custom` scripts, one focused call each.

    Same shape as `enrich_subsystems`: a narrow window per script, the real
    contract shown up front, and one correction round when the answer would not
    bind. Never raises — a failed script costs that script, not the build.

    `subsystems` is the project's composed-micro list (empty when --compose is
    off, or when this runs before it — see unity_pipeline.run() for why it is
    computed ahead of this call) — only used to tell the contract which
    composed class names actually exist in THIS project.

    MUST be called while the LLM is still warm, for the same reason
    `enrich_subsystems` must: LM Studio's idle-unload turns every call into a
    swallowed failure once a Blender/Chromium phase has run.
    """
    if not concept:
        return ["script-code: no concept available — skipped LLM enrichment"]

    targets = [s for s in (scripts or [])
               if getattr(s, "type", "") == "custom" and not _script_has_code(s)]
    if not targets:
        return []

    msgs: list[str] = []
    enriched = 0
    for script in targets:
        contract = _script_contract(script, scripts, subsystems)
        payload: dict = {}
        correction = ""
        for attempt in range(attempts):
            try:
                raw = llm.chat(
                    system=_SCRIPT_SYSTEM,
                    user=_script_user_prompt(script, concept=concept,
                                             user_prompt=user_prompt,
                                             keys=CODE_KEYS,
                                             contract=contract) + correction,
                    temperature=temperature if attempt == 0 else min(temperature, 0.15),
                    thinking=False,
                    max_tokens=_MAX_TOKENS,
                )
                candidate = {k: v for k, v in (llm.extract_json(raw) or {}).items()
                             if k in CODE_KEYS and v not in (None, "", [], {})}
                if not candidate:
                    continue
                payload = candidate
                if attempt + 1 >= attempts:
                    break
                cuts = _script_would_not_bind(script, payload)
                struct_errors = _method_structural_errors(payload)
                if not cuts and not struct_errors:
                    break
                sections = []
                if cuts:
                    sections.append(
                        "These names are not declared anywhere:\n"
                        + "\n".join("  - " + c for c in cuts) +
                        "\nDeclare what you need in extra_fields.")
                if struct_errors:
                    sections.append(
                        "These methods do not parse as valid C# (unbalanced "
                        "braces/parentheses):\n"
                        + "\n".join("  - " + e for e in struct_errors) +
                        "\nRewrite them with EVERY if/else/while/for header you "
                        "intend, one opening brace per closing brace.")
                correction = (
                    "\n\nYOUR PREVIOUS ANSWER DOES NOT COMPILE AS WRITTEN.\n"
                    + "\n\n".join(sections) +
                    "\nRewrite the SAME behaviour so it binds and parses. Do "
                    "not drop the feature."
                )
                msgs.append(f"script-code: {script.name} re-authored after "
                            f"{len(cuts) + len(struct_errors)} "
                            f"binding/structural error(s)")
            except Exception as exc:       # noqa: BLE001 — one script must not abort
                # Logged per attempt for the same reason as enrich_subsystems:
                # a silent retry loop makes a stalled call look like a hang.
                console.print(f"  [yellow]script-code: {script.name} attempt "
                              f"{attempt + 1}/{attempts} failed[/] "
                              f"({type(exc).__name__}: {str(exc)[:160]})")
                if attempt + 1 >= attempts:
                    msgs.append(f"script-code: {script.name} enrichment failed "
                                f"({type(exc).__name__}: {exc}) — stays a shell")

        if payload and _apply_script(script, payload):
            enriched += 1
            msgs.extend(_bind_script(script))

    if enriched:
        msgs.insert(0, f"script-code: LLM authored behaviour for "
                       f"{enriched}/{len(targets)} custom script(s)")
    else:
        # Loud: a custom script with no body is a component attached to a live
        # GameObject that does nothing, and nothing downstream reports it.
        msgs.insert(0, f"script-code: LLM authored nothing usable — "
                       f"{len(targets)} custom script(s) stay empty shells")
    return msgs
