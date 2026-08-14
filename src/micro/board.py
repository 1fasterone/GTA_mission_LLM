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
readout you cannot drop into, and whether a console exists. The Pipeline LLM
authors the domain on top of that — the verbs, the per-piece labels, which zone
accepts what, and the ordering between zones. A board with no authored domain is
still a board you can play; that is the same rule `steps_from_mission` follows.
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


def board_options_for_entities(entities, *, title: str = "") -> dict | None:
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
