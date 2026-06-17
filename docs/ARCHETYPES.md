# GTA Mission Archetypes — "imagination alignment"

## The problem this solves

Before this layer, **every** GTA prompt — race, heist, ocean, flight — collapsed to
the same generic shape: `go_to → kill_all → go_to → wait`. The schema had the
right building blocks (`race_lap`, `checkpoint`, `timer` step types, six named
circuits in `lookups/tracks.json`, a `race_opponent` vehicle role) but nothing
forced the LLM to *assemble* them into a real sub-game. A "race" had no start
line, no laps, no finish, no rivals.

## How it works

`src/archetypes.py` adds a **gameplay-structure** layer that is orthogonal to
themes (`src/themes.py`, which only shape *setting* — vocabulary and tone).

1. **Detect** the archetype from the prompt — `detect_archetype()` scores
   keywords (prompt weighted 3×, concept 1×). Six archetypes:
   `race`, `heist`, `dive` (ocean), `fly` (aerial), `assault`, `chase`.
2. **Bias the LLM** — each archetype contributes a `concept_hint` (Step 2, so the
   story is genre-coherent) and a `structure_hint` (Step 3, so the steps array is
   genre-shaped). Wired via `{archetype_block}` in `CONCEPT_USER` and
   `{structure_block}` in `SCHEMA_FILL_USER`.
3. **Guarantee structure** — `apply_scaffold()` is an anti-repetition guard: if
   the LLM *still* produces a degenerate steps array with none of the archetype's
   signature steps, the pipeline injects the canonical skeleton (e.g. race →
   `get_in_vehicle` + `race_lap` on a location-matched real track + rival racers).
   Non-degenerate output is trusted and left untouched.

The downstream pipeline (resolver, `_check_consistency`, compiler) is unchanged —
`_check_consistency` already fills in `race_opponent` vehicles and lap defaults.

## RAG model dedup

`resolver.resolve_schema` now runs a within-mission dedup pass: distinct
descriptions that collapsed to the same model are re-resolved to a
related-but-unused model (`security guard` → a real security ped instead of a
second cop). It is conservative — only an exact/substring-related alternative is
accepted, never a loose fuzzy drift, and identical descriptions intentionally
keep the same model.

## Verifying

Offline (deterministic): `pytest tests/test_archetypes.py`

End-to-end (needs LM Studio on :1234):
```powershell
python main.py generate "make a race mod" --seed 42
python main.py generate "pull off a jewelry store heist" --seed 7
```
Then open `output/<Name>.schema.json` and check the `steps` array tells a real
genre story. A good race schema looks like:

```
get_in_vehicle  "Board your buggy at the start line"
race_lap        track=sandy_shores.circuit  lap_count=3  require_vehicle=true
checkpoint      track=sandy_shores.circuit
+ a race_opponent vehicle, a mission_passed audio cue
```

The console prints `OK Archetype: Race`; a `FIX ... scaffold` line means the
LLM's output was degenerate and the deterministic skeleton was injected.
