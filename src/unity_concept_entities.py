"""
Concept-named entity rescue — restores the brief's own "ENTITIES" bullets
when the Step-3 schema-fill LLM silently drops them.

Hit live on ToyRacerArcade: the concept explicitly named 6 gameplay entities
(PlayerCar, 3 rival cars, Coin, BoostPad), but the schema-fill LLM wrote 5
*different* generic scene-dressing groups (TrackSegments, BarrierWall,
CheckpointBanner, SpectatorCrowd, PitBuilding) and kept only PlayerCar —
losing every AI opponent and pickup the design actually needed. Playability
was graded 0/10 as a direct result: no rivals to race, no coins, no boost
mechanic.

Mirrors unity_racetrack.compose_racetrack's "trust non-degenerate LLM output,
rescue degenerate output" contract, but keyed on named-entity coverage
instead of kind coverage: it never touches a schema that already kept every
concept-named entity, and only APPENDS the ones that went missing — it never
mutates or removes what the LLM wrote. Deterministic regex parse of the
ORIGINAL user_prompt (schema.meta['user_prompt']), not the Step-2 LLM concept
prose, which already paraphrases specific names away.

A bullet is read as its whole logical line — the brief's own wrap at ~80 cols
is folded back together first — so the "(kind: ...)", "model_hint" and
"count N" it states all survive the parse and reach the rescued EntitySpec.
"""

from __future__ import annotations

import re
from typing import TypedDict

from .unity_archetypes     import _normalize_for_matching
from .unity_asset_catalog import tokenize
from .unity_entity_asset_resolver import _STOPWORDS
from .unity_schema import EntitySpec


class _KindDefaults(TypedDict):
    shape: str
    placement: str
    count: int
    scale: float
    tag: str
    color_role: str

# A line that is ALL-CAPS (allowing digits/spaces/&/'/- ) marks the next
# section header in the structured-brief format (see unity_pipeline.py's
# _BRIEF_MARKER_RE) — used to know where the ENTITIES bullet list ends.
_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 /&'-]{1,40}$")
_BULLET_RE = re.compile(r"^([A-Za-z][\w'-]*)\s*:\s*(.+)$")
_KIND_PAREN_RE = re.compile(r"\(kind:\s*([^)]*)\)", re.IGNORECASE)

# ── kind-labeled bullets ─────────────────────────────────────────────────────
# A brief may label each bullet with the entity's KIND instead of naming it:
#     - Player:      lead skydiver in jumpsuit and helmet
#     - Enemy:       formation AI skydivers (3)
#     - Collectible: altitude gate ring
#     - Collectible: formation lock target
# rather than the "- PascalCaseName: ... (kind: x)" form. Read literally that
# format broke the rescue three ways at once (live on DropZone): every bullet
# came back kind="" and degraded to "prop" (so the player never got its tag and
# the enemies never got AI), and the two "Collectible" bullets collided in the
# name-keyed dedup so one was dropped by the rescue itself.
#
# Only consulted when the bullet carries NO explicit "(kind: ...)" clause — a
# brief that writes "- Player: the astronaut (kind: player)" is naming the
# entity "Player" on purpose and keeps that name.
_KIND_LABELS: frozenset[str] = frozenset({
    "player", "enemy", "npc", "boss", "companion", "character", "creature",
    "collectible", "pickup", "item", "weapon", "vehicle", "prop", "building",
    "obstacle", "hazard", "crowd", "platform", "ramp", "wall", "road", "rail",
    "tree", "decor", "ground", "terrain", "fx",
})
# A bare parenthesised integer states the group's size in the kind-labeled
# format ("formation AI skydivers (3)"), where there is no "count N" clause.
# Bounded like _COUNT_RE so a stray "(2024)" can't spawn a swarm.
_PAREN_COUNT_RE = re.compile(r"\((\d{1,3})\)")
# A number word in DETERMINER position ("three team support tents") — only at
# the head of the description, where it can only be a quantity. Mid-sentence
# number words are prose ("all four divers must reach") and are left alone.
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12}
_LEADING_NUMBER_WORD_RE = re.compile(
    r"^\s*(?:a\s+set\s+of\s+|a\s+pair\s+of\s+)?"
    rf"({'|'.join(_NUMBER_WORDS)})\b\s+", re.IGNORECASE)
# Head noun phrase of a description — everything up to the first clause break.
# "altitude gate ring (fly through for bonus score)" -> "altitude gate ring".
_HEAD_CLAUSE_RE = re.compile(r"^[^(,;.]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# Filler that carries no identity in a derived name — dropped so
# "the player's lead skydiver in a jumpsuit" reads as "LeadSkydiver".
_NAME_FILLER = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "for", "with", "and", "or",
    "to", "from", "this", "that", "its", "his", "her", "their", "player",
    "players", "game", "scene", "level", "world", "own",
})
_NAME_MAX_WORDS = 3
# The kind token ends at the first "," or "/" — the documented brief format is
# `(kind: character, model_hint "diver in wetsuit")`, so splitting on "/" alone
# captured the whole model_hint clause as part of the kind.
_KIND_END_RE = re.compile(r"[,/]|\bmodel_hint\b", re.IGNORECASE)
# The brief states its own model_hint; carry it through rather than
# re-deriving one from the entity name (see _model_hint).
_MODEL_HINT_RE = re.compile(
    r"model_hint\s*[:=]?\s*[\"'“‘]([^\"'”’]+)[\"'”’]",
    re.IGNORECASE)
# Per-bullet instance count ("count 3." / "count: 12") — overrides the
# per-kind default below. Bounded so a typo can't spawn a scene-killing swarm.
_COUNT_RE = re.compile(r"[,;]?\s*\bcount\s*[:=]?\s*(\d{1,4})\b", re.IGNORECASE)
_MAX_COUNT = 500

# Deterministic per-kind defaults for a rescued entity. Anything not listed
# falls back to _DEFAULT_KIND — a single centered placeholder, the same
# conservative default EntitySpec itself uses.
_KIND_DEFAULTS: dict[str, _KindDefaults] = {
    "collectible": dict(shape="sphere", placement="scatter", count=12,
                        scale=0.5, tag="Collectible", color_role="accent"),
    "prop":        dict(shape="cube",   placement="scatter", count=6,
                        scale=1.0, tag="Untagged", color_role="accent"),
    "vehicle":     dict(shape="cube",   placement="single",  count=1,
                        scale=1.0, tag="Untagged", color_role="secondary"),
    "obstacle":    dict(shape="cylinder", placement="scatter", count=8,
                        scale=0.6, tag="Untagged", color_role="accent"),
    # "enemy" is a first-class kind in the schema prompt and separately drives
    # AI-script auto-attach in unity_generator — it had no entry here, so a
    # rescued enemy fell through to _DEFAULT_KIND and landed Untagged with no
    # AI target tag.
    "enemy":       dict(shape="capsule", placement="line", count=1,
                        scale=1.0, tag="Enemy", color_role="accent"),
}
_DEFAULT_KIND: _KindDefaults = dict(shape="cube", placement="single", count=1,
                     scale=1.0, tag="Untagged", color_role="secondary")


# ── hostility detection → tag="Enemy" ────────────────────────────────────────
# The Enemy tag is runtime-load-bearing, not cosmetic: the composed AI micro
# acquires its target with GameObject.FindGameObjectWithTag (templates/unity/
# micro/ai/AI.cs.j2), and EntitySpawner only attaches a collider to a group
# that carries a tag (templates/unity/EntitySpawner.cs.j2). An untagged hostile
# is inert scenery — it cannot be seen, hit, or chased. The old rule gated the
# tag on kind=="vehicle", which covered racing rivals and nothing else (hit
# live on harbor_light_salvage: ScavengerDrone, a "(kind: character) ... attack
# the player on sight", came back Untagged).

# Kinds that ARE the hostility — no wording required.
_ENEMY_KINDS = {"enemy", "hostile"}

# Nouns that mean hostile on their own, whatever the entity's kind.
_HOSTILE_NOUNS_RE = re.compile(
    r"\b(?:rival|enem(?:y|ies)|hostile|opponent|adversar(?:y|ies)|foe|attacker)s?\b",
    re.IGNORECASE)

# Aggression aimed AT the player. Deliberately a verb+object pattern rather
# than bare verbs: alone those verbs are ambiguous — "hunt for salvage" is not
# hostility, "attacks the player" is. Matching bare verbs would tag friendly
# NPCs and scenery.
_HOSTILE_TOWARD_PLAYER_RE = re.compile(
    r"\b(?:attack|chase|hunt|ambush|shoot|charge|swarm|stalk)\w*\s+"
    r"(?:at\s+|after\s+|down\s+)?(?:the\s+)?player\b",
    re.IGNORECASE)

# "AI" means computer-controlled, not hostile — an AI companion is friendly.
# It stays a VEHICLE-only signal because "AI car" / "AI rider" is idiomatic for
# racing opponents; broadening it would tag every friendly AI NPC as an enemy.
_AI_WORD_RE = re.compile(r"\bai\b", re.IGNORECASE)

# The player is never an enemy, however the bullet is worded — hostile phrasing
# legitimately appears in the PLAYER's own description ("sneaks past hostile
# drones"). The name test is a bare prefix rather than \b-anchored because
# brief entity names are PascalCase ("PlayerDiver"), where \b never fires
# between "Player" and "Diver".
_PLAYER_NAME_RE = re.compile(r"^player", re.IGNORECASE)
_PLAYER_DESC_RE = re.compile(
    r"\bthe player's\b|\bplayer[- ]controlled\b|\byou play as\b|"
    r"\bthe player (?:is|starts|begins|spawns)\b",
    re.IGNORECASE)

# Kinds that can plausibly BE the player's avatar. An allowlist, not a
# denylist: mis-tagging is strictly worse than not tagging, so an unrecognized
# kind falls back to Untagged rather than gambling the Player tag.
_AVATAR_KINDS = {"character", "player", "npc", "creature", "vehicle"}


def _is_player(name: str, description: str, kind: str = "") -> bool:
    """Identify the player's own entity. Deliberately narrower than "mentions
    the player": a hostile bullet almost always names the player as its victim
    ("attack the player on sight").

    kind=="player" is the brief declaring it outright — load-bearing for the
    kind-labeled bullet format, where the name is derived from the description
    ("- Player: lead skydiver ..." -> LeadSkydiverJumpsuit) and so no longer
    carries the "Player" prefix the name test looks for."""
    return bool(kind.strip().lower() == "player"
                or _PLAYER_NAME_RE.match(name.strip())
                or _PLAYER_DESC_RE.search(description))


def _is_hostile(name: str, kind: str, description: str) -> bool:
    """True when a rescued entity should carry tag="Enemy"."""
    if _is_player(name, description, kind):
        return False
    if kind in _ENEMY_KINDS:
        return True
    # Entity names are PascalCase ("RivalRunner"), where \b never fires between
    # the words — split the compounds first (same normalization the intent
    # classifier uses) so a name-only hostility signal is visible.
    text = _normalize_for_matching(f"{name} {description}")
    if _HOSTILE_NOUNS_RE.search(text) or _HOSTILE_TOWARD_PLAYER_RE.search(text):
        return True
    return kind == "vehicle" and bool(_AI_WORD_RE.search(text))


def _is_player_avatar(name: str, kind: str, description: str) -> bool:
    """The object the camera follows and every CompareTag(playerTag) gate
    latches onto — not merely something the player owns. "PlayerWetsuit
    (kind: prop)" is player-associated but is NOT the avatar."""
    return kind in _AVATAR_KINDS and _is_player(name, description, kind)


def _fold_bullets(lines: list[str]) -> list[str]:
    """Fold the ENTITIES bullet list into one logical string per bullet.

    Structured briefs wrap at ~80 cols, so a bullet's "(kind: ...)" clause
    routinely straddles a line break — parsing line-by-line loses it. A
    non-bullet line continues the bullet above it when it is INDENTED (the
    documented wrap style) or when the bullet so far has an unclosed "(";
    anything else (an unindented trailing paragraph, a blank line, the next
    section header) closes the bullet instead of being absorbed into it.
    """
    bullets: list[str] = []
    pending: list[str] = []

    def close() -> None:
        if pending:
            bullets.append(" ".join(pending))
            pending.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close()
            continue
        if stripped.startswith("-"):
            close()
            pending.append(stripped.lstrip("-").strip())
        elif _SECTION_HEADER_RE.match(stripped):
            break  # next section header — the ENTITIES list is over
        elif pending and (line[:1].isspace()
                          or "".join(pending).count("(")
                          > "".join(pending).count(")")):
            pending.append(stripped)  # wrapped continuation of the bullet above
        else:
            close()  # free-form prose under the list — not part of a bullet
    close()
    return bullets


def _head_words(description: str) -> list[str]:
    """Content words of a description's head noun phrase, filler removed —
    "altitude gate ring (fly through for bonus score)" -> [altitude, gate,
    ring]. The identity of a kind-labeled bullet lives entirely here, so it
    feeds both the derived name and the derived model_hint."""
    head_m = _HEAD_CLAUSE_RE.match(description or "")
    head = head_m.group(0) if head_m else ""
    return [w for w in _WORD_RE.findall(head) if w.lower() not in _NAME_FILLER]


def _derive_name(words: list[str], kind: str, taken: set[str]) -> str:
    """PascalCase group name from a kind-labeled bullet's own description.

    The label was the KIND ("- Collectible: altitude gate ring"), so the name
    has to come from the words the brief actually used — both to keep the two
    "Collectible" bullets distinct (they used to collide and one was dropped
    by the rescue's name-keyed dedup) and to give the asset resolver a real
    noun phrase to match a model on. Falls back to the capitalized kind,
    uniquified, when the description has no usable words.
    """
    name = "".join(w[:1].upper() + w[1:] for w in words[:_NAME_MAX_WORDS])
    if not name:
        name = (kind or "entity").capitalize()
    base, n = name, 2
    while name.lower() in taken:
        name, n = f"{base}{n}", n + 1
    return name


def extract_named_entities(user_prompt: str) -> list[dict]:
    """Parse the brief's own "ENTITIES" section: bullets of the form
    "- Name: description (kind: x, model_hint "y"), count N." Returns
    [{"name", "description", "kind", "model_hint", "count"}, ...] in source
    order ("model_hint" "" and "count" None when the bullet omits them); []
    if the brief has no recognizable ENTITIES section (e.g. a one-line vibe
    prompt).

    A bullet whose label is a bare KIND word and which states no "(kind: ...)"
    clause ("- Collectible: altitude gate ring") is read the other way round:
    the label becomes the kind and the name is derived from the description
    (see _derive_name), so repeated labels stay distinct instead of colliding.
    """
    lines = (user_prompt or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper().rstrip(":") == "ENTITIES":
            start = i + 1
            break
    if start is None:
        return []

    out: list[dict] = []
    taken: set[str] = set()
    for bullet in _fold_bullets(lines[start:]):
        m = _BULLET_RE.match(bullet)
        if not m:
            continue
        name, desc = m.group(1), m.group(2)

        kind = ""
        kind_m = _KIND_PAREN_RE.search(desc)
        if kind_m:
            kind = _KIND_END_RE.split(kind_m.group(1))[0].strip().lower()
            desc = _KIND_PAREN_RE.sub("", desc)

        model_hint = ""
        hint_m = _MODEL_HINT_RE.search(bullet)
        if hint_m:
            model_hint = " ".join(hint_m.group(1).split())

        count = None
        count_m = _COUNT_RE.search(bullet)
        if count_m and 1 <= int(count_m.group(1)) <= _MAX_COUNT:
            count = int(count_m.group(1))
        desc = _COUNT_RE.sub("", desc)

        # Label-as-kind. Deliberately gated on the bullet stating no kind of
        # its own: "- Player: the astronaut (kind: player)" is naming the
        # entity "Player" on purpose and must keep that name.
        label_is_kind = not kind and name.lower() in _KIND_LABELS
        if label_is_kind:
            kind = name.lower()
            # This format carries no "count N" clause, so the group size —
            # when stated at all — is a bare "(3)" or a leading number word.
            if count is None:
                paren_m = _PAREN_COUNT_RE.search(desc)
                word_m = _LEADING_NUMBER_WORD_RE.match(desc)
                if paren_m and 1 <= int(paren_m.group(1)) <= _MAX_COUNT:
                    count = int(paren_m.group(1))
                elif word_m:
                    count = _NUMBER_WORDS[word_m.group(1).lower()]
            desc = _PAREN_COUNT_RE.sub("", desc)

        # Excising the "(kind: ...)" / "count N" clauses leaves orphaned
        # whitespace before the punctuation that followed them.
        desc = re.sub(r"\s+([,.;:])", r"\1", " ".join(desc.split()))
        desc = desc.strip().rstrip(".").strip().rstrip(",")

        if label_is_kind:
            words = _head_words(desc)
            name = _derive_name(words, kind, taken)
            # The brief's own noun phrase IS the model_hint the resolver wants
            # ("2-5 plain words naming the PHYSICAL OBJECT"). Taking it from
            # here rather than re-tokenizing the derived name matters because
            # the derived name has no word boundaries left to split on —
            # "FormationAISkydivers" tokenizes to "formation aiskydivers",
            # which matches no model in the catalog.
            if not model_hint and words:
                model_hint = " ".join(w.lower() for w in words[:4])
        taken.add(name.lower())
        out.append({"name": name, "description": desc, "kind": kind,
                    "model_hint": model_hint, "count": count})
    return out


# Bounds for the Step-3 checklist below — the schema itself caps `entities` at
# 40 groups, and a description longer than this is prose the LLM does not need
# repeated back at it.
_CHECKLIST_MAX_ENTITIES = 40
_CHECKLIST_MAX_DESC = 160


def render_entity_checklist(user_prompt: str) -> str:
    """Render the brief's own ENTITIES bullets as a mandatory checklist for the
    Step-3 schema-fill prompt. "" when the brief has no ENTITIES section.

    THE CAUSE this module's rescue was written to contain: Step 3 is handed the
    Step-2 LLM `concept` and nothing else — the raw brief never reaches it (see
    unity_pipeline.run's UNITY_SCHEMA_FILL_USER.format call). So a brief that
    names its entities explicitly has already had those names paraphrased away
    by the time the schema-fill model runs, and "the LLM dropped four explicit
    entities" is really "the LLM was never shown them". Hit live on DropZone:
    of five explicitly named ENTITIES bullets, four never reached the schema.

    Deterministic string assembly over the same deterministic parse the rescue
    uses, so the names asked for here are byte-identical to the names the
    rescue would restore — the LLM honouring the checklist and the fixer
    repairing it converge on one entity list instead of two overlapping ones.
    """
    named = extract_named_entities(user_prompt)
    if not named:
        return ""
    lines = [
        "REQUIRED ENTITIES — the brief names these groups explicitly. Your",
        '"entities" array MUST contain one group for EACH line below, using',
        "EXACTLY the name and kind given. These ARE the game's content: a line",
        "you leave out is a thing the player cannot see, reach or use. Add",
        "further groups only for scene dressing the brief also implies.",
    ]
    for i, ent in enumerate(named[:_CHECKLIST_MAX_ENTITIES], start=1):
        head = f'  {i}. name="{ent["name"]}"  kind="{ent["kind"] or "prop"}"'
        if ent.get("count"):
            head += f'  count={ent["count"]}'
        if ent.get("model_hint"):
            head += f'  model_hint="{ent["model_hint"]}"'
        lines.append(head)
        desc = " ".join((ent.get("description") or "").split())
        if desc:
            lines.append(f"     {desc[:_CHECKLIST_MAX_DESC]}")
    return "\n".join(lines) + "\n"


def _model_hint(name: str, kind: str, description: str) -> str:
    words = [t for t in tokenize(name) if t not in _STOPWORDS]
    if not words:
        words = [t for t in tokenize(description) if t not in _STOPWORDS][:3]
    if kind and kind != "prop" and kind not in words:
        words.append(kind)
    return " ".join(words[:4]) or (kind or "prop")


def inject_missing_concept_entities(schema) -> list[str]:
    """Append an EntitySpec for every brief-named entity the schema-fill
    step dropped. Mutates schema.entities in place (append-only) and returns
    fixer log messages in the same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_entities(meta.get("user_prompt", ""))
    if not named:
        return []

    existing = {e.name.strip().lower() for e in schema.entities}
    # Exactly one object may carry tag="Player": Unity's FindGameObjectWithTag
    # returns a single arbitrary match, so a second one makes camera-follow,
    # interaction, transitions and dialog latch onto a nondeterministic target.
    # If the schema-fill LLM already tagged a player, it wins.
    player_tagged = any(e.tag == "Player" for e in schema.entities)
    added: list[EntitySpec] = []
    for ent in named:
        key = ent["name"].strip().lower()
        if not key or key in existing:
            continue
        kind = ent["kind"] or "prop"
        defaults = _KIND_DEFAULTS.get(kind, _DEFAULT_KIND)
        tag = defaults["tag"]
        is_avatar = False
        if _is_hostile(ent["name"], kind, ent["description"]):
            tag = "Enemy"
        elif not player_tagged and _is_player_avatar(
                ent["name"], kind, ent["description"]):
            # Left Untagged before, so every CompareTag(playerTag) gate in the
            # interactable / transition / npc_dialog micros silently never fired.
            tag = "Player"
            is_avatar = player_tagged = True
        # The brief's own "count N" wins over the per-kind default; "single"
        # placement bakes exactly one position (see unity_layout._bake_positions),
        # so a group the brief wants repeated has to scatter instead.
        count = ent.get("count") or defaults["count"]
        # The avatar is singular by definition; a group count would spawn N
        # Player-tagged objects and break the single-match invariant above.
        if is_avatar:
            count = 1
        placement = defaults["placement"]
        if count > 1 and placement == "single":
            placement = "scatter"
        added.append(EntitySpec(
            name=ent["name"], kind=kind, count=count,
            shape=defaults["shape"], placement=placement,
            color_role=defaults["color_role"], scale=defaults["scale"],
            tag=tag,
            model_hint=(ent.get("model_hint")
                        or _model_hint(ent["name"], kind, ent["description"])),
        ))
        existing.add(key)

    if not added:
        return []
    schema.entities = list(schema.entities) + added
    return [
        f"Concept named {len(added)} entit"
        f"{'y' if len(added) == 1 else 'ies'} the schema-fill dropped "
        f"({', '.join(e.name for e in added)}) — restored from the brief's "
        "own ENTITIES section instead of leaving them out of the scene."
    ]
