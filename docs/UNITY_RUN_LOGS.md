# Unity Create Run Logs

Every real `python main.py unity-create ...` invocation writes review evidence
under:

```text
<unity_pipeline.unity_output_dir>/_run_logs/
```

Two files share a unique run stem:

- `.log` — the plain-text console transcript, including the last completed
  pipeline step and any Python traceback.
- `.json` — a machine-readable `unity-create-run-log/v1` manifest.

The manifest is created with `status: running` before the pipeline begins. A
hard-killed process therefore leaves an explicit incomplete-run data point.
Normal completion changes the status to `succeeded`; a non-zero exit or
exception changes it to `failed` and records the exception type, message, and
traceback.

The manifest also records:

- the exact raw inline/file prompt and its SHA-256;
- command arguments, working directory, config path, and selected model;
- whether `--compose` and `--no-llm` were used;
- the last observed `STEP` line;
- the generated project path and playability summary when emitted;
- start/end timestamps, duration, and exit code.

These are operational provenance artifacts. They are never placed in the
schema, never supplied to the Pipeline LLM, and never consumed by deterministic
Steps 4-7. Passing a log to a reviewer cannot alter a replay.

`unity-create --help` and unrelated CLI commands do not create run logs. If the
configured log directory cannot be written, the CLI emits a loud
`RUN LOGGING ERROR` and continues rather than silently claiming that evidence
was saved.

Transcript and manifest I/O are best-effort observation only. A mid-run write,
flush, close, or manifest-update failure disables further recording through a
one-way circuit breaker, emits at most one warning to the original stderr, and
does not change the compiler's exception or exit code. The original stdout and
stderr are restored before final manifest work.

This contract covers process-level `unity-create` invocations only. It does not
record GTA `create`, other Unity commands, or direct in-process calls to the
Typer app. Manifests contain the full raw prompt and argv, so `_run_logs` may
contain sensitive user data and should be reviewed before sharing.
