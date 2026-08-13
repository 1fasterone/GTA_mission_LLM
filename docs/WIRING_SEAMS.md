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
| 8 | trigger / interactable / spawner → objectives | an authored objective of a matching type exists | **the mission can be completed** |
| 9 | objectives → hud | the HUD declares `SetObjective` | the current objective + tally is on screen |
| 10 | inventory → hud | the HUD declares `SetCarried` | the carried count is on screen |

Seams 1–7 wire *systems to systems*. Seams 8–10 are the **mission loop**: 8 makes
a game finishable, 9 and 10 make its progress visible.

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
