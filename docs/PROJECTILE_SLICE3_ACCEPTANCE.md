# Projectile Slice 3 Acceptance

## Verdict

**PASS — 2026-10-23.** The deterministic compiler regenerates distinct bullet
and rock projectile presentations, retains a uniformly scaled physics root,
and restores presentation state through two pool reuse cycles per profile.

## Retained Unity evidence

- Editor: Unity `6000.0.66f2`
- Generated project: `I:\UnityAcceptance\ProjectileSlice3_20261023_044215`
- Compile log:
  `I:\UnityAcceptance\ProjectileSlice3_20261023_044215-compile.log`
- NUnit Play Mode report:
  `I:\UnityAcceptance\ProjectileSlice3_20261023_044215-playmode.xml`
- C# compiler errors: `0`
- Play Mode: `1 passed`, `0 failed`

The report contains these runtime proof records:

```text
SLICE3_REUSE_PASS profile=bullet cycle=1 transforms=reset materials=reset effects=reset
SLICE3_REUSE_PASS profile=bullet cycle=2 transforms=reset materials=reset effects=reset
SLICE3_REUSE_PASS profile=rock cycle=1 transforms=reset materials=reset effects=reset
SLICE3_REUSE_PASS profile=rock cycle=2 transforms=reset materials=reset effects=reset
SLICE3_VISUAL_PASS profiles=bullet,rock reuse_cycles=2 physics=root-sphere presentation=distinct-primitives
```

Reproduce from canonical repository sources:

```powershell
& .\tools\run_projectile_slice3_acceptance.ps1
```

The runner creates a new URP project, regenerates the input and controller
micros, compiles the generated project, runs the Play Mode probe, and rejects
missing proof records. The generated Unity project is evidence, not an editable
source of truth.

## Compiler and regression gates

| Gate | Result |
|---|---:|
| Focused Slice 3 / retained projectile physics tests | 20 passed |
| Frozen Step 3.5, digest inventory, CI contract | 13 passed |
| Sketchfab tests with inherited token removed | 26 passed |
| Scoped CI Ruff plus Slice 3 test/tool files | passed |
| mypy (`src main.py cli rag`) | passed, 266 source files |
| Full pytest suite | 4,964 passed, 43 skipped, 5 unrelated failures |

The five full-suite failures are outside Slice 3 and were not altered:

- Three `tests/test_outputs_smoke.py` cases reference pre-existing generated
  output directories whose `Assets` directories are absent.
- Two `tests/test_sketchfab_client.py` cases inherited the machine-level
  `SKETCHFAB_API_TOKEN`; all 26 tests pass when that environment variable is
  removed, matching their no-token premise.

Repository-wide `ruff check .` also reports 50 pre-existing findings in
unrelated legacy, test, and tool files. The configured source/CLI CI scope and
all Slice 3 files pass Ruff.

Unity project creation emitted one non-blocking cloud-link warning because no
Unity organization is configured. Local creation, generation, compilation, and
Play Mode acceptance all completed successfully.
