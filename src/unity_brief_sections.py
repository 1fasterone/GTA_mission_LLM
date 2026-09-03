"""Low-level structured-brief reading: section slicing, scalar declarations,
numbered/bulleted list folding, name/body splitting, PascalCase
normalization.

Shared by src/unity_briefparse.py (the --no-llm deterministic fill) and every
src/unity_concept_*.py rescue layer that reads a brief section by its house
header (scenes, audio, narrative, title) — so the header vocabulary and list-
folding rules live in exactly one place instead of drifting across five
files. unity_concept_entities.py / unity_concept_scripts.py are deliberately
NOT built on this: their bullet lists close on any ALL-CAPS line (a brief
author can invent a section name those two don't need to recognise), where
everything here closes only on _HEADERS, the known house vocabulary.
"""

from __future__ import annotations

import re

# Same header vocabulary unity_pipeline._BRIEF_MARKER_RE gates on (that one
# lives in unity_pipeline.py and stays a separate, deliberately looser
# duplicate — it only needs to guess "is this brief structured at all", not
# resolve individual section boundaries).
_HEADERS = (
    "TITLE", "GENRE", "STYLE", "CORE PREMISE", "SCENES", "NARRATIVE",
    "SCRIPTS", "ENTITIES", "MISSIONS", "AUDIO", "FEATURES", "VISUAL STYLE",
    "COLOR PALETTE", "PROCEDURAL GEOMETRY", "PROCEDURAL", "RENDER PIPELINE",
    "UNITY VERSION", "PLATFORM", "GAME TYPE", "LOCOMOTION",
)
_ANY_HEADER_RE = re.compile(
    r"^(" + "|".join(re.escape(h) for h in _HEADERS) + r")\b", re.IGNORECASE)

# A scalar declaration: "PLATFORM: PC", "RENDER PIPELINE: URP  (volumetric...)".
_SCALAR_RE_TMPL = r"^{key}\s*[:\-—]\s*(.+)$"

# "1. MainMenu         — animated title: skydiver silhouette against sunset"
_NUMBERED_RE = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")
# The em/en dash (or " - ") that splits a numbered entry's name from its body.
_DASH_SPLIT_RE = re.compile(r"\s+[—–]\s+|\s+-\s+")


def _lines(prompt: str) -> list[str]:
    return (prompt or "").splitlines()


def _section(prompt: str, header: str) -> list[str]:
    """Every line under `header`, up to the next recognised section header."""
    out: list[str] = []
    grabbing = False
    head_re = re.compile(r"^" + re.escape(header) + r"\b", re.IGNORECASE)
    for line in _lines(prompt):
        stripped = line.strip()
        if not grabbing:
            if head_re.match(stripped):
                grabbing = True
            continue
        # A new header closes the section. Checked against the stripped line so
        # an indented continuation can never be mistaken for a header.
        if _ANY_HEADER_RE.match(stripped) and not head_re.match(stripped):
            break
        out.append(line)
    return out


def _scalar(prompt: str, key: str) -> str:
    """Read a 'KEY: value' declaration anywhere in the brief. '' when absent."""
    pat = re.compile(_SCALAR_RE_TMPL.format(key=re.escape(key)), re.IGNORECASE)
    for line in _lines(prompt):
        m = pat.match(line.strip())
        if m:
            # Strip a trailing parenthetical rationale: "URP  (volumetric ...)"
            return re.sub(r"\s*\(.*$", "", m.group(1)).strip()
    return ""


def _fold_numbered(lines: list[str], *, bullets: bool = False) -> list[str]:
    """Fold a wrapped list into one logical string per entry.

    The generalised sibling of unity_concept_entities._fold_bullets: SCENES and
    NARRATIVE wrap their descriptions across indented continuation lines, and
    reading them line-by-line truncates every entry at ~80 cols.

    `bullets=True` also opens an entry on a "- " marker. Both list styles are
    live in prompts/: dropzone.txt numbers its scenes ("1. MainMenu — ..."),
    turbo_toys.txt and grandmas_antique_shop.txt bullet theirs
    ("- Main Menu: ..."). A reader that knows only one silently sees zero
    scenes in half the corpus — which is an abstain, not a wrong answer, but
    an abstain on a brief that plainly stated its scenes.

    A line that is neither a marker nor an indented continuation (the trailing
    "Scene transitions fade between hub, shop, museum..." prose both briefs put
    under the list) closes the current entry instead of being absorbed.
    """
    entries: list[str] = []
    pending: list[str] = []

    def close() -> None:
        if pending:
            entries.append(" ".join(pending))
            pending.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close()
            continue
        if _NUMBERED_RE.match(stripped):
            close()
            pending.append(stripped)
        elif bullets and stripped.startswith("-"):
            close()
            pending.append(stripped.lstrip("-").strip())
        elif pending and line[:1].isspace():
            pending.append(stripped)
        else:
            close()
    close()
    return entries


def _to_pascal(text: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    out = "".join(w[:1].upper() + w[1:] for w in words)
    if out and out[0].isdigit():
        out = "P" + out
    return out or fallback
