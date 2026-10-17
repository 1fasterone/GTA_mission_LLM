# Unity Run Log Handoff

Snapshot: 2026-10-17. Read `AGENTS.md` before continuing.

## Outcome

The durable Unity run-log feature is implemented locally and its normal paths
are tested, but code review found two unresolved defects. The branch is not
ready to push or merge.

## Local state

- Worktree: `C:\tmp\gta-offline-run-logs`
- Branch: `codex/offline-run-failure-logs`
- Feature commit: `386d775 feat(cli): persist unity-create run review logs`
- Base/current `origin/main`: `395e0a7`
- Remote push: **not performed**
- Pull request: **not opened**

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

## Review decision and required fixes

See `docs/UNITY_RUN_LOG_CODE_REVIEW_2026-10-17.md` for full evidence.

1. **P1: contain logging I/O failures.** A failed transcript write currently
   raises into the compiler, and finalization can mask an existing compiler
   exception. Add a best-effort disable path, guaranteed stream restoration,
   and regression tests for write/flush/finalization failures.
2. **P2: identify the actual CLI command.** Searching every argv token produces
   false activation when another command receives `unity-create` as data. Move
   activation to the command boundary or parse only the real command slot, with
   negative tests.
3. Clarify whether the product requirement covers only Unity `unity-create` or
   all offline Unity/GTA pipeline commands. Do not silently broaden scope.

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

Results on 2026-10-17:

- pytest: `14 passed in 2.41s`
- Ruff: passed
- diff check: passed

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

1. Preserve the current worktree and inspect `git status` before editing.
2. Fix the two review findings in `src/unity_run_log.py` and tests; keep the
   recorder outside the compiler/schema boundary.
3. Run the focused validation commands above.
4. Run a short failing `unity-create` smoke test and inspect both artifacts.
5. Run a controlled hard-kill test and verify `status: running` plus a partial
   transcript remain without corrupting other runs.
6. Commit only explicit source/test/doc paths. Do not add generated Unity
   projects, `_run_logs`, caches, or user-owned prompts.
7. Push only when authorized, then open a scoped GitHub pull request.

Open a GitHub Pull Request immediately. No feature is considered complete
until it is PR'd.
