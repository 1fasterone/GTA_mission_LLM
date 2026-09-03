"""Concept-named TITLE rescue — restores the brief's own declared project
title when the Step-3 schema-fill LLM invents a different one.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B) — the sibling of
unity_concept_entities.py / unity_concept_scripts.py for the project's own
name. Same cause as both (see [[step3-sees-only-the-concept]]).

Measured live on prompts/coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief's title survived 0/1 — the
LLM arm shipped "FrozenLakeRally" in place of the brief's own
"ColdwaterCircuit". Entities and scripts, which already had rescue layers,
survived at 100% on the same run — this closes the same gap for the title.

Unlike every sibling in this family, the rescue here is REPLACE-if-wrong, not
append: schema.project_name is a single scalar, so there is no "append the
ones that went missing" — only "was the LLM's value right or wrong". That
makes false positives possible in a way list-rescues aren't: an unstructured
one-line vibe prompt ("a game about a lighthouse keeper") has no real title
to recover, and forcing its first few words over as
"AGameAboutALighthouseKeeper" would replace a plausibly-good LLM invention
with a mechanically bad one. Two confidence tiers gate the override so that
never happens — see _has_explicit_title / _is_structured below. Neither tier
met -> abstain, schema.project_name is left exactly as Step 3 wrote it.

Deterministic regex parse of the ORIGINAL user_prompt
(schema.meta['user_prompt']), reusing the same section/scalar primitives
src/unity_briefparse.py's --no-llm control arm uses (see
src/unity_brief_sections.py).
"""

from __future__ import annotations

from .unity_brief_sections import (_ANY_HEADER_RE, _DASH_SPLIT_RE, _lines,
                                   _scalar, _section, _to_pascal)


def extract_title(user_prompt: str) -> tuple[str, str]:
    """(project_name, description) from the brief's TITLE line/section, or
    the "Name — premise." first-line house form when no TITLE is stated.

    Always returns SOMETHING — this is also the --no-llm control arm's sole
    source of a title, which has no LLM to fall back on. A caller deciding
    whether that "something" is trustworthy enough to override an LLM's own
    title should gate on _has_explicit_title / _is_structured below first,
    not call this directly for that purpose (see inject_missing_concept_title).
    """
    # Three title forms are live in prompts/: an inline "TITLE: X", a bare
    # "TITLE" header with the name on the next line, and no TITLE at all with
    # the name opening the first line. Reading only the first line named two
    # real briefs "TITLE" literally.
    explicit = _scalar(user_prompt, "TITLE")
    if not explicit:
        section = [ln.strip() for ln in _section(user_prompt, "TITLE") if ln.strip()]
        if section:
            explicit = section[0]
    lines = [ln.strip() for ln in _lines(user_prompt)]
    first = next((ln for ln in lines if ln), "")

    raw_title = explicit or first
    name_part = _DASH_SPLIT_RE.split(raw_title, maxsplit=1)[0].strip()
    project_name = _to_pascal(name_part, "UnityProject")

    # Description: CORE PREMISE, else GENRE (both are prose statements of what
    # the game IS), else the first paragraph under the title line.
    for header in ("CORE PREMISE", "GENRE"):
        body = [ln.strip() for ln in _section(user_prompt, header) if ln.strip()]
        if body:
            return project_name, " ".join(body)

    desc: list[str] = []
    started = False
    for ln in lines:
        if not started:
            if ln == first:
                started = True
            continue
        if not ln:
            if desc:
                break
            continue
        if _ANY_HEADER_RE.match(ln):
            break
        desc.append(ln)
    # The title's own subtitle is the one-line description when there is no
    # paragraph under it.
    tail = _DASH_SPLIT_RE.split(raw_title, maxsplit=1)
    fallback = tail[1].strip() if len(tail) > 1 else ""
    return project_name, (" ".join(desc) or fallback)


def _has_explicit_title(user_prompt: str) -> bool:
    """High-confidence tier: the brief states its title outright."""
    return bool(_scalar(user_prompt, "TITLE")
                or [ln for ln in _section(user_prompt, "TITLE") if ln.strip()])


# Mirrors unity_pipeline._is_structured_brief's own threshold in spirit (>=3
# header hits total there, gating whether to skip theme auto-detection).
# Implemented locally rather than imported — that function lives in
# unity_pipeline.py, which imports THIS module, so importing it back would be
# circular. The bar here is >=2 headers OTHER than TITLE, one lower than
# pipeline's >=3-of-anything, because a title-bearing brief routinely opens
# with exactly ENTITIES + SCENES declared and nothing else yet at the point
# this fires.
_STRUCTURAL_MIN_HEADERS = 2


def _is_structured(user_prompt: str) -> bool:
    """Lower-confidence tier: no explicit TITLE, but the brief otherwise
    follows the house structured format, where "the first line is the
    title" is a real convention (prompts/dropzone.txt) rather than a guess."""
    hits = {m.group(1).upper() for line in _lines(user_prompt)
            for m in [_ANY_HEADER_RE.match(line.strip())] if m}
    hits.discard("TITLE")
    return len(hits) >= _STRUCTURAL_MIN_HEADERS


def render_title_checklist(user_prompt: str) -> str:
    """Explicit-tier only — a checklist actively steers Step 3, so it should
    only steer on high-confidence evidence. The broader structural-fallback
    tier stays a post-hoc safety net (inject_missing_concept_title below),
    never a prompt directive. "" when the brief has no explicit title.
    """
    if not _has_explicit_title(user_prompt):
        return ""
    project_name, _desc = extract_title(user_prompt)
    return (
        "REQUIRED PROJECT TITLE — the brief explicitly declares this "
        f'project\'s title. "project_name" MUST be exactly "{project_name}" '
        "— do not invent a different one.\n"
    )


def inject_missing_concept_title(schema) -> list[str]:
    """Overwrite schema.project_name with the brief's own declared title —
    ONLY when the brief clearly states one (explicit tier, or the
    structural-fallback tier — see module docstring) AND the schema's
    current value does not already match it. Never touches a schema whose
    title already agrees with the brief. Returns a fixer log message only
    when it actually changes something.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    user_prompt = meta.get("user_prompt", "")
    if not (_has_explicit_title(user_prompt) or _is_structured(user_prompt)):
        return []

    project_name, _desc = extract_title(user_prompt)
    if not project_name or _to_pascal(schema.project_name, "") == project_name:
        return []

    old = schema.project_name
    schema.project_name = project_name
    return [
        f"Schema-fill renamed the project to {old!r} — restored "
        f"{project_name!r}, the brief's own declared title."
    ]
