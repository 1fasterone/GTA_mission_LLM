# No-cut: turning the model's intent into code instead of deleting it

Two projects were taken from "compiles" to "a human can play it" by hand —
**TowerControl** (an approach-control console) and **CodeVault** (a git teaching
sim). This is the diff between what the pipeline emitted and what playing them
actually required, and the plan that folds each manual step back into the
generators.

There are two findings and one doctrine.

---

## Finding 1 — the compiler composes mechanisms, never a game

Every micro is a mechanism: a controller that moves, a trigger that fires, an
objectives tracker that counts. Nothing in the compiler is responsible for the
**rules** (what a legal move is, what completes an objective, what losing looks
like), the **progression** (what to do first, next, last), or the
**instructions** (what the player is even supposed to do).

Those three are the game. No step owns them, so no build has ever had them.

## Finding 2 — the writer is lied to, then punished for believing it

`src/unity_micro_code.py:470`, in the prompt that asks the model for C#:

> *"Only reference members you declare yourself... Do not assume a property such
> as IsSprinting, **CurrentSpeed** or **IsGrounded** exists on this class."*

`templates/unity/micro/controller/Controller.cs.j2:332-338`:

```csharp
public float CurrentSpeed => _velocity.magnitude;
public bool  IsGrounded  => _grounded;
```

Two of the three members named as examples of things that do not exist **are
members of the class**. Worse, the same prompt forbids the model from asking for
systems it needs — *"do NOT invent a helper class such as OxygenManager"* — which
suppresses the single most useful signal it can give us.

Meanwhile `template_members()`, `template_method_arities()` and
`template_types()` compute the exact API surface, with signatures and arities.
They are **not imported by `unity_micro_code.py` at all**. They exist only in the
pass that judges the output.

> The compiler computes the contract, withholds it from the writer, then deletes
> the writer's work for violating it.

The repair passes are 32 deletion-shaped against 10 rewrite-shaped. One live
TowerControl run deleted:

| deleted | what it was |
|---|---|
| `SetConflictAlert`, `GetRadarSweepColor` | the conflict-alert mechanic and the radar sweep — the game's core pressure system |
| `ReadState`, `WriteState` | the entire save/load implementation |
| `JsonDocument`, `System`, `jsonDoc` | the serialisation those two used |
| `IsInteractable()` | an interaction gate |
| `conflictActive`, `radarSweepColor` | the state the alert ran on |

Every live bug fix to date, including three of mine, went into the **judge** —
making the deletion more precise. None went into the **writer**. That is the
circle: the cuts get cleaner, the games stay empty, and the build reports
success either way.

---

## The doctrine

> **An unbindable symbol is not an error. It is a requirement.**

The model is doing what the user is doing: expressing intent that outruns its
ability to implement. A user writes a sentence describing a game they cannot
build; the model writes C# describing a mechanic it cannot enforce. The
compiler's job is to **realise** intent, not to validate it.

Three tiers. **`delete` is not one of them.**

| tier | when | action |
|---|---|---|
| **1. Bind** | the thing exists in another shape | rewrite the call to the real thing |
| **2. Build** | it does not exist, but is well-formed intent | synthesise the field, method or type that makes it real |
| **3. Escalate** | genuinely undecidable | stop the build and ask — never resolve quietly |

Every compile error hit across both projects was **tier 1**, with a mechanical
and unambiguous answer:

| authored | intent | binds to |
|---|---|---|
| `_clearancePad?.OnClick()` | press the pad | `_clearancePad.onClick.Invoke()` |
| `OnObjectiveComplete()` | complete the objective | `OnObjectiveComplete(<id in scope>)` — the compiler knows the ids |
| `FindGameObjectWithTag("FolderContainer")` | find the folders | tag `Folder`, from entity `FolderContainers` |
| `cam.worldToScreenPoint(v)` | project a point | `cam.WorldToScreenPoint(v)` |
| `_radarSweep?.localRotation = q` | rotate if present | `if (_radarSweep != null) _radarSweep.localRotation = q` |

All of them were deleted instead.

Tier 2 is where the suppressed signal goes. `_commitTriggered` is a bool the
model wants — declare it. `SetConflictAlert(...)` is a method it wants —
generate it, signature from the call site, semantics from the concept.
`OxygenManager` is the model saying *this game needs an oxygen system* — that is
a micro request, and the right answer is to synthesise one.

The risk in tier 2 is real: a synthesised body can be plausible nonsense. The
asymmetry settles it — **a wrong stub is visible and fixable; a deleted mechanic
is invisible and unfixable.**

---

## The plan

### Stage 6.4a — show the writer the contract

Put the real surface into the authoring prompt: the template's fields, methods
and **events with signatures and arities**, the project's **defined tags**, the
usings already in scope, and the other micros reachable through `GameContext`.
The functions that compute all of this already exist and are already correct —
this is an import and a prompt section.

Delete the two false claims from the prompt while doing it: stop naming real
members as non-existent, and stop forbidding invented types — route those to
tier 2 instead.

### Stage 6.4b — close the loop: author → compile → re-author with the error

`enrich_subsystems` already takes `attempts`, but it retries a **failed
response**, not **code that did not bind**. Feed the diagnostic back verbatim
and ask again:

> `OnObjectiveComplete` takes 1 argument (string); you called it with 0.

This is the change that makes no-cut affordable. Without it, tier 1 is a growing
pile of hand-written special cases (which is exactly what today's repair passes
are). With it, the model does its own binding when it is finally told what is
wrong — including for cases nobody anticipated.

### Stage 6.4c — the binder replaces the neutraliser

`bind_authored_code` stops commenting things out. It resolves by the three
tiers, and the only terminal state is **escalate**, which fails the build with
the named mechanic rather than shipping a green build with a hole in it.

Every AUDIT removal that does survive gets stamped on
`schema.meta["authored_code_removed"]`, and a Step 7 check
`authored_gameplay_survived` (ERROR) fails when a removal touched a method
reachable from an objective. *Absence of evidence is never evidence of
playability* — the rule the non-embodied work already applies to abstained
checks.

### Stage 6.5 — the `rules` micro  ✅ *shipped*

A micro that is **not per-subsystem**. It receives the whole concept and the
whole schema and emits one `<Project>Game.cs`:

- a domain model derived from `schema.entities`
- a `Step[]` progression compiled from `schema.missions[].objectives` — each
  carrying title, what it teaches, what it asks, and a **completion predicate**
- a **briefing** built from the concept and the step list
- a **hint bar** bound to the current step's ask
- a completion report

`MissionObjective` already carries `id`, `type`, `description`, `target_ref`, so
no schema change: each `type` compiles to a real predicate (`collect` → every
entity tagged `target_ref` reached its state; `reach` → the named interaction
fired; `survive` → a timer; `defeat` → the population hit zero).

This is the piece written by hand twice. `TowerControlGame.cs` and
`CodeVaultGame.cs` differ in domain and rules and are **structurally identical**
in briefing, step plan, hint bar and completion — which is the evidence it is
templatable rather than bespoke.

### Stage 6.6 — the `board` micro  ✅ *shipped*

`rules` shipped the progression and the instructions and explicitly not the
first item on the list above: the domain model and the legal-move logic. So a
build said *"drag every file icon into a folder container"* and nothing in it
made dragging a file icon into a folder container a thing that works. The
instruction was true prose about a mechanic that did not exist.

`src/micro/board.py` is that mechanic, and it is deliberately not per-subsystem
either — it is the verbs of a game with no avatar:

- a **domain model** — every piece, every zone, and where each piece IS
- a **legal move** — `TryPlace`, which refuses BY NAME (already there / a
  readout cannot be dropped into / this zone does not take that / it has to
  come from somewhere else first)
- **drag and drop** over the plates `EntitySpawner` already built, adopted
  rather than rebuilt, because the layout pass chose where they go
- a **command parser** — quote-aware, with `move`/`status`/`zones`/`help`
  built in and the domain's own verbs declared on top — reaching the SAME
  `TryPlace`, so the two surfaces cannot disagree about what is legal

The deterministic derivation reads the UI vocabulary the layout and asset passes
already share (`ui_element_of`): an icon or card is a piece, a panel is a place,
a **log is a place you cannot drop into** — a history is produced by a command,
not filled in by hand — and a terminal-shaped window is the command line. So a
board is playable with no model at all.

The **legal move** is derived on top of that, from the mission — deterministically,
because in the compose path nothing else could: `MicroComposer._resolve` builds a
micro's schema straight from `Subsystem.options` with no LLM, and
`enrich_subsystems` may only merge C# fragments, so a field with no producer
ships empty. `board_domain_for_missions` fills every one of them, from two
sources, each for what it actually knows — the **mission** carries the rules,
the **brief** carries the names:

| field | derived from | abstains when |
|---|---|---|
| `accepts` | name affinity — a piece and a place that share a word belong together (`RedCards` → `RedSlots`) | any place matches no piece, any piece has nowhere to go, or the result refuses nothing |
| `commands` | the opening imperative of each objective's description (`"Stage every modified file"` → `stage`), with the description itself as its `help` | the objective is about nothing on the board, or the verb collides with a built-in or with an earlier objective's |
| `command_prefix` | the word the brief puts in front of one of those verbs (`"git commit"` → `git`), else the console entity's name minus what makes it a console (`GitConsole` → `git`) | the candidate is grammar rather than a name, is itself a verb (`Run` would eat the bare verb and answer with usage), or is part of the cast |
| `items` | the concrete names a brief lists in a clause that names the group — quoted strings, dotted filenames, alphanumeric codes | the clause is not about that group, or lists fewer than two: one mention in passing is not a roster |
| `requires` | the order the mission's objectives first reach each place; an objective naming a PIECE reaches the place that alone accepts it | fewer than two places are reached, or the two places share no piece, which would close the second forever rather than sequence it |

All-or-nothing and loud, because one place left taking anything while its
neighbours are restricted reads to the player as a bug in the game rather than a
rule of it. The board is also **titled from the same mission the briefing is**,
so the two player-facing surfaces cannot disagree about what is being played.

`items` earns more than captions: `ResolvePieces` matches on the label, so a
named piece is a piece the player can type at — `stage README.md` means nothing
until the roster is read. Clause splitting therefore does **not** break on a
newline (briefs arrive hard-wrapped, and treating the soft wrap as a boundary
cut a nine-name roster after the second) nor on the dot inside a filename.

### The `OnCommand` arm  ✅ *bridged*

`RunCommand` raised `OnCommand` from the day it was written — as a signal for a
verb that moves nothing, and after a move for one that does — and no composed
build could ever fire it, because `commands` had no producer. The seam could not
reach it either: `_board_objective_binding` keyed everything off `target_ref`,
which names a piece or a place, never a verb.

The case that needs it is the **readout**. `TryPlace` refuses a DRAG into one, so
an objective about a log ("Record what you staged") cannot be satisfied by
dragging at all; it was binding to `OnZoneFilled`, reachable only through the
built-in `move` verb that nothing tells the player about. `meta.objective_commands`
now records the verb derived FOR such an objective, and SEAM 8a consults it
first:

- readout target → `OnCommand`, filtered on the verb, whole count at once
- piece target → unchanged `OnPiecePlaced`, one per placement (a verb fires once
  and would complete a count of nine on the first command)
- drop-target place → unchanged `OnZoneFilled`
- nothing on the board → **no verb at all**, so SEAM 8a — which runs before every
  other source — cannot claim a step away from the trigger volume that really
  raises it

`harden` reports a verb that collides with one of `Run`'s built-ins: the switch
answers those before it reads the declared commands, so such a verb is not
overridden, it is unreachable, and an objective bound to it could never advance.

Its events (`OnPiecePlaced`, `OnGroupComplete`, `OnZoneFilled`, `OnCommand`) are
the honest event source a UI-first game never had — SEAM 8 could only offer a
trigger volume or an interactable, both of which wait for a body to arrive.

### The `mission_loop` bug  ✅ *fixed*

Found while wiring the above. `_check_mission_loop` graded `ok = bool(bound)` —
its own version of the defect it was written to catch. Completion is AND-shaped
(`CheckMissionComplete` returns early on the first incomplete non-optional
objective), so a mission with one live objective and five dead ones can never
finish, and it passed with a warning. The mirror error failed an all-`survive`
mission, where the tracker's own clock advances everything and the correct
number of seams is zero.

The composer now reports self-driven, optional-unbound and required-unbound
apart, and the check grades the third: **one unbound required objective is an
unwinnable build, however many others bound.**

### The metric — concept fidelity

None of the above is measurable without this, and nothing in the pipeline
currently notices if every adjective in a prompt evaporates.

> *"paper airplanes attacking six-legged hippos in bowler hats driving tanks,
> hurling custard pies"*

The win is exactly that, and today it is lost at every hand-off: **six-legged**
is dropped because the asset resolver globs "hippo" to the nearest FBX;
**bowler hat** is an attachment nobody models; the **custard pie** becomes a
generic pickup with no splat. The same loss showed up from the other side this
week — a *flight strip* resolved to a **metal paper clip**.

So: extract the concrete nouns and modifiers from the brief and assert each one
survives into the output — as an entity, a model spec, a material, a VFX or a
rule. "Six legs" either appears in the model spec or it does not. That turns
*word-for-word concept representation* from an aspiration into a number that can
regress.

### `unity-playtest`

Build headless, enter play mode, drive the declared progression through its own
predicates, assert every step becomes reachable and the last one completes.

This is what was done by hand for both games. It is the only check that would
have caught the CodeVault dead-end (after the first commit every file was
committed, so the "commit on the branch" step had nothing to stage and the
player was stuck — no compile error, no failed check). Until it exists,
"playable" is an opinion.

---

## Sequence

1. **6.4a + 6.4b** — the writer-side fixes. Small, and they change what every
   subsequent run produces, so evidence stops coming from a rigged setup.
2. **6.4c** — the binder, once the loop is closed and cuts are rare.
3. **6.5** — the rules micro.
4. **Concept fidelity** + **playtest** — so both of the above can be measured
   instead of asserted.
