# Wiring Seams

A **seam** is one named, typed connection between two composed micros, emitted
into `GameWiring.cs` by `--compose`. Seams are what turn a folder of independent
subsystems into a game: without them the micros all run, and none of them talk.

- **Built in:** `src/unity_compose.py` (`MicroComposer.emit`, the `SEAM N` blocks)
- **Snippets:** `src/atoms/wiring.py` (definitions) + `src/atoms/backends/unity.py`
  (the emitted C#)
- **Emitted to:** `Assets/Micros/wiring/Scripts/GameWiring.cs`

Every seam is deterministic. A seam fires only when both endpoints were composed
**and** the target method provably exists on the emitted class.

---

## The seams

| # | Source → Target | Fires when | What the player notices |
|---|---|---|---|
| 1 | health → hud | the HUD class declares `SetHealth` | the HP bar moves when you take damage |
| 2 | health → statemachine | both composed | dying drives the `Dead` state |
| 3 | health → audio | a real cue matched the hurt/death/heal role | you hear the hit |
| 4 | statemachine → animator | both composed | state changes drive `CrossFade` |
| 4b | input → controller | the controller exposes `SetInputReader` | the avatar responds to input |
| 5 | input → statemachine | both composed | move/jump reach the FSM |
| 6 | controller → fsm/animator | grounded bridge (runs in `Update`) | jump/land read correctly |
| 7 | trigger → saveload | both composed | zones checkpoint-save |
| 8a | **board → objectives** | an objective's `target_ref` names a piece group, zone or verb the board carries | **the move the player was told to make completes the objective** |
| 8 | trigger / interactable / spawner → objectives | an authored objective of a matching type exists, still unbound | **the mission can be completed** |
| 9 | objectives → hud | the HUD declares `SetObjective` | the current objective + tally is on screen |
| 10 | inventory → hud | the HUD declares `SetCarried` | the carried count is on screen |

Seams 1–7 wire *systems to systems*. Seams 8–10 are the **mission loop**: 8 makes
a game finishable, 9 and 10 make its progress visible.

## SEAM 8a — why the board binds first, and per objective

A trigger volume and an enemy spawner cannot tell which objective an event was
about, so each binds exactly ONE: two `eliminate` objectives sharing one spawner
would both advance on every kill, which is worse than not wiring the second.

A board can tell. Every event it raises carries a **key** — the piece group, the
zone, or the verb it happened to — so each objective gets its own filtered
binding and a six-objective board does not have to settle for one live step:

```csharp
_onBoardObjective0 = key => { if (key == "FileIcons") _objectives.ReportProgress("obj_1", 1); };
_gameboard.OnPiecePlaced += _onBoardObjective0;
```

| objective | board event | amount |
|---|---|---|
| `collect` / `interact` naming a piece group | `OnPiecePlaced` (per piece) | 1 |
| naming a zone | `OnZoneFilled` (once) | the objective's whole `count` |
| naming a command verb | `OnCommand` (once) | the whole `count` |
| otherwise naming a piece group | `OnGroupComplete` (once) | the whole `count` |

The amount differs because the events do. An event that fires once, bound at one
count per event, leaves a `count: 5` objective permanently at 1/5 — silently,
which is the failure mode this seam exists to end.

It runs before the generic sources so a `collect` objective about real pieces
binds to a piece being placed rather than to any click on any interactable. That
is the difference between the rules micro's instruction being **true** and it
merely being **displayed**.

## The wiring report — how Step 7 grades this

Whether a mission can be completed is a **compose-time** fact, but the
playability evaluator reads the *schema*. So the composer records what it
decided on `MicroComposer.wiring_report`, and the pipeline stamps it onto
`schema.meta["wiring"]` after `emit()`:

```python
{"seams": ["SEAM 1", "SEAM 8", "SEAM 9", ...],
 "objectives_bound":            ["ColdwaterCircuitObjectives.beat_02"],
 "objectives_unbound":          ["ColdwaterCircuitObjectives.beat_04 (reach)"],
 "objectives_optional_unbound": ["ColdwaterCircuitObjectives.beat_07 (escort)"],
 "objectives_self_driven":      ["ColdwaterCircuitObjectives.beat_05 (survive)"],
 "objectives_total":    5,
 "objectives_required": 4,
 "hud_objective":       True}
```

`unity_playability._check_mission_loop` grades that ground truth rather than
re-deriving a prediction — which is what let it once score an unwinnable build
above a winnable one. On a build without `--compose` it reports *ungraded*
rather than passing: absence of evidence is never evidence of playability.

### The unbound list is three lists

`Objectives.cs.j2` sets `MissionComplete` only once **every** non-optional
objective is done, so completion is AND-shaped and the three cases below are not
interchangeable. Reporting them as one list is what let the check pass a mission
with one live objective and five dead ones (`ok = bool(bound)` — its own version
of the defect it exists to catch), and fail an all-`survive` mission whose
correct number of seams is zero.

| list | blocks completion? | mission_loop |
|---|---|---|
| `objectives_self_driven` | no — the tracker's own clock advances it | counts as advanceable |
| `objectives_optional_unbound` | no — `CheckMissionComplete` skips optional | `mission_optional` WARN |
| `objectives_unbound` | **yes** | ERROR, however many others bound |

---

## Gating rule: prove the method exists

A seam calls a method on a generated class. If that method is not there, the
project fails to compile (CS1061) — and the failure is in generated glue, which
is the worst place to debug.

So a seam must never be gated on a proxy for the method (a variant name, a
feature flag). It is gated on the method itself:

```python
# src/unity_compose.py — ComposedUnit.setters is the HUD class's real surface,
# read back AFTER default_fix has run its collision lint.
if health and hud and "SetHealth" in hud.setters:
```

`hud.setters` comes from `src.micro.hud.hud_setters(variant, widgets)`: the
variant baseline's own methods plus one per widget that survived linting. This
replaced a `hud.variant == "health_bar"` test, which was wrong in both
directions — it skipped `full_hud_bundle` (which does declare `SetHealth`) and
could not see a widget-provided setter on any other variant.

---

## Abstain loudly

A seam that cannot be wired is **named in the log**, never silently dropped:

```
WARN  objectives: no event source for MissionObjectives.escape_dock (reach)
      — these cannot advance at runtime. Reachable sources this build: interactable, spawner
```

The rule from `CLAUDE.md` applies exactly here: a wrong silent fallback is harder
to diagnose than a loud refusal with an explicit remedy. SEAM 8 binds one source
to one objective and names every objective it could not bind; SEAM 9 names the
extra objective trackers whose progress is live but not displayed.

---

## Adding a seam

1. **Define the atom** in `src/atoms/wiring.py` — an `AtomDef` with the params
   the snippet binds. Keep params to variable names and ids; an atom must not
   take a code fragment.
2. **Emit the C#** in `src/atoms/backends/unity.py` and register it in
   `_SNIPPETS`.
3. **Wire it** in `MicroComposer.emit`: `_pick()` both endpoints, gate on a
   provable method, append to `awake_seams` (or `update_seams` for per-frame),
   push any stored delegate onto `stored_delegates`, and append the matching
   `-=` to `destroy_cleanups`.
4. **Register the pair** in `_NAMED_SEAM_PAIRS` so the generic `schema.wiring`
   pass does not emit a duplicate comment for it.
5. **Test it** — assert the `SEAM N` label, the `+=`, the call, and the `-=`.
   See `tests/test_seam8_mission_loop.py` and `tests/test_hud_content_model.py`.

Every subscription needs its unsubscription. A stored delegate field exists so
`OnDestroy` can remove *the same instance* it added — a lambda re-created at
unsubscribe time removes nothing.
