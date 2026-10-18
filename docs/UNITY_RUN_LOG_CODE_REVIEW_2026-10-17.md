# Unity Run Log Code Review

Review date: 2026-10-17

Branch: `codex/offline-run-failure-logs`

Reviewed commit: `386d775`

Base: `origin/main` at `395e0a7`

Decision: **Approved after repairs**

Original reviewed commit: `386d775` — request changes.
Repair verification: the findings below are resolved on the branch tip.

## Scope

This review covers the operational run recorder added around real
`python main.py unity-create ...` invocations:

- `main.py`
- `src/unity_run_log.py`
- `tests/test_unity_run_log.py`
- `docs/UNITY_RUN_LOGS.md`

The recorder remains outside the schema and compiler. It does not add an LLM
call or change deterministic Steps 4-7.

## Findings

### P1 - Resolved: logging I/O failure could abort or replace the pipeline result

`_Tee.write()` and `_Tee.flush()` write to the review log without containing
`OSError`. The recorder's `__exit__()` also performs several unguarded log
writes and flushes before restoring stdout/stderr. A full disk, disconnected
volume, permission change, or failed file handle can therefore:

1. abort an otherwise healthy compiler run while it prints output; or
2. replace the original compiler exception with a secondary logging error
   during finalization.

This contradicts the documented contract that logging failure emits a loud
warning and allows the compiler to continue. It also makes the observation
layer capable of changing the run it is observing.

Reproduction from the reviewed commit:

```text
_Tee(..., BrokenLogThatRaisesOSError, ...).write("pipeline output")
=> OSError: disk full
```

Required repair:

- Make transcript writes best-effort behind a one-way failure circuit breaker.
- Restore the original streams even when recorder finalization fails.
- Never replace the pipeline's active exception or exit code with a logging
  exception.
- Emit at most one warning to the original stderr when logging is disabled.
- Add tests for a mid-run write failure, a flush/finalization failure, and a
  logging failure while a different pipeline exception is already active.

Resolution: transcript writes and flushes now pass through a one-way failure
circuit breaker. The recorder warns the original stderr at most once, restores
stdout/stderr before final manifest work, and contains transcript, manifest,
and close errors. Regression tests cover all three requested failure cases.

Relevant code: `src/unity_run_log.py:142-151` and
`src/unity_run_log.py:266-327`.

### P2 - Resolved: recorder activation searched every argument

`from_argv()` uses `full_argv.index("unity-create")`. Any unrelated command
whose positional value happens to equal `unity-create` activates the recorder.
For example, the following probe reports `active=True`:

```text
main.py unity-validate unity-create
```

That can create a misleading empty-prompt manifest and wrap a command the
documentation says is excluded.

Required repair:

- Identify the actual Typer command token after supported global options, or
  attach the recorder directly to the `unity-create` command boundary.
- Add negative tests where `unity-create` appears only as another command's
  value or positional argument.

Resolution: activation now selects the first Typer command token after the
supported global flags and refuses unknown leading options. Negative tests
cover `unity-create` as data for both Unity and GTA validation commands.

Relevant code: `src/unity_run_log.py:174-194`.

## Scope clarification

The implementation records real `unity-create` process invocations only. It
does not record GTA `create`, other pipeline commands, direct calls to
`main.app`, or hard termination before Python reaches the recorder. If “any
offline pipeline run” means both Unity and GTA backends, that expansion should
be specified and implemented separately rather than implied by this document.

The manifest intentionally stores the full raw prompt and complete argv. That
is useful review provenance, but reviewers must treat `_run_logs` as potentially
sensitive user data and agree on retention/redaction before sharing it.

## Verification performed

- Current `origin/main` fetched and confirmed as `395e0a7`; reviewed HEAD
  contains it.
- Focused tests: `14 passed`.
- Ruff: passed for the changed Python files.
- `git diff --check origin/main...HEAD`: passed.
- Failure probes reproduced both findings above.

The passing suite establishes the intended happy path, normal exception path,
and manifest fields. It does not cover the observer-failure cases described in
P1 or the false activation described in P2.

## Positive observations

- The manifest is initialized as `status: running`, preserving a useful marker
  after most abrupt terminations.
- Raw-input hashing, stage extraction, project path, playability summary, exit
  code, and traceback provide useful review evidence.
- Operational artifacts are kept outside schema and generation inputs.
- Help and ordinary non-target commands are covered by baseline tests.

## Repair verification

- Focused suite: `21 passed`.
- Ruff: passed for the changed Python files.
- `git diff --check`: passed.
- Real failing `unity-create` smoke: exit code 2, finalized `failed` manifest,
  traceback and transcript preserved.
- Controlled hard kill: process exit `-1`, manifest remained `running`, and the
  partial transcript remained reviewable.

## Merge recommendation

The two blocking findings are resolved. The branch is ready for scoped review
and merge after current-base integration and required remote checks pass.
