"""Board micro-generator — the DOMAIN MODEL and the LEGAL-MOVE logic.

Why this exists
---------------
The rules micro (micro/rules.py) tells the player "drag every file icon into a
folder container". Nothing made dragging a file icon into a folder container a
thing that works. A board game shipped as a scatter of plates the pointer could
click and nothing else: the instruction was true prose about a mechanic that did
not exist.

This micro is that mechanic. It owns the two things `rules` deliberately does
not:

    the domain model   every piece, every zone, and WHERE each piece currently is
    the legal move     whether this piece may go in that zone, and if not, why

and it exposes both through the two surfaces a board game actually has — a
DRAG and a TYPED COMMAND — over one shared move core, so the two can never
disagree about what is legal.

What it binds to
----------------
Nothing here spawns anything. A non-embodied project's content is already in the
scene: `EntitySpawner` builds one plate per entity instance (Tier U, the
`ui:<element>` route) under a group GameObject named after the entity. So the
board ADOPTS those objects — the icons and cards of the cast become pieces, the
panels become zones — rather than building a second, parallel board that the
layout pass never placed.

The events are the honest event source a UI-first game never had. SEAM 8 could
only bind a mission objective to a trigger volume or an interactable, both of
which wait for a body to arrive; a game with no avatar has none, so its
objectives bound to events that could never fire. `OnPiecePlaced`,
`OnGroupComplete`, `OnZoneFilled` and `OnCommand` are things a board can really
raise.

What is deterministic and what is authored
------------------------------------------
`board_options_for_entities` derives a playable board from the entity list with
no model at all: which groups are pieces, which are zones, which zone is a
readout you cannot drop into, and whether a console exists.

`board_domain_for_missions` derives the LEGAL MOVE on top of that — which zone
accepts what, and the ordering between zones — from the mission the player is
already shown. Both halves are deterministic, because in the compose path there
is no other producer: `MicroComposer._resolve` builds this schema straight from
`Subsystem.options` with no LLM, and `enrich_subsystems` may only merge C#
fragments. A field with no producer is a field that ships empty, which is how
every composed board came to accept every piece everywhere.

The Pipeline LLM authors the rest — the verbs, the per-piece labels — on the
standalone micro path only. A board with no authored domain is still a board you
can play; that is the same rule `steps_from_mission` follows.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .code_fields import (UNITY_EXTRA_INSTRUCTIONS, CodeExtensibleSchema,
                          clamp_enum, lint_extensible)
from .unity_base import UnityMicroPipeline

# "drag"              pointer only — pick a piece up, drop it on a zone
# "terminal"          typed commands only — no zone is a drop target, or the
#                     board is a console game
# "drag_and_terminal" both surfaces over the same move core (the default when
#                     the cast declares a console)
BoardVariant = Literal["drag", "terminal", "drag_and_terminal"]
VARIANTS = list(BoardVariant.__args__)   # type: ignore[attr-defined]

# Words that make a `window` element a REAL command line rather than a picture
# of one. Kept narrow deliberately: a dashboard is a window and is not a shell.
_CONSOLE_WORDS: tuple[str, ...] = (
    "terminal", "console", "shell", "prompt", "command line", "commandline",
    "cli", "repl",
)

# The verbs `Run` answers in its own switch, BEFORE it looks at the declared
# commands (GameBoard.cs.j2). A domain verb that collides with one of these is
# not overridden — it is unreachable, silently, and an objective bound to it
# could never advance. Kept in step with the template's switch.
_BUILTIN_VERBS: frozenset[str] = frozenset({
    "help", "?", "status", "st", "zones", "places", "ls", "list",
    "move", "put", "place",
})

# Leading words that are never the action in an objective description: a
# sequencer, an article, a quantifier or an auxiliary. Skipped so "Then stage
# every modified file" yields `stage` rather than `then`.
_VERB_SKIP: frozenset[str] = frozenset({
    "the", "a", "an", "you", "your", "then", "now", "first", "next", "finally",
    "must", "should", "please", "all", "every", "each", "both", "and", "to",
})

# Words that turn up in front of a verb without being the tool that owns it.
# A command prefix is a NAME ("git commit"); these are grammar.
_PREFIX_SKIP: frozenset[str] = frozenset(_VERB_SKIP | {
    "can", "will", "may", "might", "cannot", "never", "always", "also", "just",
    "only", "before", "after", "when", "while", "once", "they", "we", "it",
    "this", "that", "these", "those", "who", "which", "there", "here", "not",
    "let", "lets", "helps", "used", "use", "using", "want", "wants", "need",
    "needs", "player", "players", "game", "goal", "step", "steps", "task",
    # Instructions ABOUT typing rather than the name of the thing typed:
    # "Type commit to finish" must not make `type` the tool.
    "type", "types", "press", "hit", "enter", "click", "run", "runs", "say",
    "write", "writes", "then", "finally", "again",
})

# The nouns that make a name a console rather than the tool it runs. Stripped
# so `GitConsole` yields `git` and `TerminalWindow` yields nothing.
_CONSOLE_TOKENS: frozenset[str] = frozenset({
    "terminal", "console", "shell", "prompt", "cli", "repl", "window",
    "command", "commandline", "line", "pane", "panel", "box", "input",
})

# A concrete instance name a brief actually commits to. Bare prose is NOT an
# item: "nine file icons" names a count, not nine names, and inventing labels
# for the plates is worse than the caption EntitySpawner already gives them.
_ITEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "README.md", 'Ace of Spades', `main.py` — an author quoting is an author
    # naming, whatever the domain.
    re.compile(r"[\"'`]([^\"'`\n]{1,40})[\"'`]"),
    # A filename with an extension. The dot is what makes it a name and not a
    # word, so it is also why sentence-splitting must not break on it.
    re.compile(r"\b([A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z][A-Za-z0-9]{0,4})\b"),
    # A code: a tail number, a flight, a part — "BA419", "N747", "SR-71".
    re.compile(r"\b([A-Z]{1,3}-?\d{2,5}[A-Z]?)\b"),
)

# Sentence end = punctuation followed by whitespace AND a capital. "main.py,
# index.html" must not split; "utils.py. The next step" must. \x00 marks a
# paragraph break, substituted in by `_clauses`.
_SENTENCE_RE = re.compile(r"[\x00;]+|(?<=[A-Za-z0-9)])[.!?]\s+(?=[A-Z])")


def _prettify(name: str) -> str:
    """"FolderContainers" -> "FOLDER CONTAINERS". The plates already caption
    themselves this way (EntitySpawner.UiCaption), so the terminal naming a
    zone and the board showing it agree."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name or "").strip())
    spaced = re.sub(r"[_\-]+", " ", spaced)
    return " ".join(spaced.split()).upper()


def _singular(name: str) -> str:
    """A display noun for one instance of a group. Crude on purpose — it names
    a thing on screen, it does not have to be grammar."""
    label = _prettify(name).title()
    if label.endswith("ies") and len(label) > 4:
        return label[:-3] + "y"
    if label.endswith("ses") or label.endswith("xes"):
        return label[:-2]
    if label.endswith("s") and not label.endswith("ss"):
        return label[:-1]
    return label


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _imperative_verb(description: str) -> str:
    """The action an objective describes, as one typeable word.

    "Stage every modified file" -> `stage`; "Record what you staged" ->
    `record`. The objective's own opening imperative, because that is the word
    the briefing already puts in front of the player: the rules micro shows the
    description in the hint bar, so a parser that answers to its first word is
    a parser the player has already been told how to use.
    """
    for tok in re.findall(r"[A-Za-z][A-Za-z'-]*", str(description or "")):
        w = tok.lower()
        if w in _VERB_SKIP:
            continue
        verb = re.sub(r"[^a-z0-9_-]+", "", w)
        return verb if len(verb) >= 3 else ""
    return ""


def _item_names(text: str) -> list[str]:
    """The concrete names a clause commits to, in the order it writes them."""
    hits: list[tuple[int, str]] = []
    for rx in _ITEM_PATTERNS:
        for m in rx.finditer(text):
            hits.append((m.start(1), " ".join(m.group(1).split())))
    out: list[str] = []
    for _pos, value in sorted(hits):
        if value and value not in out:
            out.append(value)
    return out


def _clauses(text: str) -> list[str]:
    """Split prose into clauses without breaking a wrapped list.

    A SOFT wrap is not a break: briefs arrive hard-wrapped, and treating every
    newline as a boundary cut "README.md, main.py,\\nutils.py, ..." after the
    second name and lost seven of the nine. Only a blank line separates topics.
    """
    t = re.sub(r"\n[ \t]*\n+", "\x00", str(text or ""))
    t = re.sub(r"[ \t\r\n]+", " ", t)
    return [c for c in _SENTENCE_RE.split(t) if c and c.strip()]


def _prefix_from_console(console: str, verbs: set[str]) -> str:
    """`GitConsole` -> `git`. The tool the command line belongs to, with the
    words that merely make it a command line removed."""
    rest = [t for t in _id_tokens(console) if t not in _CONSOLE_TOKENS]
    if len(rest) != 1:
        return ""
    cand = rest[0]
    return cand if cand not in verbs and cand not in _BUILTIN_VERBS else ""


def _id_tokens(name: str) -> set[str]:
    """The word set of a group id, for matching a piece to the place it belongs.

    "RedCards" -> {"red", "card"}; "FolderContainers" -> {"folder",
    "container"}. Singularised so "Cards" and "CardSlot" meet, and short words
    dropped so a stray "of"/"an" cannot pair two unrelated groups.
    """
    words = _prettify(name).lower().split()
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in words if len(w) >= 3}


def norm_ref(s: str) -> str:
    """Normalised form used to match an objective's `target_ref` to a board
    group. Case, spacing and a trailing plural all vanish, because the schema
    writes "FileIcons" and the mission writes "file icons"."""
    key = _slug(s)
    return key[:-1] if len(key) > 3 and key.endswith("s") else key


class BoardPieceGroup(BaseModel):
    """One movable cast member, with as many instances as the entity declares."""
    id:    str = ""                 # entity name — the spawned group GameObject
    label: str = ""                 # singular display noun ("File Icon")
    count: int = 1
    # Per-instance names, in spawn order. Authored ("README.md", "app.js") or
    # left empty, in which case the plate's own caption is used.
    items: list[str] = Field(default_factory=list)

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(1, min(999, int(v)))
        except (TypeError, ValueError):
            return 1


class BoardZoneGroup(BaseModel):
    """Somewhere a piece can BE. Every spawned instance of the group is its own
    drop target; the rules below are declared once for the group."""
    id:       str  = ""
    label:    str  = ""
    describe: str  = ""             # the sub-caption: what this place means
    # Piece group ids this zone takes. Empty = anything. A piece a zone refuses
    # is refused BY NAME at runtime, never silently snapped back.
    accepts:  list[str] = Field(default_factory=list)
    # A zone group id a piece must already be in before it may come here. This
    # is the ordering rule — the reason you cannot skip the middle of a
    # workflow — and it is the one piece of domain logic no vocabulary can
    # derive, so it is authored or absent.
    requires: str  = ""
    # A readout (a log, a history, a feed) is not a drop target: something has
    # to PRODUCE the entry. Derived from the UI vocabulary, overridable.
    drop_target: bool = True


class BoardCommand(BaseModel):
    """One typed verb. `to_zone` empty = a signal: it announces itself and moves
    nothing, which is what a verb like `push` or `submit` actually is."""
    verb:    str = ""
    moves:   str = ""               # piece group id; "" = whatever is named
    to_zone: str = ""               # zone group id; "" = signal only
    takes_target: bool = True       # accepts `<name>` / `.` / `all`
    help:    str = ""

    @field_validator("verb", mode="before")
    @classmethod
    def _clean_verb(cls, v) -> str:
        # One token, lowercase: the parser splits on whitespace, so a two-word
        # "verb" could never be matched and would look like a silent no-op.
        return re.sub(r"[^a-z0-9_-]+", "", str(v or "").strip().lower().split(" ")[0])


class BoardSchema(CodeExtensibleSchema):
    name:    str = "GameBoard"
    variant: str = "drag_and_terminal"
    title:   str = ""
    pieces:  list[BoardPieceGroup] = Field(default_factory=list)
    zones:   list[BoardZoneGroup]  = Field(default_factory=list)
    commands: list[BoardCommand]   = Field(default_factory=list)
    # Stripped before the verb when present ("git" in `git commit -m ...`), so
    # the domain's real command line reads the way the domain writes it.
    command_prefix: str = ""
    # The entity whose plate is a PICTURE of a console. The board replaces it
    # with a working one and says so in the log rather than leaving two.
    console_group:  str = ""
    description: str = "Board domain model, legal moves and command parser"
    meta: dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v) -> str:
        return clamp_enum(v, tuple(VARIANTS), "drag_and_terminal")


CONCEPT_PROMPT = """\
{theme_block}
Describe the RULES of this game as a board of pieces and places:

"{user_prompt}"

How the player acts on it: {variant}

Cover only:
- what the movable pieces are, and what one of them is called
- the places a piece can be, and what each place means
- which moves are legal, and which are not — and why
- the commands a player can type, if this game has a command line

Do not describe rendering, architecture or packages.
"""

SCHEMA_PROMPT = """\
You are defining the legal moves of a game. The game:

{concept}
{schema_hints}

The pieces and places this project already contains — use these ids exactly and
invent no others:
{cast}

Return ONE JSON object:
{{
  "title": "short name of the board",
  "command_prefix": "a word every command starts with, or \\"\\"",
  "pieces": [
    {{"id": "<a piece id above>", "label": "singular name for one of them",
      "items": ["name of instance 1", "name of instance 2"]}}
  ],
  "zones": [
    {{"id": "<a place id above>", "label": "SHORT NAME ON SCREEN",
      "describe": "one line: what this place means",
      "accepts": ["<piece id>"],
      "requires": "<zone id a piece must be in first, or empty>"}}
  ],
  "commands": [
    {{"verb": "one lowercase word", "moves": "<piece id or empty>",
      "to_zone": "<zone id, or empty for a command that moves nothing>",
      "help": "what typing this does"}}
  ]
}}

`requires` is the ordering rule: leave it empty unless a piece really must pass
through one place before another. Every id must be one of the ids listed above.
""" + UNITY_EXTRA_INSTRUCTIONS


class BoardPipeline(UnityMicroPipeline):
    kind            = "board"
    display_name    = "Unity Game Board"
    schema_cls      = BoardSchema
    variants        = VARIANTS
    default_variant = "drag_and_terminal"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "board"

    _RESERVED_FIELD_NAMES = {
        "pieces", "zones", "commands", "_pieces", "_zones", "_commands",
        "_dragging", "_dragPlane", "_termIn", "_termOut", "_lines", "_cam",
        "Pieces", "Zones", "TryPlace", "Run", "PlacedCount",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.UI", "UnityEngine.EventSystems",
                        "System", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "GameBoard")
        if not schema.title:
            schema.title = _prettify(schema.name).title()

        for p in schema.pieces:
            if not p.label:
                p.label = _singular(p.id)
            # Never more names than instances: an item list longer than the
            # spawned group would name pieces that are not on the board.
            del p.items[p.count:]
        for z in schema.zones:
            if not z.label:
                z.label = _prettify(z.id)

        # Verbs are the parser's dispatch keys: two commands sharing one is a
        # command the player can type and never reach.
        seen: set[str] = set()
        kept: list[BoardCommand] = []
        for c in schema.commands:
            if not c.verb or c.verb in seen:
                continue
            seen.add(c.verb)
            if not c.help:
                c.help = (f"move {c.moves or 'a piece'} to {c.to_zone}"
                          if c.to_zone else f"{c.verb} (announces itself)")
            kept.append(c)
        schema.commands = kept

        # A board with no drop target has no drag move, whatever the variant
        # says — fall back to the surface that still works rather than shipping
        # a pointer that can never place anything.
        if not any(z.drop_target for z in schema.zones) and variant != "terminal":
            schema.variant = "terminal"
        return schema

    def harden(self, schema) -> list[str]:
        msgs: list[str] = []
        piece_ids = {p.id for p in schema.pieces if p.id}
        zone_ids  = {z.id for z in schema.zones if z.id}

        if not piece_ids:
            msgs.append("board: no pieces — there is nothing to move")
        if not zone_ids:
            msgs.append("board: no zones — there is nowhere to move anything")

        # A piece no zone will accept can never be placed. Same defect class as
        # the rules micro's stranded step: named, never silently dropped.
        for pid in sorted(piece_ids):
            takers = [z for z in schema.zones
                      if not z.accepts or pid in z.accepts]
            if not takers:
                msgs.append(f"board: no zone accepts {pid} — those pieces can "
                            f"never be placed. Zones: {', '.join(sorted(zone_ids))}")

        for z in schema.zones:
            unknown = [a for a in z.accepts if a not in piece_ids]
            if unknown:
                msgs.append(f"board: zone {z.id} accepts {', '.join(unknown)}, "
                            f"which no piece group declares")
            if z.requires and z.requires not in zone_ids:
                msgs.append(f"board: zone {z.id} requires {z.requires!r}, which "
                            f"is not a zone — nothing could ever satisfy it")
            if z.requires == z.id:
                msgs.append(f"board: zone {z.id} requires itself — no piece can "
                            f"enter it")

        for c in schema.commands:
            # `Run` switches on the built-ins before it reads `commands`, so a
            # colliding verb is not overridden — it is unreachable, and an
            # objective bound to it could never advance.
            if c.verb in _BUILTIN_VERBS:
                msgs.append(f"board: command {c.verb!r} is a verb the parser "
                            f"answers itself, so this one can never run")
            if c.to_zone and c.to_zone not in zone_ids:
                msgs.append(f"board: command {c.verb!r} moves pieces to "
                            f"{c.to_zone!r}, which is not a zone")
            if c.moves and c.moves not in piece_ids:
                msgs.append(f"board: command {c.verb!r} moves {c.moves!r}, "
                            f"which is not a piece group")

        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return msgs

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts = out_dir / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / f"{schema.name}.cs").write_text(
            env.get_template("GameBoard.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8")
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8")
        return 2


# ── Deterministic derivation ─────────────────────────────────────────────────

def is_console_entity(entity) -> bool:
    """A `window` element that is a real command line rather than a display."""
    from ..unity_layout_archetype import ui_element_of
    if ui_element_of(entity) != "window":
        return False
    text = f"{getattr(entity, 'name', '')} {getattr(entity, 'description', '')}".lower()
    return any(w in text for w in _CONSOLE_WORDS)


# Verbs whose OBJECT is a thing you move and whose destination is a place.
# Read off the objective text, which is the one place the brief states the move
# in the player's own words.
_MOVE_VERBS: tuple[str, ...] = (
    "drag", "drop", "place", "put", "move", "stack", "sort", "deal", "play",
    "slot", "load", "file", "post", "assign", "throw", "toss", "set",
)
# What separates the thing moved from the place it goes.
_DESTINATION_PREPS: tuple[str, ...] = (
    " into ", " onto ", " on to ", " in to ", " to the ", " in the ", " on the ",
    " into the ", " onto the ", " across ", " at the ",
)


def _objectives_of(mission) -> list:
    """The objective sequence of one MissionSpec or a list of them, in schema
    order. Every caller here reads ALL of them: an objective from the third
    mission is as real as one from the first, and SEAM 8a binds it either way."""
    missions = ([m for m in mission if m is not None]
                if isinstance(mission, (list, tuple)) else
                ([mission] if mission is not None else []))
    return [o for m in missions for o in (getattr(m, "objectives", None) or [])]


def _roles_from_objectives(mission, names: "set[str]") -> "tuple[set[str], set[str]]":
    """`(moved, destinations)` — entity names the MISSIONS say move, and where.

    The vocabulary in `ui_element_of` is a closed list, and a closed list can
    never cover every game: "a Jenga game using high-heeled shoes played by
    hamsters" has pieces called shoes, and no word table will ever say a shoe
    is a piece. Measured on ordinary board-game nouns the list missed 27 of 39
    before it was extended, and extending it only moves the boundary.

    The brief itself does not have that problem. "Drag all file icons into a
    folder container" names the thing moved and the place it goes, in the
    player's own words — so when the vocabulary cannot classify a cast, the
    objectives can. Matching is on the entity NAMES the schema already
    declares, so nothing here invents a group.
    """
    moved: set[str] = set()
    dest:  set[str] = set()
    for o in _objectives_of(mission):
        text = " " + str(getattr(o, "description", "") or "").lower() + " "
        ref  = str(getattr(o, "target_ref", "") or "").strip()
        if not any(f" {v}" in text for v in _MOVE_VERBS):
            continue
        # Split the sentence at the preposition: what precedes is carried,
        # what follows is arrived at.
        head, tail = text, ""
        for p in _DESTINATION_PREPS:
            if p in text:
                head, tail = text.split(p, 1)
                break
        for n in names:
            toks = _id_tokens(n)
            if not toks:
                continue
            if all(t in head for t in toks):
                moved.add(n)
            elif tail and all(t in tail for t in toks):
                dest.add(n)
        # An objective whose target_ref names a real group and which the text
        # puts on the carried side is the strongest single signal there is.
        if ref in names and ref not in dest:
            moved.add(ref)
    return moved, dest


def board_options_for_entities(entities, *, title: str = "",
                               mission=None) -> dict | None:
    """A playable board from the cast alone — no LLM.

    Returns the `Subsystem.options` for the board micro, or None when this cast
    cannot make a board: the caller composes nothing rather than a component
    that would sit in the scene with no legal move in it.

    The mapping is the UI vocabulary the layout and asset passes already agree
    on (unity_layout_archetype.ui_element_of), so a group is a piece here for
    exactly the reason it was drawn as an icon there:

        icon, card      a PIECE      the things you move
        panel           a ZONE       the places you move them to
        list            a ZONE, but not a drop target — a log is produced, not
                        filled by hand
        window(console) the COMMAND LINE
    """
    from ..unity_layout_archetype import ui_board_layer, ui_element_of

    pieces: list[dict] = []
    zones:  list[dict] = []
    console = ""
    for e in entities or []:
        name = str(getattr(e, "name", "") or "").strip()
        if not name:
            continue
        # Backdrops and chrome are not played with: a caption is not a piece
        # and a backboard is not a place.
        if ui_board_layer(e) != "board":
            continue
        element = ui_element_of(e)
        if element in ("icon", "card"):
            pieces.append({"id": name, "label": _singular(name),
                           "count": int(getattr(e, "count", 1) or 1)})
        elif element in ("panel", "list"):
            zones.append({"id": name, "label": _prettify(name),
                          "drop_target": element != "list"})
        elif element == "window" and not console and is_console_entity(e):
            console = name

    # ── Structural fallback ──────────────────────────────────────────────────
    # The vocabulary above could not make a board. Before abstaining, ask the
    # MISSION what moves where: a cast of shoes and a cast of hippos are
    # unnameable, but "drag the shoes onto the tower" is not.
    if (not pieces or not zones) and mission is not None:
        known  = {p["id"] for p in pieces} | {z["id"] for z in zones}
        by_name = {str(getattr(e, "name", "") or "").strip(): e
                   for e in (entities or [])
                   if str(getattr(e, "name", "") or "").strip()
                   and ui_board_layer(e) == "board"}
        moved, dest = _roles_from_objectives(mission, set(by_name))
        for n in sorted(moved - dest - known):
            e = by_name[n]
            pieces.append({"id": n, "label": _singular(n),
                           "count": int(getattr(e, "count", 1) or 1)})
        for n in sorted(dest - moved - known):
            zones.append({"id": n, "label": _prettify(n), "drop_target": True})

    if not pieces or not zones:
        return None
    droppable = any(z["drop_target"] for z in zones)
    if not droppable and not console:
        # Every place is a readout and there is no command line: nothing can
        # ever move. Abstain loudly at the call site rather than compose a
        # board whose only answer to every move is "no".
        return None

    variant = ("drag_and_terminal" if droppable and console
               else "drag" if droppable else "terminal")
    return {
        "variant": variant,
        "title": title or "",
        "pieces": pieces,
        "zones": zones,
        "console_group": console,
    }


def board_domain_for_missions(options: dict, mission, *,
                              brief: str = "") -> list[tuple[bool, str]]:
    """Fill every field of the domain no other producer writes.

    `board_options_for_entities` says what the pieces and places ARE. These say
    what the player may DO with them — which piece belongs where, what can be
    typed, what the pieces are called, and what order the places come in.
    Without them the only illegal moves left are "it is already there" and
    "that is a readout", the parser answers only its own built-ins, and every
    plate is captioned by its index.

    `mission` takes one MissionSpec or a list of them. EVERY mission counts:
    SEAM 8a binds an objective from any of them, so a derivation held to the
    first left the parser with no verb for two thirds of the live steps and
    ordered the places by a third of the evidence. `micro/rules.steps_from_missions`
    spans them for the same reason and in the same order, which is what keeps
    what the board ENFORCES and what the player is SHOWN two readings of one
    authored list.

    Two sources, each for what it actually knows. The MISSIONS carry the rules.
    The BRIEF carries the names, because that is where a human writes them.

        accepts         name affinity — a piece and a place that share a word
                        belong together ("RedCards" -> "RedPile"). Applied only
                        when every place is named by some piece, every piece has
                        a place, and the result actually discriminates.
        commands        the opening imperative of each objective's description,
                        which is the word the hint bar already puts in front of
                        the player. `meta.objective_commands` records which
                        objective a verb was derived for, so SEAM 8a can bind
                        the ones only typing can satisfy.
        command_prefix  the tool the command line belongs to — the word the
                        brief puts in front of a verb, else the console's own
                        name minus the words that make it a console.
        items           the concrete names a brief lists for a group, which are
                        also what `ResolvePieces` lets the player type at a
                        single piece.
        requires        the order the objectives first reach each place, read
                        across the missions in schema order. An objective naming
                        a PIECE reaches the place that accepts it, which is how a
                        mission that never names a zone still orders them.

    All-or-nothing, and abstains loudly, because a half-derived rule is worse
    than none: one place left open while its neighbours are restricted reads to
    the player as a bug in the game rather than as a rule of it.

    Mutates `options` in place. Returns (ok, message) pairs for the caller to
    print — no rich markup here, so this stays importable from a test.
    """
    out: list[tuple[bool, str]] = []
    # Schema order, across every mission — so the ordering rule derived below
    # and the step list the player works through agree beat for beat.
    objectives = _objectives_of(mission)
    piece_specs = [p for p in (options.get("pieces") or []) if p.get("id")]
    pieces = [p["id"] for p in piece_specs]
    zones  = [z for z in (options.get("zones") or []) if z.get("id")]
    drops  = [z for z in zones if z.get("drop_target")]
    # Zones, not drop targets: a terminal-only board is all readouts, and it is
    # the board that needs its verbs MOST. Gating the whole derivation on a drop
    # target left it with the parser's built-ins and nothing else.
    if not pieces or not zones:
        return out
    by_id = {z["id"]: z for z in drops}

    # ── items ────────────────────────────────────────────────────────────────
    # The names on the plates, and — because `ResolvePieces` matches on the
    # label — the words the player can type at a single piece: `stage README.md`
    # only means something once the pieces have names. Taken from the brief,
    # where a human writes them, and only from a clause that names this group
    # and lists at least two: one mention in passing is not a roster, and an
    # invented label is worse than the caption EntitySpawner already draws.
    named = []
    for spec in piece_specs:
        want = _id_tokens(spec["id"])
        for clause in _clauses(brief):
            if not want <= _id_tokens(clause):
                continue
            found = _item_names(clause)
            if len(found) < 2:
                continue
            spec["items"] = found[:int(spec.get("count") or 1)]
            named.append(f"{spec['id']}: {', '.join(spec['items'])}")
            break
    if named:
        out.append((True, "board: named pieces — " + "; ".join(named)))

    # ── accepts ──────────────────────────────────────────────────────────────
    affinity = {z["id"]: sorted(p for p in pieces
                                if _id_tokens(p) & _id_tokens(z["id"]))
                for z in drops}
    unnamed   = [zid for zid, ps in affinity.items() if not ps]
    stranded  = sorted(set(pieces) - {p for ps in affinity.values() for p in ps})
    # A rule that lets every place take every piece refuses nothing, so applying
    # it would only add noise to the board's own readout of itself.
    discriminates = any(len(ps) < len(pieces) for ps in affinity.values())

    # Nothing is dropped anywhere on an all-readout board, so there is no
    # restriction to state and no abstention worth printing — the variant was
    # already reported as terminal.
    if drops:
        if len(drops) < 2:
            reason = (f"only one place to drop into ({drops[0]['id']}), so there "
                      "is nothing to tell apart")
        elif unnamed:
            reason = (f"{', '.join(sorted(unnamed))} "
                      + ("share no word" if len(unnamed) > 1 else "shares no word")
                      + " with any piece — restricting the others would leave "
                      + ("them the places" if len(unnamed) > 1 else "it the one place")
                      + " that take anything")
        elif stranded:
            reason = (f"{', '.join(stranded)} would have nowhere legal to go")
        elif not discriminates:
            reason = "every place matched every piece, which refuses nothing"
        else:
            reason = ""
        if reason:
            out.append((False, "board: no piece/place restriction derived — "
                        + reason + "; every place takes every piece"))
        else:
            for z in drops:
                z["accepts"] = affinity[z["id"]]
            out.append((True, "board: " + "; ".join(
                f"{z['id']} takes {', '.join(affinity[z['id']])}" for z in drops)))

    # Empty `accepts` means "anything" everywhere it is read, including here.
    takes = {z["id"]: set(z.get("accepts") or pieces) for z in drops}
    zone_by_ref  = {norm_ref(z["id"]): z["id"] for z in drops}
    piece_by_ref = {norm_ref(p): p for p in pieces}
    # Readouts included: a command is the ONLY thing that can reach one.
    all_zone_by_ref = {norm_ref(z["id"]): z["id"] for z in zones}
    readouts = {z["id"] for z in zones if not z.get("drop_target")}

    def _stage_for(ref: str) -> str:
        """The place an objective is about — named directly, or reached through
        the piece it is about. Ambiguous when several places take that piece,
        and a stage guessed from an ambiguous ref is exactly the silent wrong
        answer this module exists to stop raising."""
        zid = all_zone_by_ref.get(ref, "")
        if not zid and ref in piece_by_ref:
            takers = [z for z in by_id if piece_by_ref[ref] in takes[z]]
            zid = takers[0] if len(takers) == 1 else ""
        return zid

    # ── commands ─────────────────────────────────────────────────────────────
    # The typed half of the board. Without these the parser answers only its
    # built-ins, so the domain had no verb of its own and `OnCommand` — one of
    # the four events SEAM 8a can bind an objective to — could never fire in a
    # composed build.
    #
    # A readout is why this matters rather than merely being nice: `TryPlace`
    # refuses a DRAG into one ("a history is produced, not filled in by hand"),
    # so an objective about a log can only ever be satisfied by typing. Bound
    # below to the verb, not to the zone.
    commands: list[dict] = []
    objective_commands: dict[str, str] = {}
    taken: set[str] = set()
    for obj in objectives:
        oid  = str(getattr(obj, "id", "") or "")
        verb = _imperative_verb(getattr(obj, "description", ""))
        if not verb:
            continue
        if verb in _BUILTIN_VERBS:
            out.append((False, f"board: {oid or 'an objective'} describes "
                        f"{verb!r}, which the parser answers itself before it "
                        "reads the domain's verbs — no command declared for it"))
            continue
        if verb in taken:
            out.append((False, f"board: {oid or 'an objective'} also describes "
                        f"{verb!r}, already declared by an earlier objective — "
                        "one verb cannot mean two moves, so none was added"))
            continue
        # Only an objective about something ON the board gets a verb. The board
        # cannot honestly name the action for a step it owns nothing in, and a
        # verb that announces itself and moves nothing would be a word the
        # player types to no effect — while SEAM 8a, which runs before every
        # other source, would claim the objective away from the trigger volume
        # that really raises it.
        ref = norm_ref(getattr(obj, "target_ref", "") or "")
        if ref not in piece_by_ref and ref not in all_zone_by_ref:
            continue
        to_zone = _stage_for(ref)
        moves   = piece_by_ref.get(ref, "")
        taken.add(verb)
        commands.append({
            "verb": verb, "moves": moves, "to_zone": to_zone,
            # The objective's own sentence, so `help` in the terminal and the
            # hint bar over it are the same words.
            "help": " ".join(str(getattr(obj, "description", "") or "").split()),
        })
        # Bind to the verb only where a drag CANNOT do the job. A piece
        # objective already rides OnPiecePlaced, which counts each placement —
        # rebinding it to a verb that fires once would complete a count of nine
        # on the first command.
        if oid and to_zone in readouts:
            objective_commands[oid] = verb
    if commands:
        options["commands"] = commands
        options.setdefault("meta", {})["objective_commands"] = objective_commands
        out.append((True, "board: verbs — " + ", ".join(
            c["verb"] + (f" -> {c['to_zone']}" if c["to_zone"] else " (signal)")
            for c in commands)))
    else:
        out.append((False, "board: no domain verbs derived — the mission's "
                    "objectives describe no action the parser could answer to; "
                    "the terminal offers its built-ins only"))

    # ── command_prefix ───────────────────────────────────────────────────────
    # The tool the command line belongs to — the `git` in `git commit`. Optional
    # when typing (`Run` only strips it when argv[0] matches), so its whole job
    # is to let the domain's command line read the way the domain writes it.
    #
    # Two tiers, evidence first: the word the brief actually puts in front of
    # one of the verbs above, then the name of the console entity with the words
    # that merely make it a console removed.
    verbs = {c["verb"] for c in commands}
    prefix = ""
    if verbs:
        low = " ".join(str(brief or "").split()).lower()
        for verb in sorted(verbs):
            for m in re.finditer(rf"\b([a-z][a-z0-9_-]{{1,15}})\s+{re.escape(verb)}\b",
                                 low):
                cand = m.group(1)
                if (cand in _PREFIX_SKIP or cand in verbs
                        or cand in _BUILTIN_VERBS or len(cand) < 2):
                    continue
                # A word that is part of the cast is describing the board, not
                # naming the tool that acts on it.
                if any(cand in _id_tokens(n) for n in pieces + [z["id"] for z in zones]):
                    continue
                prefix = cand
                break
            if prefix:
                break
        if not prefix:
            prefix = _prefix_from_console(str(options.get("console_group") or ""),
                                          verbs)
    if prefix:
        options["command_prefix"] = prefix
        out.append((True, f"board: commands read as `{prefix} <verb>` "
                          f"({prefix} is optional when typing)"))
    elif verbs:
        out.append((False, "board: no command prefix derived — nothing in the "
                    "brief puts a tool name in front of a verb and the console "
                    "is not named after one; commands are typed bare"))

    # ── requires ─────────────────────────────────────────────────────────────
    if not drops:
        return out
    stages: list[str] = []
    for obj in objectives:
        ref = norm_ref(getattr(obj, "target_ref", "") or "")
        if not ref:
            continue
        zid = zone_by_ref.get(ref) or ""
        if not zid and ref in piece_by_ref:
            zid = _stage_for(ref)
        # An ordering rule is about places a piece can be PUT, so a readout is
        # not a stage even though a command can reach it.
        if zid and zid in by_id and zid not in stages:
            stages.append(zid)

    if len(stages) < 2:
        if len(drops) < 2:
            why = f"there is only one place to reach ({drops[0]['id']})"
        elif not objectives:
            why = ("the project declares no mission objectives to order the "
                   "places by")
        else:
            why = (f"the mission objectives name {len(stages)} of the "
                   f"{len(drops)} places, too few to sequence")
        out.append((False, f"board: no ordering rule derived — {why}; a piece "
                    "may go anywhere it is accepted, in any order"))
        return out

    linked: list[str] = []
    for prev, cur in zip(stages, stages[1:]):
        # An ordering rule between two places with no piece in common could
        # never be satisfied by any move — it would not sequence the board, it
        # would close `cur` permanently.
        if not (takes[prev] & takes[cur]):
            out.append((False, f"board: {cur} does not require {prev} — no piece "
                                f"{prev} takes may enter {cur}, so the rule "
                                "could never be satisfied"))
            continue
        by_id[cur]["requires"] = prev
        linked.append(f"{cur} after {prev}")
    if linked:
        out.append((True, "board: ordering — " + "; ".join(linked)))
    return out
