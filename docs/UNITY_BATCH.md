# unity-batch: live runs a few at a time, examined later

`python main.py unity-batch` runs one brief N times through the live pipeline
(Pipeline LLM included), one run at a time. Stop it whenever the machine is
needed; run the same command later and it continues with the next run. Every
run keeps its own folder, so results pile up to be read later with
`python main.py unity-batch-status`.

Implementation: `src/unity_batch.py`. Commands: `cli/unity_eval.py`.

## Commands

| Command | Does |
|---|---|
| `unity-batch neon --file prompts/neon_fugitive.txt --runs 20` | Create batch `neon` (target 20) and start running |
| `unity-batch neon` | Continue: run the next pending slot, and so on |
| `unity-batch neon --max 2` | Run at most 2 now, then stop |
| `unity-batch neon --hours 7` | Start no new run after 7 hours (a run in progress finishes) |
| `unity-batch neon --add 10` | Raise the target by 10 (`--runs N` sets it) |
| `unity-batch neon --add 5 --max 0` | Change the target without running anything |
| `unity-batch neon --stop` | From another terminal: the driver stops after its current run |
| Ctrl-C | Stop now. The current attempt is killed with its whole process tree, recorded, and its slot runs again |
| `unity-batch neon --discard 3 --reason "..."` | Discard finished run 3 (recorded); its slot runs again |
| `unity-batch-status` | Every batch: finished/target, verdict counts, driver running or idle |
| `unity-batch-status neon [--json]` | One batch, run by run |

Unknown options are forwarded to every run's `unity-create`
(`unity-batch neon --file b.txt -- --no-micro-code`). They are fixed when the
batch is created. `--file`, `--config` and an inline prompt are refused,
because the batch owns them.

## Where it lives

```
<unity_output_dir>/_batches/<name>/
  batch.json      manifest: brief, config, flags, target, runs, discarded
  brief.txt       the brief snapshot: every run reads this file
  events.log      human-readable timeline (starts, verdicts, stops)
  driver.lock     while a driver runs (pid)
  runs/01/        one folder per attempt (a re-run slot gets runs/01-a2)
    config.yaml   the batch config with unity_output_dir -> this folder
    create.log    unity-create stdout + stderr
    _run_logs/    unity-create-run-log/v2 (docs/UNITY_RUN_LOGS.md)
    _failed_runs/ unity-failed-run/v1 on failure (docs/FAILED_RUN_DIAGNOSTICS.md)
    <Project>/    the generated Unity project
    verify.log    e2e-verify --playtest output
    _verify_<Project>/  kept only when verify failed; Library/ and Temp/ pruned
```

A batch named with a path (`unity-batch I:/runs/neon`) lives there instead.
A batch made with `--config X` lives under X's `unity_output_dir`, so pass the
same `--config` to resume it, or pass the batch folder path.

## What counts as a sample

| Outcome | Sample? | Verdict |
|---|---|---|
| unity-create exit 0, e2e-verify PASS | yes | `PASS` |
| unity-create exit 0, verify off | yes | `UNVERIFIED` (the Editor compile gate never ran) |
| unity-create or verify failed | yes | `FAIL`, with the failed-run category and phase |
| no output for `--stall-minutes` (60) | yes | `STALLED`, the child tree killed |
| Ctrl-C, or the driver died mid-run | no | attempt `interrupted`; slot runs again |
| failed-run category `llm_transport` | no | attempt `env`; the batch stops (exit 3) |
| `--discard N --reason` | no longer | recorded under `discarded` with the reason |

A failure is data: it is never retried automatically. Only an attempt that
did not end, or one the LLM backend never answered, gives its slot back.
Discarding a finished run is a human decision, and it is recorded.

An interrupted `e2e-verify` keeps the unity-create result: on resume only the
verify runs again.

## Eval discipline

- Byte-identical input. The brief is snapshotted at creation (sha256 in
  `batch.json`). Each run's run log hash is checked against the snapshot
  (`brief_matches_snapshot`). Resuming with a different `--file` abstains: a
  new brief is a new batch.
- One declared derivation. The per-run `config.yaml` differs from the batch
  config only in `unity_pipeline.unity_output_dir`.
- Changes are visible, not blocked. Each run records the batch config's
  sha256 and the commit it ran (from its run log). `unity-batch-status` flags
  a batch whose runs span more than one config or commit.
- The code under test is the checkout the command runs from: `main.py` next
  to the imported `src/unity_batch.py`. A batch started from a worktree runs
  that worktree.

## Guards

- One driver per batch (`driver.lock`). A second driver, `--add`, `--runs` and
  `--discard` abstain while one is live, because they would race its manifest
  writes. `--stop` is the way to talk to a live driver.
- Before each run: free disk on the batch drive is at least `--min-free-gb`
  (10), and LM Studio answers `GET /v1/models` and lists the model. If either
  check fails, the batch stops with exit 2 and no slot is used.
- A record left `running` by a driver that is gone becomes an `interrupted`
  attempt on the next start. If that run's process is still alive, the start
  abstains and names the pid.
- Runs inherit the pipeline's own LLM caps (`src/llm_budgets.py`). The Step 2
  concept is capped at 12,000 tokens and the Step 3 fill at its measured
  budget, so a runaway ends itself instead of waiting for the stall guard.

## Exit codes

`0` stop condition reached (target, `--max`, `--hours`, `--stop`) · `1` abstain
(the message names the remedy) · `2` preflight stopped the batch · `3` LLM
backend failure mid-run · `130` Ctrl-C.

## Contract

Review provenance only, like the run logs. Nothing here enters a schema or is
read by Steps 2-7. The driver runs the ordinary `unity-create` and
`e2e-verify` CLIs as child processes and never imports or alters the compiler.
