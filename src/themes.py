"""
Theme template layer for the mission/project generator.

A theme is a YAML file in themes/ that nudges the LLM at Step 2 (concept)
and Step 3 (schema fill) toward a distinct, original-feeling setting.
Themes inject vocabulary, atmosphere, suggested step types, an anti-trope
guard, and one randomly-chosen "twist seed" per run.

The pipeline (Steps 4-6) is untouched: themes only shape the LLM's prose
and JSON. Asset resolution still goes through the existing resolver and
lookup tables.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


# ── Schema ────────────────────────────────────────────────────────────────────

class ThemeVocabulary(BaseModel):
    peds:          list[str] = Field(default_factory=list)
    vehicles:      list[str] = Field(default_factory=list)
    objects:       list[str] = Field(default_factory=list)
    locations_gta: list[str] = Field(default_factory=list)


class ThemeUnity(BaseModel):
    palette:       dict[str, str] = Field(default_factory=dict)
    systems_hint:  list[str] = Field(default_factory=list)
    visual_style:  Optional[str] = None


class Theme(BaseModel):
    name:             str
    display:          str
    pipelines:        list[str]                = Field(default_factory=lambda: ["gta", "unity"])
    concept_bias:     str                       = ""
    detect_keywords:  list[str]                 = Field(default_factory=list)
    vocabulary:       ThemeVocabulary           = Field(default_factory=ThemeVocabulary)
    step_hints:       list[str]                 = Field(default_factory=list)
    unity:            ThemeUnity                = Field(default_factory=ThemeUnity)
    twist_seeds:      list[str]                 = Field(default_factory=list)
    avoid:            list[str]                 = Field(default_factory=list)
    variants:         dict[str, str]            = Field(default_factory=dict)


# ── Disk I/O ──────────────────────────────────────────────────────────────────

def _themes_dir() -> Path:
    """Resolve the themes/ directory next to the repo root."""
    return Path(__file__).resolve().parent.parent / "themes"


def load_theme(name: str, themes_dir: Optional[Path] = None) -> Theme:
    """Load a theme by name (filename without .yaml)."""
    base = themes_dir or _themes_dir()
    path = base / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"theme not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Theme.model_validate(data)


def list_themes(themes_dir: Optional[Path] = None) -> list[Theme]:
    """List every theme available on disk."""
    base = themes_dir or _themes_dir()
    if not base.exists():
        return []
    out: list[Theme] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            out.append(Theme.model_validate(data))
        except Exception:
            # Skip malformed theme files rather than blowing up the pipeline.
            continue
    return out


def pick_random_theme(rng: Optional[random.Random] = None,
                      themes_dir: Optional[Path] = None) -> Theme:
    """Pick one theme uniformly at random."""
    rng = rng or random.Random()
    themes = list_themes(themes_dir)
    if not themes:
        raise RuntimeError("no themes found on disk")
    return rng.choice(themes)


# ── Auto-detect ───────────────────────────────────────────────────────────────

def autodetect_theme(prompt: str, themes_dir: Optional[Path] = None) -> Optional[Theme]:
    """
    Pick the theme whose keywords have the strongest signal in the prompt.
    Returns None if no theme scores above zero (caller should leave themes off).
    """
    if not prompt:
        return None
    text = prompt.lower()
    best: Optional[Theme] = None
    best_score = 0
    for theme in list_themes(themes_dir):
        score = 0
        for kw in theme.detect_keywords:
            if kw.lower() in text:
                score += 2          # explicit keyword hit is strong
        # Soft signal: any vocabulary word appearing in the prompt
        for v in (theme.vocabulary.peds + theme.vocabulary.vehicles
                  + theme.vocabulary.objects):
            tok = v.lower().split()[0]
            if len(tok) >= 5 and tok in text:
                score += 1
        if score > best_score:
            best, best_score = theme, score
    return best


# ── Mashup ────────────────────────────────────────────────────────────────────

def _merge_unique(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for item in lst:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def mashup(a: Theme, b: Theme, *, rng: Optional[random.Random] = None) -> Theme:
    """
    Combine two themes into a synthetic third. Vocabulary and step hints are
    unioned; twist seeds are one from each (randomly); concept_bias is a new
    paragraph instructing the LLM to genuinely fuse — not pick one or the
    other.
    """
    rng = rng or random.Random()
    seed_a = rng.choice(a.twist_seeds) if a.twist_seeds else ""
    seed_b = rng.choice(b.twist_seeds) if b.twist_seeds else ""

    bias = (
        f"A genuine fusion of two settings — neither dominates.\n\n"
        f"FIRST SETTING ({a.display}):\n{a.concept_bias.strip()}\n\n"
        f"SECOND SETTING ({b.display}):\n{b.concept_bias.strip()}\n\n"
        f"Do not alternate scenes between them. Build one world where both "
        f"are simultaneously, structurally true."
    )

    merged = Theme(
        name        = f"{a.name}__{b.name}",
        display     = f"{a.display} × {b.display}",
        pipelines   = _merge_unique(a.pipelines, b.pipelines),
        concept_bias = bias,
        detect_keywords = _merge_unique(a.detect_keywords, b.detect_keywords),
        vocabulary = ThemeVocabulary(
            peds          = _merge_unique(a.vocabulary.peds,          b.vocabulary.peds),
            vehicles      = _merge_unique(a.vocabulary.vehicles,      b.vocabulary.vehicles),
            objects       = _merge_unique(a.vocabulary.objects,       b.vocabulary.objects),
            locations_gta = _merge_unique(a.vocabulary.locations_gta, b.vocabulary.locations_gta),
        ),
        step_hints  = _merge_unique(a.step_hints, b.step_hints),
        unity = ThemeUnity(
            # Blend palettes: A's primary/accent, B's secondary/background, A's trim
            palette = {
                "name":       f"{a.unity.palette.get('name', a.name)}_x_{b.unity.palette.get('name', b.name)}",
                "primary":    a.unity.palette.get("primary",    "#FFFFFF"),
                "secondary":  b.unity.palette.get("secondary",  "#101010"),
                "accent":     a.unity.palette.get("accent",     "#FF00FF"),
                "background": b.unity.palette.get("background", "#000000"),
                "trim":       a.unity.palette.get("trim",       "#CCCCCC"),
            },
            systems_hint = _merge_unique(a.unity.systems_hint, b.unity.systems_hint),
            visual_style = a.unity.visual_style or b.unity.visual_style,
        ),
        # Twist seeds: just the two we already picked, in order
        twist_seeds = [s for s in (seed_a, seed_b) if s],
        avoid       = _merge_unique(a.avoid, b.avoid),
    )
    return merged


# ── Prompt rendering ──────────────────────────────────────────────────────────

def render_concept_bias(theme: Theme, *, rng: Optional[random.Random] = None) -> tuple[str, str]:
    """
    Return ``(theme_block, chosen_twist_seed)``.

    The theme block is inserted near the top of the Step 2 prompt.
    The chosen twist seed is also recorded in the schema metadata so the run
    is reproducible given the seed.
    """
    rng = rng or random.Random()
    twist = rng.choice(theme.twist_seeds) if theme.twist_seeds else ""

    lines: list[str] = []
    lines.append(f"THEME: {theme.display}")
    if theme.concept_bias.strip():
        lines.append(f"TONE: {theme.concept_bias.strip()}")
    if twist:
        lines.append("REQUIRED TWIST (must be honored, not glossed over):")
        lines.append(f"  {twist}")

    vocab = theme.vocabulary
    if vocab.peds or vocab.vehicles or vocab.objects:
        lines.append("PREFERRED ELEMENTS:")
        if vocab.peds:
            lines.append("  characters: " + " | ".join(vocab.peds))
        if vocab.vehicles:
            lines.append("  vehicles:   " + " | ".join(vocab.vehicles))
        if vocab.objects:
            lines.append("  objects:    " + " | ".join(vocab.objects))

    if theme.avoid:
        lines.append("AVOID THESE CLICHÉS: " + "; ".join(theme.avoid) + ".")

    lines.append(
        "ORIGINALITY DIRECTIVE: do not retell a famous example of this genre. "
        "Use the twist above as the spine of the mission; build characters and "
        "objectives that only make sense because of it."
    )

    return "\n".join(lines), twist


def render_schema_hints(theme: Theme) -> str:
    """A softer hint block for Step 3 — preferred step types and vocabulary."""
    if not theme:
        return ""
    parts: list[str] = []
    if theme.step_hints:
        parts.append("PREFERRED STEP TYPES (use these where they fit): "
                     + ", ".join(theme.step_hints))
    if theme.vocabulary.peds:
        parts.append("Suggested ped descriptions: " + " | ".join(theme.vocabulary.peds))
    if theme.vocabulary.vehicles:
        parts.append("Suggested vehicle descriptions: " + " | ".join(theme.vocabulary.vehicles))
    if theme.vocabulary.objects:
        parts.append("Suggested object descriptions: " + " | ".join(theme.vocabulary.objects))
    if theme.vocabulary.locations_gta:
        parts.append("Prefer one of these GTA locations: " + ", ".join(theme.vocabulary.locations_gta))
    return "\n".join(parts)


# ── Top-level resolve helper used by the pipelines ────────────────────────────

def resolve_theme(
    *,
    user_prompt:  str,
    theme_name:   Optional[str] = None,
    mashup_with:  Optional[str] = None,
    seed:         Optional[int] = None,
    themes_dir:   Optional[Path] = None,
) -> tuple[Optional[Theme], random.Random, dict]:
    """
    Resolve which theme (if any) should apply for this run.

    Precedence: explicit name (or 'random') → mashup → auto-detect → none.
    Returns the (possibly None) Theme, a seeded RNG, and a metadata dict the
    pipeline can stash on the output schema.
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    meta: dict = {
        "theme":       None,
        "mashup_with": None,
        "seed":        seed,
        "twist_seed":  None,
    }

    if theme_name and theme_name.lower() == "random":
        theme = pick_random_theme(rng=rng, themes_dir=themes_dir)
    elif theme_name and theme_name.lower() == "none":
        theme = None  # explicit opt-out of theme injection
    elif theme_name:
        theme = load_theme(theme_name, themes_dir=themes_dir)
    else:
        theme = autodetect_theme(user_prompt, themes_dir=themes_dir)

    if theme and mashup_with:
        partner = load_theme(mashup_with, themes_dir=themes_dir)
        theme = mashup(theme, partner, rng=rng)
        meta["mashup_with"] = mashup_with

    if theme:
        meta["theme"] = theme.name
    return theme, rng, meta
