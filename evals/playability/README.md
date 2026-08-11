# evals/playability

Playability regression cases. Each case replays a filled Unity schema through
the deterministic tail (Steps 4-6) and grades both the final schema and the
files the generator emitted, then compares the result to a floor locked in the
case file.

```powershell
python main.py unity-eval                        # every case, writes _runs/
python main.py unity-eval --case dropzone_arcade # one case
python main.py unity-eval --update-floors        # re-lock floors after a fix
python -m pytest -m playability -q               # the same cases, as a gate
```

## Why this exists next to tests/test_unity_playability.py

Those tests are engineer-authored schemas that exercise the **checks**. They
cannot catch a pipeline regression, because the engineer writes their input.
These cases are real schema-fill output, so what they exercise is the
**compiler**.

The failure that motivated the harness: `dropzone_arcade` graded **96/100
PLAYABLE** while its avatar spawned at the world origin, 2000 m below a descent
course the layout pass had explicitly placed it at the top of. Four ERROR-class
checks had never run at all, and the score was computed only over the ones that
did.

## Case format

```json
{
  "name": "dropzone_arcade",
  "schema": "tests/fixtures/dropzone_arcade_llm.schema.json",
  "input": "captured-llm",
  "provenance": "how this schema came to exist — required reading",
  "notes": "what shape this case covers",
  "floor": {
    "score": 70,
    "must_pass": ["avatar", "controllable", "..."],
    "ungraded_ok": ["brief_entities", "role_graph"]
  }
}
```

`input` is mandatory and is the eval-discipline declaration (CLAUDE.md):

| value | meaning |
| --- | --- |
| `captured-llm` | verbatim Step-3 output from a live run. No engineer edited it, so a pass says something about the real pipeline. |
| `engineer-authored` | a hand-written schema. Still a valid test of Steps 4-7; proves **nothing** about Steps 2-3. |

`floor.must_pass` is the locked set of checks that pass today — a check falling
out of it is a regression. `floor.ungraded_ok` names the ERROR-class checks this
input genuinely cannot supply evidence for; anything ungraded **outside** that
list fails the case, because a check that silently stops running takes its part
of the score with it.

## Briefs

A case carries a `brief` only when that brief is known to have produced that
schema. None of the current captures were saved with their prompt, so
`brief_entities` stays in `ungraded_ok` rather than being compared against a
brief that may not be its own — grading a build against someone else's brief
would manufacture the very failure the check exists to find. Brief coverage gets
graded for real by the live tier.

## Tiers

* **Tier R (replay)** — everything here. No LLM, no Unity, no network; runs in
  CI under `pytest -m playability`.
* **Tier L (live)** — `unity-eval --live`, the full pipeline from brief text
  through LM Studio. Deferred: the flag shares this grading path but no live
  cases ship yet, and it can never gate CI.

`_runs/` holds the last run's logs (gitignored): `<case>.json` is the
machine-readable failure log every finding of which names the pipeline stage
that produced it; `<case>.txt` is the human scorecard.
