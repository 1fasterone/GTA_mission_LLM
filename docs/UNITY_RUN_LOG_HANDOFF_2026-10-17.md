# Unity Run Log Handoff

Snapshot: 2026-10-17. Read `AGENTS.md` before continuing.

## Outcome

The durable Unity run-log feature and both code-review repairs are implemented.
Focused tests, lint, a real failing CLI smoke, and a controlled hard-kill probe
pass. The branch is ready for scoped review and merge.

## Local state

- Worktree: `C:\tmp\gta-offline-run-logs`
- Branch: `codex/offline-run-failure-logs`
- Feature commit: `386d775 feat(cli): persist unity-create run review logs`
- Base/current `origin/main`: `395e0a7`
- Remote push and pull request state must be verified from GitHub at delivery.

The feature commit changes only:

- `main.py`
- `src/unity_run_log.py`
- `tests/test_unity_run_log.py`
- `docs/UNITY_RUN_LOGS.md`

## Implemented behavior

Every real `python main.py unity-create ...` invocation is intended to create:

```text
<unity_pipeline.unity_output_dir>/_run_logs/<unique-run>.log
<unity_pipeline.unity_output_dir>/_run_logs/<unique-run>.json
```

The JSON uses `unity-create-run-log/v1`, begins with `status: running`, and is
finalized as `succeeded` or `failed` on normal Python exit. It records the raw
prompt and hash, argv, config/model, `--no-llm`, `--compose`, last emitted step,
project path, playability summary, timing, exit code, and traceback.

These files are operational review evidence only. They are not schema fields,
Pipeline LLM input, resolver input, or Step 4-7 input.

## Review decision and repairs

See `docs/UNITY_RUN_LOG_CODE_REVIEW_2026-10-17.md` for full evidence.

1. **P1 repaired:** failed transcript/manifest I/O now trips a one-way circuit
   breaker, warns original stderr once, restores streams, and cannot replace a
   compiler exception or exit code.
2. **P2 repaired:** activation parses the actual Typer command slot after
   supported global flags; later positional values cannot activate logging.
3. **Scope clarified:** this feature records process-level Unity
   `unity-create` only. GTA and other commands remain out of scope.

## Verified commands

Run from the worktree using the shared repository virtual environment:

```powershell
& 'C:\Users\Owner\claude projects\GTA_mission_LLM\.venv\Scripts\python.exe' `
  -m pytest -q tests\test_unity_run_log.py `
  tests\test_cli_unity_project.py tests\test_unity_projectile_contract.py

& 'C:\Users\Owner\claude projects\GTA_mission_LLM\.venv\Scripts\python.exe' `
  -m ruff check main.py src\unity_run_log.py tests\test_unity_run_log.py

git diff --check origin/main...HEAD
```

Repair results on 2026-10-17:

- pytest: `21 passed in 1.36s`
- Ruff: passed
- diff check: passed
- real failing `unity-create`: exit code 2 with finalized failed artifacts
- controlled hard kill: `status: running` and partial transcript preserved

## Existing smoke evidence

A deliberate failed invocation left both artifacts:

- `C:\u\unity\_run_logs\unity-create-20261013T230329.266593Z-e3b0c44298fc-10436.json`
- `C:\u\unity\_run_logs\unity-create-20261013T230329.266593Z-e3b0c44298fc-10436.log`

Both files still existed when this handoff was written. Treat them as local
review data; they may contain the full prompt, command-line paths, and
traceback.

## Projectile acceptance context

The raw projectile run produced `C:\u\unity\GAMETYPE` but did not pass E2E:

- Step 7 reported `PLAYABILITY 91/100 - NOT PLAYABLE`.
- Unity compilation failed with duplicate global `ProjectilePool` (`CS0101`).
- The duplicate came from both `Assets/Scripts/ProjectilePool.cs` and
  `Assets/Micros/controller/Scripts/ProjectilePool.cs`.
- Enemy/target asset resolution was also visibly incorrect.
- There is no Unity Play Mode proof of a visible weapon firing a physical
  projectile.

The detailed local review is:

`C:\Users\Owner\.codex\visualizations\2026\10\13\01a1375f-0fba-73e2-ac1e-e32f59748485\projectile-run-review-2026-10-13.md`

Fix generated-project defects in compiler source/templates, regenerate from a
raw natural-language prompt, compile the whole Unity project, and obtain Play
Mode visual proof. Do not hand-edit generated Unity output and do not treat
per-file syntax checks as Unity compilation.

## Safe continuation sequence

1. Fetch the current base and integrate it without absorbing unrelated work.
2. Commit only explicit source/test/doc paths. Do not add generated Unity
   projects, `_run_logs`, caches, or user-owned prompts.
3. Push, open a scoped GitHub pull request, wait for required checks, and merge
   only when GitHub reports the branch mergeable.

Open a GitHub Pull Request immediately. No feature is considered complete
until it is PR'd.
