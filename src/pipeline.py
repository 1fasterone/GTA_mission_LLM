"""
6-step GTA5 mission pipeline — fully synchronous.

STEP 2 — LLM generates mission concept (story, no code)
STEP 3 — LLM fills the mission schema (plain English descriptions)
         + location sanity check against concept keywords
STEP 4 — RAG resolves all descriptions → GTA5 model IDs
         + step↔entity consistency enforcement
STEP 5 — Python fixer + per-item LLM pick for UNRESOLVED models
STEP 6 — Pipeline compiles schema → .NET 4.8 C# (no LLM involved)
"""

import json
import re
from pathlib import Path

from rich.console import Console
from rich.panel   import Panel

from .lm_studio_client   import LMStudioClient
from .backends           import make_chat_client, make_rag_client
from .resolver           import ModelResolver, _load_lookups
from .mission_schema     import MissionSchema, MissionObject
from .code_generator     import CodeGenerator
from .prompts            import (
    CONCEPT_SYSTEM, CONCEPT_USER,
    SCHEMA_FILL_SYSTEM, SCHEMA_FILL_USER,
)
from .themes             import (
    resolve_theme, render_concept_bias, render_schema_hints,
)
from .archetypes         import (
    detect_archetype, render_concept_hint, render_structure_hint, apply_scaffold,
)
from .micro.code_fields  import (
    lint_usings, lint_fields, lint_code_fragment, lint_method, lint_hash_references,
    lint_member_references, lint_vector3_static_misuse, lint_enum_references,
    lint_bare_task_static_misuse,
)

# Names + namespaces the mission template already emits — LLM extras that
# collide get dropped so the generated C# always compiles.
_GTA_RESERVED_FIELDS = {
    "_state", "_step", "_lastblipstep", "_startblip", "_startready",
    "_peds", "_vehicles", "_props", "_blip", "_lapsdone", "_cpidx",
    "_stepstart", "spawnorigin", "waypoints",
    # HeliRace-style AI difficulty + config menu (see mission.cs.j2)
    "_aidifficultyidx", "_menuopen", "_menuselection",
    "_cfgweatheridx", "_cfgweatheroptions",
}
_GTA_TEMPLATE_USINGS = {
    "System", "System.Collections.Generic", "System.Windows.Forms",
    "GTA", "GTA.Math", "GTA.Native",
}


# ── Real GTA.Native.Hash members — catches LLM-invented natives (e.g. the
# hallucinated SET_MISSION_FAILED / SET_MISSION_PASSED that fail CS0117 at
# compile time) before they reach the generated .3.cs. Sourced from a real
# SHVDN3 audit tool's allowlist (6,460 native names); missing file → None,
# which makes lint_hash_references() a no-op rather than a hard failure.
def _load_hash_allowlist(lookups_dir: str = "lookups") -> "frozenset[str] | None":
    path = Path(lookups_dir) / "gta_hash_allowlist.json"
    if not path.exists():
        return None
    try:
        return frozenset(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None

_GTA_HASH_ALLOWLIST: "frozenset[str] | None" = _load_hash_allowlist()


# ── Real SHVDN3 member surface for a fixed set of commonly-referenced types —
# catches LLM-invented members (e.g. the confirmed Ped.GetNearestVehicle /
# Ped.SetPositionAndRotation, neither of which exist) the same way the hash
# allowlist catches invented natives. Sourced from the same SHVDN3 audit
# tool's reflected API extraction, trimmed to the types this checker
# understands. Missing file → None → lint_member_references() is a no-op.
def _load_api_surface(lookups_dir: str = "lookups") -> "dict | None":
    path = Path(lookups_dir) / "gta_api_surface.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

_GTA_API_SURFACE: "dict | None" = _load_api_surface()


# ── Real SHVDN3 enum members — catches LLM-invented enum values (e.g.
# Weather.Rain, which doesn't exist — the real member is Weather.Raining).
# Corpus-mined: the 4th most common real failure (133 occurrences) across a
# 1191-file independent dataset. No type inference needed — an enum name in
# `EnumType.Member` is unambiguous wherever it appears. Missing file → None
# → lint_enum_references() is a no-op.
def _load_enum_allowlist(lookups_dir: str = "lookups") -> "dict | None":
    path = Path(lookups_dir) / "gta_enum_allowlist.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

_GTA_ENUM_ALLOWLIST: "dict | None" = _load_enum_allowlist()


def _lint_mission_extras(schema: MissionSchema) -> None:
    """Defensively clean the LLM-authored mission-specific C# (in place)."""
    schema.extra_usings = lint_usings(schema.extra_usings, already_in_template=_GTA_TEMPLATE_USINGS)
    schema.extra_fields = lint_fields(schema.extra_fields, reserved=_GTA_RESERVED_FIELDS)
    schema.extra_init   = lint_code_fragment(schema.extra_init, what="Init() body")
    schema.extra_tick   = lint_code_fragment(schema.extra_tick, what="OnTick() body")
    taken: set[str] = set()
    cleaned = []
    for m in schema.extra_methods:
        m2 = lint_method(m, taken_names=taken)
        if m2 is not None:
            cleaned.append(m2)
    schema.extra_methods = cleaned

    # Neutralize any invented GTA.Native.Hash reference, invented C# member on
    # a known SHVDN3 type, invented enum value, Vector3.Distance/.Dot
    # instance-call misuse, or bare Task.X() static misuse — run last, after
    # the other passes have already balanced braces / capped lines.
    for field in ("extra_init", "extra_tick"):
        value = getattr(schema, field)
        value = lint_hash_references(value, allowed=_GTA_HASH_ALLOWLIST)
        value = lint_member_references(value, surface=_GTA_API_SURFACE)
        value = lint_enum_references(value, enums=_GTA_ENUM_ALLOWLIST)
        value = lint_vector3_static_misuse(value)
        value = lint_bare_task_static_misuse(value)
        setattr(schema, field, value)
    for m in schema.extra_methods:
        m.body = lint_hash_references(m.body, allowed=_GTA_HASH_ALLOWLIST)
        m.body = lint_member_references(m.body, surface=_GTA_API_SURFACE)
        m.body = lint_enum_references(m.body, enums=_GTA_ENUM_ALLOWLIST)
        m.body = lint_vector3_static_misuse(m.body)
        m.body = lint_bare_task_static_misuse(m.body)

console = Console()

_MODEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")

# ── FIX 3: Location keyword map ───────────────────────────────────────────────
# Used to detect when the LLM story describes location X but the schema
# resolves to location Y.

_LOCATION_KEYWORDS: dict[str, list[str]] = {
    "docks":         ["dock", "terminal", "cargo ship", "shipping containers", "crane"],
    "airport":       ["airport", "lsia", "runway", "hangar", "airfield", "tarmac"],
    "downtown":      ["downtown", "legion square", "maze bank", "city hall", "skyscraper"],
    "vinewood":      ["vinewood", "hills", "celebrity", "mansion", "observatory"],
    "sandy_shores":  ["sandy shores", "desert", "trailer park", "meth lab", "alamo sea"],
    "paleto_bay":    ["paleto", "paleto bay", "blaine county", "north yankton"],
    "alamo_sea":     ["alamo sea", "alamo", "lake", "dam"],
    "military_base": ["military", "fort zancudo", "army base", "air force", "soldiers",
                      "fort", "military facility", "base"],
    "prison":        ["prison", "bolingbroke", "penitentiary", "inmates", "yard", "cell"],
    "port":          ["port", "la mesa", "pier", "dockyard", "boats", "harbor"],
    "highway":       ["highway", "freeway", "interstate", "overpass", "motorway"],
}

# ── Step 5a: programmatic schema fixer ───────────────────────────────────────

def _programmatic_fix(schema: MissionSchema) -> MissionSchema:
    schema.weather = schema.weather.upper()
    for ped in schema.peds:
        ped.description = ped.description.strip()
        ped.count = max(1, min(8, ped.count))
    for v in schema.vehicles:
        v.description = v.description.strip()
        v.count = max(1, min(4, v.count))
    for o in schema.objects:
        o.description = o.description.strip()
    # Clamp racing/timer step params to sane ranges
    for step in schema.steps:
        if step.type == "race_lap":
            lc = step.params.get("lap_count", 3)
            try:
                step.params["lap_count"] = max(1, min(20, int(lc)))
            except (TypeError, ValueError):
                step.params["lap_count"] = 3
        if step.type == "timer":
            sec = step.params.get("seconds", 120)
            try:
                step.params["seconds"] = max(10, min(600, int(sec)))
            except (TypeError, ValueError):
                step.params["seconds"] = 120
    # Clean any LLM-authored mission-specific C# fragments
    _lint_mission_extras(schema)
    return schema


# ── Step 5b: LLM model picker (one call per UNRESOLVED item) ─────────────────

def _llm_pick_model(llm: LMStudioClient, description: str,
                    category: str, candidates: list[str]) -> str:
    candidate_str = "\n".join(f"  {m}" for m in candidates[:30])
    system = (
        "You are a GTA5 asset lookup tool. "
        "Reply with ONLY a single model ID from the list — no explanation."
    )
    user = (
        f"Which GTA5 {category} model best matches: \"{description}\"?\n\n"
        f"Choose from:\n{candidate_str}\n\n"
        "Reply with the single best model ID only."
    )
    try:
        raw = llm.chat(system=system, user=user, temperature=0.05, thinking=False)
    except Exception as exc:
        fallback = candidates[0] if candidates else "UNRESOLVED"
        console.print(f"  [yellow]WARN[/] LLM pick failed for '{description}' ({exc!r}) — using {fallback}")
        return fallback
    match = re.search(r"\b([a-z][a-z0-9_]{2,})\b", raw.strip().lower())
    candidate_set = set(candidates)
    if match and match.group(1) in candidate_set:
        return match.group(1)
    fallback = candidates[0] if candidates else "UNRESOLVED"
    console.print(f"  [yellow]WARN[/] LLM pick returned no valid ID for '{description}' — using {fallback}")
    return fallback


# ── Pipeline class ────────────────────────────────────────────────────────────

class MissionPipeline:
    def __init__(self, config: dict):
        lm_cfg   = config["lm_studio"]
        llm_cfg  = config["anythingllm"]
        pipe_cfg = config["pipeline"]

        self.llm = make_chat_client(config)
        self.rag = make_rag_client(config)
        lookups_dir = pipe_cfg.get("lookups_dir", "lookups")
        self.resolver = ModelResolver(
            rag         = self.rag,
            lookups_dir = lookups_dir,
            fallback    = llm_cfg.get("fallback_to_local", True),
            use_rag     = not config.get("basic_mode", False) and self.rag is not None,
        )
        self.lookups = _load_lookups(lookups_dir)
        self.generator = CodeGenerator(
            template_file = pipe_cfg.get("template_file", "templates/mission.cs.j2")
        )
        self.output_dir   = pipe_cfg.get("output_dir", "output")
        self.show_prompts = config.get("logging", {}).get("show_prompts", False)
        self.concept_temp = lm_cfg.get("concept_temperature", 0.85)
        self.schema_temp  = lm_cfg.get("schema_temperature", 0.2)

    # ── console helpers ───────────────────────────────────────────────────────

    def _step(self, n: int, title: str) -> None:
        console.print(f"\n[bold cyan]STEP {n}[/] -- {title}")

    def _ok(self, msg: str) -> None:
        console.print(f"  [green]OK[/] {msg}")

    def _warn(self, msg: str) -> None:
        console.print(f"  [yellow]WARN[/] {msg}")

    def _fix(self, msg: str) -> None:
        console.print(f"  [magenta]FIX[/] {msg}")

    # ── FIX 3: location sanity check ─────────────────────────────────────────

    def _check_location(self, concept: str, schema: MissionSchema) -> MissionSchema:
        """
        Compare keywords in the concept against the resolved location_name.
        If the story clearly describes a different location, auto-correct it.
        """
        concept_lower = concept.lower()
        resolved = schema.location_name

        # Score each location by how many of its keywords appear in the concept
        scores: dict[str, int] = {}
        for loc, keywords in _LOCATION_KEYWORDS.items():
            scores[loc] = sum(1 for kw in keywords if kw in concept_lower)

        best_loc  = max(scores, key=lambda l: scores[l])
        best_score = scores[best_loc]

        if best_score == 0:
            return schema   # no strong signal — trust the LLM

        if best_loc != resolved:
            self._warn(
                f"Location mismatch: concept signals '{best_loc}' "
                f"but schema says '{resolved}'"
            )
            self._fix(f"Correcting location_name: '{resolved}' -> '{best_loc}'")
            schema.location_name = best_loc
        else:
            self._ok(f"Location '{resolved}' matches concept keywords")

        return schema

    # ── FIX 2: step↔entity consistency ───────────────────────────────────────

    def _check_consistency(self, schema: MissionSchema) -> MissionSchema:
        """
        Enforce that every step type has the entities it needs to be completable.
        Auto-adds sensible defaults rather than letting the mission get stuck.
        """
        step_types = {s.type for s in schema.steps}

        # PICKUP needs at least one resolved pickup prop
        if "pickup" in step_types:
            pickup_props = [
                o for o in schema.objects
                if o.role == "pickup" and o.model and o.model != "UNRESOLVED"
            ]
            if not pickup_props:
                self._warn("PICKUP step exists but no pickup prop was resolved")
                self._fix("Adding prop_drug_package_01 as default pickup")
                schema.objects.append(MissionObject(
                    description = "generic package",
                    model       = "prop_drug_package_01",
                    role        = "pickup",
                ))

        # KILL_ALL needs at least one ped
        if "kill_all" in step_types and not schema.peds:
            self._warn("KILL_ALL step exists but no peds defined — mission cannot complete")

        # GET_IN_VEHICLE needs at least one vehicle
        if "get_in_vehicle" in step_types and not schema.vehicles:
            self._warn("GET_IN_VEHICLE step but no vehicles defined — adding burrito")
            from .mission_schema import MissionVehicle
            schema.vehicles.append(MissionVehicle(
                description = "van",
                model       = "burrito",
                role        = "escape",
                count       = 1,
            ))

        # CHECKPOINT / RACE_LAP → must have at least one vehicle
        racing_steps = {s.type for s in schema.steps} & {"checkpoint", "race_lap"}
        if racing_steps:
            from .mission_schema import MissionVehicle
            all_roles = {v.role for v in schema.vehicles}
            if not schema.vehicles:
                self._warn("RACE_LAP/CHECKPOINT step but no vehicles — adding sultan as race_opponent")
                schema.vehicles.append(MissionVehicle(
                    description="sports car", model="sultan", role="race_opponent", count=1))
            elif "race_opponent" not in all_roles:
                self._warn("RACE_LAP/CHECKPOINT but no race_opponent vehicle — promoting first vehicle")
                schema.vehicles[0].role = "race_opponent"

        # RACE_LAP / TIMER — default missing params
        from .code_generator import _TRACKS
        from .archetypes import track_from_text
        valid_tracks = set(_TRACKS.keys())
        intent_text = " ".join(filter(None, [
            str(schema.meta.get("user_prompt", "")), str(schema.meta.get("concept", "")),
        ]))
        for step in schema.steps:
            if step.type in ("race_lap", "checkpoint"):
                track = str(step.params.get("track", "")).strip()
                # Qualify shorthand ("figure_8" → "airport.figure_8") for the check.
                qualified = track if "." in track else f"{schema.location_name}.{track}"
                if not track or qualified not in valid_tracks:
                    inferred = track_from_text(intent_text, schema.location_name,
                                               valid_tracks=valid_tracks)
                    self._fix(f"{step.type} track {track!r} missing/invalid — using {inferred!r} from intent")
                    step.params["track"] = inferred
            if step.type == "race_lap" and not step.params.get("lap_count"):
                self._fix("race_lap missing lap_count — defaulting to 3")
                step.params["lap_count"] = 3
            if step.type == "timer" and not step.params.get("seconds"):
                self._fix("timer missing seconds — defaulting to 120")
                step.params["seconds"] = 120

        # DELIVER needs at least one preceding go_to (sets the blip via SetBlip(0) in Init).
        # If none exists, _blip is always null and the step can never complete.
        if "deliver" in step_types:
            deliver_indices = [i for i, s in enumerate(schema.steps) if s.type == "deliver"]
            goto_indices    = [i for i, s in enumerate(schema.steps) if s.type == "go_to"]
            for di in deliver_indices:
                if not any(gi < di for gi in goto_indices):
                    self._warn(f"DELIVER step (index {di}) has no preceding go_to — _blip will be null")
                    self._fix("Inserting go_to step before DELIVER so the blip is initialised")
                    from .mission_schema import MissionStep as _MS
                    schema.steps.insert(di, _MS(type="go_to", label="Go to the delivery point"))
                    # Recompute indices after insertion
                    deliver_indices = [i for i, s in enumerate(schema.steps) if s.type == "deliver"]
                    goto_indices    = [i for i, s in enumerate(schema.steps) if s.type == "go_to"]

        return schema

    # ── Step 5b: resolve UNRESOLVED models with LLM pick ────────────────────

    def _fix_unresolved(self, schema: MissionSchema) -> MissionSchema:
        for ped in schema.peds:
            if not ped.model or ped.model == "UNRESOLVED":
                candidates = self.lookups.get("peds", {}).get("valid_models", [])
                if candidates:
                    ped.model = _llm_pick_model(self.llm, ped.description, "ped", candidates)
                    self._fix(f"'{ped.description}' -> {ped.model}")

        for v in schema.vehicles:
            if not v.model or v.model == "UNRESOLVED":
                candidates = self.lookups.get("vehicles", {}).get("valid_models", [])
                if candidates:
                    v.model = _llm_pick_model(self.llm, v.description, "vehicle", candidates)
                    self._fix(f"'{v.description}' -> {v.model}")

        for o in schema.objects:
            if not o.model or o.model == "UNRESOLVED":
                candidates = self.lookups.get("objects", {}).get("valid_models", [])
                if candidates:
                    o.model = _llm_pick_model(self.llm, o.description, "prop", candidates)
                    self._fix(f"'{o.description}' -> {o.model}")

        return schema

    # ── main run ──────────────────────────────────────────────────────────────

    def run(self, user_prompt: str,
            theme:       str | None = None,
            mashup_with: str | None = None,
            seed:        int | None = None) -> Path:
        console.print(Panel(
            f"[bold]GTA5 Mission Pipeline[/]\n{user_prompt}", style="blue"
        ))

        # Fixed seed → reproducible LLM generations (same seed the theme RNG uses).
        if seed is not None:
            self.llm.seed = seed

        # Resolve theme (may be None → behaves exactly as before)
        active_theme, rng, theme_meta = resolve_theme(
            user_prompt = user_prompt,
            theme_name  = theme,
            mashup_with = mashup_with,
            seed        = seed,
        )
        if active_theme:
            theme_block, twist = render_concept_bias(active_theme, rng=rng)
            schema_hints       = render_schema_hints(active_theme)
            theme_meta["twist_seed"] = twist
            self._ok(f"Theme: {active_theme.display}")
            if twist:
                self._ok(f"Twist seed: {twist}")
        else:
            theme_block, schema_hints = "", ""

        # Detect the gameplay archetype (race/heist/dive/fly/assault/chase) so the
        # mission gets a real genre structure, not a generic step list. Orthogonal
        # to themes (which only shape setting). None → behaves exactly as before.
        archetype       = detect_archetype(user_prompt)
        archetype_block = render_concept_hint(archetype)
        structure_block = render_structure_hint(archetype)
        if archetype:
            theme_meta["archetype"] = archetype.name
            self._ok(f"Archetype: {archetype.display}")

        # STEP 2 — mission concept ────────────────────────────────────────────
        self._step(2, "LLM generates mission concept")
        concept = self.llm.chat(
            system      = CONCEPT_SYSTEM,
            user        = CONCEPT_USER.format(
                user_prompt=user_prompt, theme_block=theme_block,
                archetype_block=archetype_block,
            ),
            temperature = self.concept_temp,
            thinking    = True,
        )
        self._ok(f"{len(concept)} chars of story generated")
        if self.show_prompts:
            console.print(concept)

        # STEP 3 — fill schema + location check ───────────────────────────────
        self._step(3, "LLM fills mission schema")
        schema_user = SCHEMA_FILL_USER.format(
            concept=concept, schema_hints=schema_hints,
            structure_block=structure_block,
        )
        # Smaller / cross-family models intermittently emit bad JSON or a value the
        # schema rejects (even after clamping). Re-roll once with a firmer nudge
        # before giving up — mirrors the micro pipeline's STEP-3 hardening.
        from pydantic import ValidationError as _ValidationError
        schema = None
        last_err: Exception | None = None
        for attempt in range(2):
            user_msg = schema_user
            if attempt > 0:
                user_msg += (
                    "\n\nREMINDER: Output ONE valid JSON object only — no prose. Use the "
                    "exact enum values listed (ped role is guard|patrol|target|civilian|ally; "
                    "race rivals are VEHICLES with role race_opponent, never peds)."
                )
            try:
                schema_raw = self.llm.chat(
                    system      = SCHEMA_FILL_SYSTEM,
                    user        = user_msg,
                    temperature = self.schema_temp if attempt == 0 else min(self.schema_temp, 0.1),
                    thinking    = False,
                )
                schema_dict = self.llm.extract_json(schema_raw)
                schema = MissionSchema.model_validate(schema_dict)
                break
            except (ValueError, _ValidationError) as e:
                last_err = e
                if attempt == 0:
                    self._warn(f"schema fill attempt 1 failed ({type(e).__name__}); retrying")
        if schema is None:
            raise RuntimeError(f"schema fill failed after 2 attempts: {last_err}") from last_err
        schema.meta = {**schema.meta, **theme_meta}
        # Persist the LLM concept (Step 2 story) so the Library page can show
        # a description for each saved mod, not just its filename.
        schema.meta["concept"]      = concept
        schema.meta["user_prompt"]  = user_prompt
        self._ok(
            f"{len(schema.steps)} steps  {len(schema.peds)} peds  "
            f"{len(schema.vehicles)} vehicles  {len(schema.objects)} objects"
        )

        # FIX 3 — location sanity check
        schema = self._check_location(concept, schema)

        # Anti-repetition guard: if the LLM still produced a degenerate steps
        # array for this archetype (no genre-defining step), inject the canonical
        # sub-game scaffold so the output is a real race/heist/etc.
        if archetype:
            for m in apply_scaffold(schema, archetype):
                self._fix(m)

        # STEP 4 — RAG resolves model names + consistency check ───────────────
        self._step(4, "RAG resolves model names")
        schema = self.resolver.resolve_schema(schema)
        unresolved = schema.unresolved_models()
        if unresolved:
            self._warn(f"Still unresolved after RAG: {unresolved}")
        else:
            self._ok("All model names resolved")

        # FIX 2 — step↔entity consistency
        schema = self._check_consistency(schema)

        # STEP 5 — validate ───────────────────────────────────────────────────
        self._step(5, "Validating schema")
        schema = _programmatic_fix(schema)
        self._ok("Programmatic fixes applied (weather, counts, whitespace)")

        still_unresolved = schema.unresolved_models()
        if still_unresolved:
            self._ok(f"LLM picking closest model for {len(still_unresolved)} UNRESOLVED item(s)")
            schema = self._fix_unresolved(schema)

        # Final audit — anything still UNRESOLVED after LLM pick will silently skip spawning
        final_unresolved = schema.unresolved_models()
        if final_unresolved:
            for item in final_unresolved:
                self._warn(f"Could not resolve model for {item} — spawn will be skipped in generated code")
        else:
            self._ok("All models resolved — no skipped spawns")

        # STEP 6 — compile ────────────────────────────────────────────────────
        self._step(6, "Pipeline compiles to .NET 4.8 C#")
        output_path = self.generator.write(schema, self.output_dir)
        self._ok(f"Written -> {output_path}")
        self._ok(f"Schema  -> {output_path.with_suffix('.schema.json')}")

        console.print(Panel(
            f"[bold green]Done![/]\n"
            f"Mission:  [cyan]{schema.mission_name}[/]\n"
            f"Location: [cyan]{schema.location_name}[/]\n"
            f"Output:   [cyan]{output_path}[/]",
            style="green",
        ))
        return output_path


# ── Editor entry-point: deterministic Step-6-only compile from a schema file ─
# Used by the GTA5 Mission Editor (sibling project) — the editor IS the schema
# author, so steps 1–5 (LLM concept, schema fill, RAG, fixers) are skipped.
def compile_from_schema(schema_path: Path | str,
                        output_dir: str = "output",
                        template_file: str = "templates/mission.cs.j2") -> Path:
    import json
    schema_path = Path(schema_path)
    data = json.loads(schema_path.read_text(encoding="utf-8"))
    schema = MissionSchema(**data)
    _lint_mission_extras(schema)
    gen = CodeGenerator(template_file=template_file)
    return gen.write(schema, output_dir)


def _main() -> int:
    import argparse, sys
    p = argparse.ArgumentParser(prog="python -m src.pipeline",
                                description="GTA5 mission pipeline (full LLM run, "
                                            "or --from-schema for Step-6-only compile)")
    p.add_argument("--from-schema", metavar="PATH",
                   help="Skip LLM/RAG and compile this schema JSON directly.")
    p.add_argument("--output-dir", default="output",
                   help="Where to write the .3.cs (default: output)")
    args = p.parse_args()

    if args.from_schema:
        out = compile_from_schema(args.from_schema, output_dir=args.output_dir)
        print(f"OK: {out}")
        return 0

    p.print_help()
    print("\n(Use main.py for the full LLM-driven pipeline.)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
