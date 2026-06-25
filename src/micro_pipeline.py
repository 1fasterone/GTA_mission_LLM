"""
Shared 6-step base class for domain-agnostic micro-generators.

A micro-generator produces a small, drop-in engine artefact (a character
controller, a camera rig, a physics body, a HUD) — not a whole project.
Each subclass provides:
  * `kind`              — short identifier (e.g. "controller")
  * `display_name`      — human label for log output
  * `engine`            — target engine tag ("unity", "gta5", "shared")
  * `concept_system`    — Step 2 LLM system prompt (override per engine)
  * `schema_fill_system`— Step 3 LLM system prompt (override per engine)
  * `schema_cls`        — Pydantic model the LLM fills at Step 3
  * `variants`          — list of allowed --variant values
  * `concept_prompt`    — Step 2 user prompt template ({user_prompt}, {theme_block}, {variant})
  * `schema_prompt`     — Step 3 user prompt template ({concept}, {schema_hints}, {variant})
  * `default_fix`       — Step 5 fixer (optional, base provides a no-op)
  * `write_files`       — Step 6 deterministic writer (subclass owns templates + output layout)

The base handles theming, LLM calls, JSON extraction, output dir naming, and the
schema.json sidecar.  Identical mental model to the existing GTA + Unity
pipelines, just smaller-scoped.

Engine-specific subclasses (UnityMicroPipeline, etc.) live in src/micro/unity_base.py
and override `concept_system`, `schema_fill_system`, and provide C# lint helpers.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Type

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.panel import Panel

from .lm_studio_client import LMStudioClient
from .backends import make_chat_client
from .themes import (
    resolve_theme,
    render_concept_bias,
    render_schema_hints,
)

console = Console()

# ── Default engine-agnostic system prompts ────────────────────────────────────
# Subclasses (UnityMicroPipeline, GTA5MicroPipeline, …) override these to
# give the LLM domain-specific context.  The base defaults are deliberately
# generic so an accidental direct instantiation still produces usable output.

_DEFAULT_CONCEPT_SYSTEM = (
    "You are a senior game engineer. Describe a focused, self-contained game "
    "feature in concrete technical terms — no code, no JSON. Be specific about "
    "behaviour, inputs, and which engine systems are involved."
)

_DEFAULT_SCHEMA_FILL_SYSTEM = (
    "You are a schema designer for a game content compiler. "
    "Output ONLY valid JSON — no explanation, no markdown fences. "
    "Use plain English for description fields."
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def to_pascal(s: str, fallback: str = "Generated") -> str:
    """Mirror of unity_generator._to_pascal — kept local to avoid circular imports."""
    if not s:
        return fallback
    if _IDENT_RE.match(s):
        return s
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", s).split()
    result = "".join(p.capitalize() for p in parts)
    if not result:
        return fallback
    if result[0].isdigit():
        result = fallback + result
    return result


# ── Base class ────────────────────────────────────────────────────────────────

class MicroPipeline(ABC):
    kind:               str = "micro"
    display_name:       str = "Micro"
    engine:             str = "shared"   # "unity" | "gta5" | "shared"
    # Override in engine-specific subclasses (e.g. UnityMicroPipeline):
    concept_system:     str = _DEFAULT_CONCEPT_SYSTEM
    schema_fill_system: str = _DEFAULT_SCHEMA_FILL_SYSTEM
    schema_cls:         Type[BaseModel]
    variants:           list[str] = []
    default_variant:    str = ""
    concept_prompt:     str = ""
    schema_prompt:      str = ""
    template_subdir:    str = ""         # under templates/<engine>/micro/
    SCHEMA_FILL_ATTEMPTS: int = 2        # re-roll STEP 3 on JSON/validation failures

    def __init__(self, config: dict):
        lm_cfg = config["lm_studio"]
        self.llm = make_chat_client(config)
        self.concept_temp = lm_cfg.get("concept_temperature", 0.85)
        self.schema_temp  = lm_cfg.get("schema_temperature", 0.2)
        # Generic fallback roots — engine subclasses override these in their own
        # __init__ to point at the correct output/template tree.
        self.output_root   = Path("output") / self.engine / "micro" / self.kind
        self.template_root = Path("templates") / self.engine / "micro" / (self.template_subdir or self.kind)

    # ── console helpers ──────────────────────────────────────────────────────

    def _step(self, n: int, title: str) -> None:
        console.print(f"\n[bold cyan]STEP {n}[/] -- {title}")
    def _ok(self, msg: str)   -> None: console.print(f"  [green]OK[/]   {msg}")
    def _warn(self, msg: str) -> None: console.print(f"  [yellow]WARN[/] {msg}")
    def _fix(self, msg: str)  -> None: console.print(f"  [magenta]FIX[/]  {msg}")

    # ── subclass hooks ───────────────────────────────────────────────────────

    def default_fix(self, schema: BaseModel, variant: str) -> BaseModel:
        return schema

    def _pre_write(self, schema: BaseModel, variant: str) -> None:
        """Hook called after the fixer (Step 5), before write_files (Step 6).

        Override in domain subclasses to perform infra work that must happen
        while the LLM client is still accessible but before deterministic codegen:
        supplementary LLM calls, VRAM management, model unloading, etc.
        The base no-ops; failures in overrides must be caught and logged, not raised.
        """

    def attach_class(self, schema: BaseModel) -> str:
        """The MonoBehaviour class a composed scene should attach for this micro.
        Defaults to schema.name; override when the micro's primary file is NOT
        the attachable component (e.g. audio: <name>.cs is a ScriptableObject
        bank — the MonoBehaviour is <name>AudioManager)."""
        return getattr(schema, "name", self.kind.title())

    @abstractmethod
    def write_files(self, schema: BaseModel, variant: str, out_dir: Path) -> int:
        """Write the actual files for this generator. Return file count."""
        ...

    def _after_write(self, schema: BaseModel, variant: str, out_dir: Path, name: str) -> None:
        """Post-write hook. Engine subclasses override to inject extra artefacts."""

    # ── Jinja env (subclasses can call) ──────────────────────────────────────

    def jinja(self) -> Environment:
        # Search the generator's own folder first, then the shared micro root so
        # templates can `{% import "_inject.j2" as inject %}` from one place.
        return Environment(
            loader=FileSystemLoader([
                str(self.template_root),
                str(self.template_root.parent),
            ]),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )

    # ── main run ─────────────────────────────────────────────────────────────

    def run(self, user_prompt: str, *,
            variant:     Optional[str] = None,
            theme:       Optional[str] = None,
            mashup_with: Optional[str] = None,
            seed:        Optional[int] = None,
            out:         Optional[str] = None) -> Path:

        variant = variant or self.default_variant or (self.variants[0] if self.variants else "")
        if self.variants and variant not in self.variants:
            raise ValueError(
                f"unknown variant {variant!r} for {self.kind}. "
                f"Choose one of: {', '.join(self.variants)}"
            )

        console.print(Panel(
            f"[bold]{self.display_name} Pipeline[/]\n"
            f"variant: [cyan]{variant}[/]\n"
            f"{user_prompt}", style="blue"))

        # Theme resolution (same precedence as the full pipelines)
        active_theme, rng, theme_meta = resolve_theme(
            user_prompt=user_prompt,
            theme_name=theme,
            mashup_with=mashup_with,
            seed=seed,
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

        # STEP 2 — concept
        self._step(2, f"LLM describes the {self.kind}")
        concept = self.llm.chat(
            system      = self.concept_system,
            user        = self.concept_prompt.format(
                user_prompt=user_prompt,
                theme_block=theme_block,
                variant=variant,
            ),
            temperature = self.concept_temp,
            thinking    = True,
        )
        self._ok(f"{len(concept)} chars of brief generated")

        # STEP 3 — schema
        self._step(3, f"LLM fills the {self.kind} schema")
        schema_user = self.schema_prompt.format(
            concept=concept,
            schema_hints=schema_hints,
            variant=variant,
        )
        # Smaller / code-trained local models intermittently fail this step:
        # chatty refusals (no JSON), C# literals in JSON (10f), or a value of the
        # wrong shape. These are flaky per-roll, so re-roll with a firmer nudge
        # before giving up — a cheap, model-agnostic robustness win.
        schema = None
        last_err: Exception | None = None
        for attempt in range(self.SCHEMA_FILL_ATTEMPTS):
            user_msg = schema_user
            if attempt > 0:
                user_msg += (
                    "\n\nREMINDER: Respond with ONE valid JSON object and nothing "
                    "else — no prose, no markdown. Use plain JSON numbers (e.g. 10, "
                    "not 10f) and a single value per field (no arrays unless the "
                    "field is a list)."
                )
            try:
                schema_raw = self.llm.chat(
                    system      = self.schema_fill_system,
                    user        = user_msg,
                    temperature = self.schema_temp if attempt == 0 else min(self.schema_temp, 0.1),
                    thinking    = False,
                )
                schema_dict = self.llm.extract_json(schema_raw)
                # The LLM occasionally omits or fights the variant — force it.
                schema_dict.setdefault("variant", variant)
                schema = self.schema_cls.model_validate(schema_dict)
                break
            except (ValueError, ValidationError) as e:
                last_err = e
                if attempt + 1 < self.SCHEMA_FILL_ATTEMPTS:
                    self._warn(f"schema fill attempt {attempt + 1} failed ({type(e).__name__}); retrying")
        if schema is None:
            raise RuntimeError(
                f"schema fill failed after {self.SCHEMA_FILL_ATTEMPTS} attempts: {last_err}"
            ) from last_err
        # Stash theme metadata + the LLM concept so the Library page can show
        # a description for each saved micro output.
        if hasattr(schema, "meta"):
            current = getattr(schema, "meta") or {}
            current.update(theme_meta)
            current["concept"]     = concept
            current["user_prompt"] = user_prompt
            setattr(schema, "meta", current)
        self._ok("schema validated")

        # STEP 4 — (micros generally have nothing to resolve; subclasses may override)
        self._step(4, "no external assets to resolve")
        self._ok("skipped — micro-generators are self-contained")

        # STEP 5 — fixer
        self._step(5, "Programmatic fixer")
        schema = self.default_fix(schema, variant)
        self._ok("fixer pass complete")

        # STEP 5.5 — pre-write hook (VRAM management, supplementary LLM calls, etc.)
        self._pre_write(schema, variant)

        # STEP 6 — write
        self._step(6, f"Write the {self.kind} folder")
        # Pick a project name
        name = to_pascal(getattr(schema, "name", "") or self.kind.title(), self.kind.title())
        if hasattr(schema, "name"):
            setattr(schema, "name", name)

        out_dir = Path(out) if out else (self.output_root / name)
        out_dir.mkdir(parents=True, exist_ok=True)

        file_count = self.write_files(schema, variant, out_dir)
        self._after_write(schema, variant, out_dir, name)

        # Sidecar schema for re-runs
        schema_path = out_dir / f"{name}.schema.json"
        schema_path.write_text(
            json.dumps(schema.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._ok(f"wrote {file_count} file(s) + schema -> {out_dir}")

        console.print(Panel(
            f"[bold green]Done![/]\n"
            f"{self.display_name}:  [cyan]{name}[/]\n"
            f"Variant:    [cyan]{variant}[/]\n"
            f"Output:     [cyan]{out_dir}[/]",
            style="green",
        ))
        return out_dir
