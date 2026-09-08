"""
Concept fidelity — did the words in the brief survive into the game?

Nothing in the pipeline currently notices when every adjective in a prompt
evaporates. `unity_playability._check_brief_entity_coverage` grades NAMES: the
brief said "Hippo", the schema has an entity called "Hippo", pass. It never
looks at what that entity became. So both of these grade clean today:

    "purple hippos with six legs"  -> entity Hippo -> hippo-low.fbx
    "commit node"                  -> entity CommitNode -> marble-center-butterfly-low.fbx

The first lost `purple` and `six legs`, which no field was asked to carry. The
second lost the entity outright — the name survived, the MODEL is a marble run.
Neither is visible anywhere: the resolver returns its best available match and
reports success, exactly as the micro-code writer used to delete a mechanic and
report success.

That is the pattern this module measures, and it is one pattern in three places:

    a deterministic step substitutes its nearest match for what was actually
    asked, and nothing downstream can tell the difference.

Two independent gaps, because they are lost at different steps and repaired by
different work:

  MODIFIER  — an adjective or attribute bound to a noun in the brief that
              appears nowhere in the output. Lost at Step 3 (nothing asked for
              it) or Step 6 (no field carries it).
  ASSET     — an entity whose resolved model shares no vocabulary with the
              thing it is supposed to be. Lost at Step 6, in the resolver's
              nearest-match scoring.

Deterministic and LLM-free, like every post-schema step. It reports; it does not
repair. A number that regresses is the point — "six legs" either appears in the
output or it does not, and today that is an opinion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

# ── Vocabulary ────────────────────────────────────────────────────────────────

# Words that can never be a modifier worth tracking: they carry no visual or
# mechanical commitment, so demanding they "survive" would be noise.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "some", "any", "each",
    "every", "its", "their", "his", "her", "your", "our", "my", "of", "in",
    "on", "at", "to", "and", "or", "but", "with", "for", "from", "by", "as",
    "is", "are", "be", "was", "were", "you", "they", "it", "we", "he", "she",
    "who", "which", "there", "here", "when", "where", "while", "into", "onto",
    "one", "up", "down", "out", "over", "under", "then", "than", "so", "if",
    "can", "will", "must", "should", "may", "own", "more", "most", "very",
    "player", "game", "games", "level", "scene", "screen", "thing", "things",
})

# Number words that quantify a feature ("six legs"). The count IS the modifier —
# a hippo with six legs is not a hippo, and that is the whole point of the
# canonical example.
_NUMBER_WORDS: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

# `six-legged`, `low-poly`, `bowler-hatted` — a hyphenated compound adjacent to a
# noun is nearly always a modifier, and it is the single highest-precision
# signal available without a POS tagger.
_HYPHEN_COMPOUND_RE = re.compile(r"\b([a-z]+(?:-[a-z]+)+)\b")

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*|\d+")

# `with six legs`, `with 6 legs`, `wearing bowler hats`, `made of spaghetti`.
# Post-modifiers carry as much commitment as pre-modifiers and are how English
# actually expresses the interesting ones.
#
# The trailing repetition must NOT swallow the next cue word: a greedy
# `(\s+[a-z]+){0,2}` turned "with six legs, wearing bowler hats" into one match
# ending at "wearing", so the hats were never seen at all. The lookahead ends
# each clause at the start of the next one.
_POST_CUES = r"with|wearing|carrying|holding|made|covered|full|driving|and"
_POST_MODIFIER_RE = re.compile(
    r"\b(?:with|wearing|carrying|holding|made\s+of|covered\s+in|full\s+of)\s+"
    rf"((?:\d+|[a-z]+)(?:\s+(?!(?:{_POST_CUES})\b)[a-z]+){{0,2}})",
    re.IGNORECASE)


@dataclass
class Demand:
    """One concrete thing the brief asked for, with the words that qualify it."""
    noun: str
    modifiers: tuple[str, ...] = ()
    phrase: str = ""

    def __str__(self) -> str:  # pragma: no cover - display only
        mods = " ".join(self.modifiers)
        return f"{mods} {self.noun}".strip()


@dataclass
class Gap:
    """One thing the brief asked for that the output does not contain."""
    kind: str            # "modifier" | "asset"
    demand: str          # what was asked for
    detail: str          # what happened instead
    owner: str           # the step that lost it


@dataclass
class FidelityReport:
    demands: list[Demand] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    # Entities whose resolved model was actually graded. Counted alongside the
    # demanded words because an entity resolved to an unrelated model is a
    # failed check, and leaving it out of the denominator produced a report that
    # said "100/100" next to a list of losses.
    assets_checked: int = 0

    @property
    def checked(self) -> int:
        return (sum(1 + len(d.modifiers) for d in self.demands)
                + self.assets_checked)

    @property
    def survived(self) -> int:
        return max(0, self.checked - len(self.gaps))

    @property
    def score(self) -> int:
        """Percent of demanded words that reached the output. 100 on an empty
        brief — nothing was asked for, so nothing was lost."""
        return 100 if not self.checked else round(100 * self.survived / self.checked)


# ── Extraction ────────────────────────────────────────────────────────────────

# Entity names are PascalCase and briefs are prose: `CessnaCaravan` tokenised to
# one word `cessnacaravan`, which never matched the brief's "Cessna Caravan", so
# every entity bound zero modifiers and the report read a confident 100/100.
# Split on the case transition BEFORE lowercasing — lowercasing first destroys
# the signal the split depends on. Same rule and same lookahead as
# unity_archetypes._CAMEL_BOUNDARY_RE, which keeps "2.5D"/"3D" intact.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z][a-z])")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_CAMEL_BOUNDARY_RE.sub(" ", text).lower())


_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]")


def _is_modifier(tok: str) -> bool:
    """Is this token a qualifier worth demanding survives?

    Deliberately generous: any non-stopword adjacent to a noun. An earlier cut
    only accepted colours, numbers, hyphenates and -ed/-ish, which sounds
    principled and threw away `custard` pies, `paper` airplanes and `bowler`
    hats — the exact words the metric exists to protect. Without a POS tagger
    the immediately-preceding non-stopword IS the modifier, and over-reporting a
    word the brief really used beats silently dropping it. Silent dropping is
    the defect being measured.
    """
    if tok.isdigit():
        return True
    return tok not in _STOPWORDS and len(tok) >= 3


def extract_demands(brief: str, nouns: Iterable[str]) -> list[Demand]:
    """Bind each named noun to the qualifying words attached to it in the brief.

    `nouns` comes from the caller — normally `unity_concept_entities
    .extract_named_entities`, so this module never invents a second opinion
    about what the brief's entities are. It only asks what was said ABOUT them.

    Pre-modifiers are taken from the two tokens before the noun; post-modifiers
    from a `with/wearing/made of` clause after it. Both are capped, because a
    modifier three clauses away is usually about something else.
    """
    if not brief.strip():
        return []
    out: list[Demand] = []

    # Every noun's surface forms, so a trailing clause can be stopped by the
    # NEXT noun rather than smeared across every noun in the sentence. Without
    # this boundary "purple hippos with six legs, wearing bowler hats, driving
    # tanks" handed the hats to the tanks and the pies as well.
    forms_by_noun: dict[str, set[str]] = {}
    for raw in nouns:
        # NOT .lower() first — _tokens camel-splits then lowercases, and
        # pre-lowercasing destroys the case transition it splits on, collapsing
        # "CessnaCaravan" to one token that no prose brief can ever match.
        n_toks = _tokens(raw.strip())
        if not n_toks:
            continue
        head = n_toks[-1]
        forms = {head, head + "s", head + "es"}
        if head.endswith("s"):
            forms.add(head[:-1])
        forms_by_noun[raw.strip()] = forms
    all_forms = set().union(*forms_by_noun.values()) if forms_by_noun else set()

    sentences = [_tokens(s) for s in _SENTENCE_SPLIT_RE.split(brief.lower())]

    for raw, forms in forms_by_noun.items():
        own_toks = set(_tokens(raw.strip()))
        mods: list[str] = []
        phrase = ""
        for toks in sentences:
            i = next((k for k, t in enumerate(toks) if t in forms), None)
            if i is None:
                continue
            phrase = " ".join(toks[max(0, i - 3):i + 6])

            # Pre-modifiers: the two tokens before the noun.
            for back in (toks[i - 2] if i >= 2 else "",
                         toks[i - 1] if i >= 1 else ""):
                if back and _is_modifier(back) and back not in own_toks:
                    mods.append(back)

            # Post-modifiers: from the noun to the next DIFFERENT noun, or the
            # end of the sentence. "with six legs, wearing bowler hats" belongs
            # to the hippo; "driving tanks" starts the tank's territory.
            others = forms_by_noun and (all_forms - forms)
            end = next((k for k in range(i + 1, len(toks)) if toks[k] in others),
                       len(toks))
            for m in _POST_MODIFIER_RE.finditer(" ".join(toks[i + 1:end])):
                for t in _tokens(m.group(1)):
                    if t not in own_toks and t not in mods and _is_modifier(t):
                        mods.append(t)
            break

        # A noun the brief never says is not a demand. Without this, the
        # schema-entity fallback graded "make me a game" against the build's own
        # Player/Camera entities, every one of them trivially "survived", and a
        # brief that committed to NOTHING scored 100/100 — the unearned perfect
        # score this module treats as worse than no score at all.
        #
        # Dropping it also avoids double-reporting: "the brief named it and the
        # build lacks it" is already unity_playability's `brief_entities`, at
        # ERROR, which outranks anything reported from here.
        if not phrase:
            continue
        out.append(Demand(noun=raw, modifiers=tuple(dict.fromkeys(mods)),
                          phrase=phrase))
    return out


# "rather than an arena shooter or open-world exploration title", "not a
# stealth game", "instead of tank controls" — a compound inside one of these
# clauses names something the brief is explicitly RULING OUT, not asking
# for. Confirmed live: Grandma's Antique Shop's own auto-expanded restatement
# paragraph reads "...a cramped interior space rather than an arena shooter
# or open-world exploration title" — demanding "open-world" survive into a
# shop-interior brawler schema would be grading the brief's own negation as
# a loss.
_NEGATION_CUE_RE = re.compile(
    r"\b(?:rather than|instead of|as opposed to|not an?|isn't|is not|"
    r"never an?|without)\b")


def extract_free_modifiers(brief: str) -> list[str]:
    """Hyphenated compounds anywhere in the brief.

    `six-legged`, `bowler-hatted`, `low-poly` are commitments whether or not
    this module managed to bind them to a noun. Reported separately so a
    binding miss never silently drops the word — the failure mode being fixed
    is losing things quietly.

    A compound is dropped only when EVERY sentence it appears in negates it
    (see `_NEGATION_CUE_RE`) — one affirming mention elsewhere in the brief
    still counts as a real demand.
    """
    order: list[str] = []
    affirmed: set[str] = set()
    negated: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(brief.lower()):
        cue = _NEGATION_CUE_RE.search(sentence)
        cue_pos = cue.start() if cue else None
        for m in _HYPHEN_COMPOUND_RE.finditer(sentence):
            tok = m.group(1)
            if tok in _STOPWORDS:
                continue
            if all(p in _STOPWORDS for p in tok.split("-")):
                continue
            if tok not in order:
                order.append(tok)
            if cue_pos is not None and m.start() > cue_pos:
                negated.add(tok)
            else:
                affirmed.add(tok)
    return [t for t in order if t in affirmed or t not in negated]


# ── Survival ──────────────────────────────────────────────────────────────────

def _entity_blob(e: Any) -> str:
    """Every free-text field on an entity that could carry a modifier."""
    parts = [str(getattr(e, f, "") or "") for f in
             ("name", "kind", "shape", "color_role", "behavior",
              "model_hint", "sketchfab_query", "tag")]
    return " ".join(parts).lower()


def _schema_blob(schema: Any) -> str:
    """Everywhere in the built schema a demanded word could legitimately land —
    an entity, a material, a VFX, a rule, a mission description."""
    chunks: list[str] = []
    for e in (getattr(schema, "entities", None) or []):
        chunks.append(_entity_blob(e))
    for attr in ("materials", "vfx", "shaders", "animations", "audio",
                 "characters", "missions", "world", "scenes"):
        val = getattr(schema, attr, None)
        if val is None:
            continue
        chunks.append(_stringify(val))
    meta = getattr(schema, "meta", None)
    if isinstance(meta, dict):
        # user_prompt lives here and would make every check trivially pass.
        chunks.append(" ".join(str(v) for k, v in meta.items()
                               if k not in ("user_prompt", "concept")))
    return " ".join(chunks).lower()


def _stringify(val: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if val is None or isinstance(val, (int, float, bool)):
        return str(val)
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return " ".join(_stringify(v, depth + 1) for v in val.values())
    if isinstance(val, (list, tuple, set)):
        return " ".join(_stringify(v, depth + 1) for v in val)
    dumper = getattr(val, "model_dump", None)
    if callable(dumper):
        try:
            return _stringify(dumper(), depth + 1)
        except Exception:      # noqa: BLE001 — a report must never fail a build
            return ""
    return str(val)


def _word_present(word: str, blob: str) -> bool:
    """Is this demanded word anywhere in the output?

    A number word counts as its digit and vice versa — a schema that wrote
    `count: 6` honoured "six", and failing it for spelling would be noise.
    """
    forms = {word}
    if word in _NUMBER_WORDS:
        forms.add(str(_NUMBER_WORDS[word]))
    if word.isdigit():
        forms |= {k for k, v in _NUMBER_WORDS.items() if str(v) == word}
    # Number agreement is not a fidelity loss: a brief asking for "bowler hats"
    # is honoured by a model spec that says "bowler hat", and failing it for
    # pluralisation would be noise in a metric whose whole value is that its
    # failures mean something.
    if len(word) > 3 and not word.isdigit():
        if word.endswith("es"):
            forms.add(word[:-2])
        if word.endswith("s"):
            forms.add(word[:-1])
        else:
            forms |= {word + "s", word + "es"}
    if "-" in word:
        # `six-legged` is honoured by `six legged`, `sixlegged`, or both parts.
        forms.add(word.replace("-", " "))
        forms.add(word.replace("-", ""))
    for f in forms:
        if re.search(r"\b" + re.escape(f) + r"\b", blob):
            return True
    # A hyphenated compound also counts as honoured if every part is present.
    if "-" in word:
        return all(re.search(r"\b" + re.escape(p) + r"\b", blob)
                   for p in word.split("-") if p not in _STOPWORDS)
    return False


_ASSET_TOKEN_RE = re.compile(r"[a-z]{3,}")
# Filename noise that carries no semantics — every Kenney asset ends in one.
_ASSET_NOISE: frozenset[str] = frozenset({
    "low", "high", "poly", "mesh", "model", "prefab", "fbx", "obj", "glb",
    "gltf", "assets", "models", "resources", "default", "defaults", "kenney",
    "variant", "detailed", "simple", "large", "small", "med", "medium",
})


def _asset_tokens(path: str) -> set[str]:
    stem = re.split(r"[\\/]", path.lower())[-1]
    stem = re.sub(r"\.(fbx|obj|glb|gltf|prefab)$", "", stem)
    return {t for t in _ASSET_TOKEN_RE.findall(stem) if t not in _ASSET_NOISE}


def check_asset_fidelity(schema: Any,
                         entity_model_paths: dict[str, str]) -> list[Gap]:
    """Flag every entity whose resolved model shares NO vocabulary with it.

    This is the six-legged-hippo gap measured directly. `commit node` ->
    `marble-center-butterfly-low.fbx` has zero overlap with anything the entity
    is, and the resolver reported success. Zero overlap is the honest bar: it
    does not claim to judge whether `hippo-low.fbx` is a GOOD hippo, only that a
    marble run is not a commit node.

    `ui:` paths are built to spec by EntitySpawner rather than matched, and are
    never scored here. Neither is a `Models/Default/` path (unity_generator.py
    `_RES_ROOT`): that tier is Tier B of unity_entity_asset_resolver.py's
    chain, the curated animation-rigged pack a `player`/`npc`/`prop` kind
    resolves to by ROLE rather than by name — it never claimed a semantic
    match to the entity's own name in the first place (a brief calling its
    hero "Operative" still needs the one rigged default player body, not a
    Sketchfab search for "operative"), so judging it against the entity's
    name vocabulary is grading a promise this tier never made. Confirmed live
    (DeadReckoningProtocol): `Operative -> Models/Default/Player/Player` and
    `Engineer -> Models/Default/Npc/Spectator` both flagged here alongside
    genuine catalog-tier misses, mislabeling two correct, by-design
    assignments as "Step 6 unity_entity_asset_resolver (catalog tier)"
    defects.
    """
    gaps: list[Gap] = []
    by_name = {str(getattr(e, "name", "")): e
               for e in (getattr(schema, "entities", None) or [])}
    for name, path in sorted(entity_model_paths.items()):
        if not path or path.startswith("ui:") or path.startswith("Models/Default/"):
            continue
        ent = by_name.get(name)
        want = {t for t in _ASSET_TOKEN_RE.findall(
            (_entity_blob(ent) if ent is not None else name.lower()))
            if t not in _ASSET_NOISE and t not in _STOPWORDS}
        if not want:
            continue
        have = _asset_tokens(path)
        if have and not (want & have):
            gaps.append(Gap(
                kind="asset",
                demand=name,
                detail=f"resolved to {path.split('/')[-1]} - no shared "
                       f"vocabulary with the entity it represents",
                owner="Step 6 unity_entity_asset_resolver (catalog tier)"))
    return gaps


def check_fidelity(brief: str, schema: Any, *,
                   nouns: Optional[Sequence[str]] = None,
                   entity_model_paths: Optional[dict[str, str]] = None
                   ) -> FidelityReport:
    """Grade how much of the brief's concrete vocabulary reached the build."""
    rep = FidelityReport()
    if nouns is None:
        try:
            from .unity_concept_entities import extract_named_entities
            nouns = [n["name"] for n in extract_named_entities(brief)]
        except Exception:          # noqa: BLE001 — a report never fails a build
            nouns = []
        # `extract_named_entities` only finds entities in a STRUCTURED brief.
        # A vibe prompt — "purple hippos with six legs, hurling custard pies" —
        # yields none, which produced 0 demands, 0 losses and a triumphant
        # 100/100 measuring nothing at all. That is the silent-default failure
        # this module exists to catch, reproduced inside the instrument.
        #
        # The schema's own entity names are the honest fallback: they are what
        # the build actually produced, and asking "did the words the brief
        # attached to this thing survive" is exactly the question. A build that
        # dropped the entity outright is already an ERROR under
        # unity_playability's brief_entities check.
        if not nouns:
            nouns = [str(getattr(e, "name", "") or "")
                     for e in (getattr(schema, "entities", None) or [])]

    rep.demands = extract_demands(brief, nouns)
    blob = _schema_blob(schema)

    for d in rep.demands:
        if not _word_present(d.noun.lower(), blob):
            rep.gaps.append(Gap("noun", d.noun,
                                "named in the brief, absent from the build",
                                "Step 3 schema-fill"))
        for m in d.modifiers:
            if not _word_present(m, blob):
                rep.gaps.append(Gap(
                    "modifier", f"{m} {d.noun}",
                    f"'{m}' qualifies '{d.noun}' in the brief and appears "
                    f"nowhere in the build",
                    "Step 3 schema-fill / Step 6 model spec"))

    # Free-standing compounds, so a binding miss never silently drops a word.
    bound = {m for d in rep.demands for m in d.modifiers}
    for tok in extract_free_modifiers(brief):
        if tok in bound or _word_present(tok, blob):
            continue
        rep.demands.append(Demand(noun=tok, modifiers=(), phrase=tok))
        rep.gaps.append(Gap(
            "modifier", tok,
            f"'{tok}' is an explicit commitment in the brief and appears "
            f"nowhere in the build",
            "Step 3 schema-fill / Step 6 model spec"))

    if entity_model_paths:
        rep.assets_checked = sum(
            1 for p in entity_model_paths.values()
            if p and not str(p).startswith("ui:"))
        rep.gaps.extend(check_asset_fidelity(schema, entity_model_paths))
    return rep


def render_scorecard(rep: FidelityReport, *, limit: int = 12) -> str:
    """Plain-text scorecard, same shape as the playability one.

    ASCII only. The box-drawing characters this started with crashed a plain
    `print()` under the Windows console's cp1252 codec — a report that kills the
    run it is reporting on is worse than no report.
    """
    head = (f"CONCEPT FIDELITY {rep.score}/100  "
            f"({rep.survived}/{rep.checked} demanded words survived, "
            f"{len(rep.gaps)} lost)")
    if not rep.gaps:
        return head + "\n  every concrete word the brief committed to is present"
    lines = [head]
    # Assets first: a wrong model is a bigger loss than a missing adjective.
    for g in sorted(rep.gaps, key=lambda g: (g.kind != "asset", g.kind, g.demand))[:limit]:
        lines.append(f"  LOST  [{g.kind}] {g.demand}")
        lines.append(f"          {g.detail}")
        lines.append(f"          owner: {g.owner}")
    if len(rep.gaps) > limit:
        lines.append(f"  ... and {len(rep.gaps) - limit} more")
    return "\n".join(lines)
