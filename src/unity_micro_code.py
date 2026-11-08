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
                                find_unbound_identifiers,
                                lint_method, sanitize_default, template_members,
                                _promote_list_default_to_array,
                                template_method_arities, template_usings,
                                _KNOWN_SYMBOLS, _MONO_INHERITED, _signature_param_names,
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


from .micro.code_fields import (template_method_params,
                                template_field_types)
from .unity_component_contract import (public_surface, render_own_class_surface)

_FRAGMENT_KEYS = ("awake_extra", "update_extra", "fixed_update_extra")


def _apply(sub, candidate: dict) -> list[str]:
    """Validate `candidate` shape and merge it without deleting collaborators.

    Member/type truth belongs to the selected-source contract and final C#
    diagnostics.  Earlier S1 code removed fields and methods merely because a
    local Python name list could not prove their collaborator type; that made
    intended gameplay disappear before the authoritative consumer could report
    it.
    """
    if not candidate:
        return []
    kept, validation_msgs = _validate(sub.kind, sub.variant or "", sub.name or "",
                                      dict(sub.options), candidate)
    if kept:
        sub.options.update(kept)
    return validation_msgs


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
    for sub in subsystems:
        authored = by_kind.get(sub.kind)
        if not authored or _has_code(sub.options):
            continue
        applied = _apply(sub, authored)
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


def _contract_for(sub, *, tags: Sequence[str] = (), component_api=None) -> str:
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
    if component_api is not None:
        try:
            lines = ["", "WHAT THIS CLASS ALREADY HAS — selected rendered declarations"]
            lines.append(render_own_class_surface(
                component_api, _selected_component_class(component_api, sub)))
            if tags:
                lines.append("  tags THIS PROJECT defines (no others exist — "
                             "GameObject.FindGameObjectWithTag THROWS on an undefined "
                             "tag): " + ", ".join(sorted(tags)))
            return "\n".join(lines)
        except ValueError:
            return ""
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


def _selected_component_class(component_api, sub) -> str:
    """Return the manifest-recorded attachment class for one composed subsystem."""
    selection = component_api.document.get("selection", [])
    if not selection:
        return str(getattr(sub, "name", "") or "")
    identity = {
        "kind": str(getattr(sub, "kind", "") or ""),
        "name": str(getattr(sub, "name", "") or ""),
        "target": str(getattr(sub, "target", "") or ""),
    }
    matches = [row for row in selection
               if all(str(row.get(key, "") or "") == value
                      for key, value in identity.items())]
    if len(matches) != 1:
        raise ValueError(
            "[component-api] selected attachment class resolves to "
            f"{len(matches)} manifest rows for {identity!r}")
    return str(matches[0]["class_name"])


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
                      attempts: int = 3, tags: Sequence[str] = (),
                      component_api=None) -> list[str]:
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

    for sub in subsystems:
        keys = code_keys_for(sub.kind)
        if not keys:
            skipped_kinds.append(sub.kind)
            continue

        contract = _contract_for(sub, tags=tags, component_api=component_api)
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
        msgs.extend(_apply(sub, payload))
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
# `extra_methods[].body` (_strip_undeclared_foreach,
# _sanitize_undeclared_self_calls, ...). This adds
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
                     composed_subsystems: Sequence[Any] = (),
                     component_api=None) -> str:
    """What this script really has, and what it may really reach for.

    The same lesson as `_contract_for`: the compiler has always known this and
    only ever used it to JUDGE the answer. A custom script's surface is small
    and exact — MonoBehaviour, its own serialized fields, the PUBLIC methods of
    its sibling custom scripts (a sibling's extra_fields always render
    `[SerializeField] private`, so reaching one is CS0122 from anywhere else),
    and the public API extracted from the selected rendered source of whichever
    composed micros this project actually has (`component_api`, the S2
    contract). Confirmed live: OxygenManager called
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

    # Only classes this project actually composes, read from the selected
    # rendered source; with no extracted contract nothing is guessed at.
    if component_api is not None and composed_subsystems:
        lines.append("")
        lines.append("COMPOSED PROJECT SYSTEMS YOU MAY CALL (selected public API only)")
        for sub in sorted(composed_subsystems, key=lambda item: str(getattr(item, "name", "") or "")):
            try:
                name = _selected_component_class(component_api, sub)
                members = public_surface(component_api, name)
            except ValueError:
                continue
            surface = "; ".join(members) or "no public declarations beyond real base types"
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


def enrich_scripts(scripts: Sequence[Any], *, llm, concept: str,
                   user_prompt: str = "", temperature: float = 0.35,
                   attempts: int = 3, subsystems: Sequence[Any] = (),
                   component_api=None) -> list[str]:
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
        contract = _script_contract(script, scripts, subsystems, component_api)
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

    if enriched:
        msgs.insert(0, f"script-code: LLM authored behaviour for "
                       f"{enriched}/{len(targets)} custom script(s)")
    else:
        # Loud: a custom script with no body is a component attached to a live
        # GameObject that does nothing, and nothing downstream reports it.
        msgs.insert(0, f"script-code: LLM authored nothing usable — "
                       f"{len(targets)} custom script(s) stay empty shells")
    return msgs
