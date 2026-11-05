# Untouched-brief evaluation: preflight, not a live harness

Baseline: `6919e7ebb0e7d45a046af6486f700120fbf685a5`. The S2 production consumer
cutover is still pending. This separate preparation does not change the pipeline
or claim model, compile, or runtime fidelity.

## Confirmed execution locations

The user reports Qwen 3.5 9B loaded on their Windows PC. Unity 6000.1.4f1's Editor
folder is `C:\Program Files\Unity\Hub\Editor\6000.1.4f1\Editor`; its executable
is `Unity.exe`. Other Editor installations are under `D:\unity`. These paths
identify the PC installations. They are not mounted in this chat's Linux executor.

No authenticated PC execution bridge is exposed in this session. Linux localhost
is not the Windows machine's localhost. The absent Linux model endpoint therefore
does not establish that Qwen is unavailable on the PC. Do not request licenses,
passwords or API keys in chat, expose LM Studio publicly, or silently use another
provider.

The real compile gate remains pinned to Unity **6000.1.4f1**. Other installed
versions can supply additional separately labelled evidence; they cannot silently
replace the pinned gate.

## Read-only PC preflight

From the Windows repository checkout, run:

```powershell
python tools/untouched_brief_preflight.py --unity-root "C:\Program Files\Unity\Hub\Editor" --base-url http://localhost:1234/v1 --expected-model qwen3.5-9b --output output/untouched_preflight/pc-01.json
```

Use the actual configured API base URL/model ID if different. A loaded model is
not proof its HTTP server is enabled. The script performs only GET `/models`,
Editor executable file inventory, SDK inventory, git revision inspection and
SHA-256 over the six existing original brief files. It launches neither model
inference nor Unity, reads no secrets, and refuses to overwrite a prior report.
Use another attempt filename for each later run. `--offline` explicitly skips
network discovery.

The script reports model IDs honestly; revision/quantization/file hashes remain
unverified. Editor file presence proves neither licensing nor compilation.
Candidate input hashes do not establish original authorship, held-out status, or
a completed requirement oracle. The script is not `unity-eval --live`.

## Candidate provenance inventory

The six handoff candidates were inspected as inputs. This table does not freeze
an accepted held-out cohort. Preserve the full existing bytes; do not feed this
inventory or an expected checklist to the Pipeline LLM.

| Original file | Coverage reason | Known history / limitation |
| --- | --- | --- |
| `prompts/turbo_toys.txt` | Vehicle physics, drift, boost, laps and rivals | Previously used in E2E Concept/image comparison; regression pool, not held out. Includes explicit production directives. |
| `prompts/oddities_baggage_claim_3d.txt` | Linked scanning/carrying/sorting/stamping, rising-water deadline, named clutter and exclusions | Prior tuning history unknown. Mixed prose and production headings; original authorship not established by repository presence. |
| `prompts/mossward_2d_topdown.txt` | 2D twin-stick control, no gravity/jump, dash/parry, inventory/gates, scenes | Prior tuning history unknown. Starts with genre/style/premise directives; keep those original bytes. |
| `prompts/chromabreak_arena_fps.txt` | Shooting/reload/melee, waves, exact entities, dialogue exclusions, UI and persistence | Prior tuning history unknown. Explicit schema/material/entity directives make this a production-style input; physical versus hitscan delivery is ambiguous. |
| `prompts/tower_control.txt` | Pointer-only desk, legal clearances, conflict resolution, handoff progression and no scenery/avatar/vehicle | Prior tuning history unknown; promising natural-language input. No held-out claim without provenance. |
| `prompts/dropzone.txt` | Long late requirements, altitude thresholds, formation, canopy/wingsuit, career progression and multiple scenes | Prior fixtures explicitly reference this brief; regression pool. Structured production blueprint with ambiguous AI cardinality (3 entity divers versus four in narrative). |

Sources: `prompts/E2E_PROMPT_INDEX.md`, `docs/E2E_REFERENCE_IMAGE_EXPERIMENT.md`,
`tests/fixtures/README.md`, and full candidate files. No captured schema is claimed
to have been generated from a newly paired plausible brief.

Coverage gaps remain: established fresh held-out provenance; an authentic brief
that unambiguously distinguishes physical projectiles from hitscan; and six
accepted natural-language cases distinct from structured production inputs.

## Remaining work before any live conclusion

1. Complete S2 and its independent review/required gates.
2. Confirm the actual PC-hosted provider/model identity and execution access; retain
   model revision/quantization/settings/context/token limits without exposing secrets.
3. Select/freeze the accepted cohort and original attachments. Write the independent
   per-requirement oracle with exact spans before inspecting new model output.
4. Implement opt-in live orchestration and full stage capture. Existing unity-eval
   replay and the pre-IR mechanics evaluator do not run the model or Unity.
5. Run one case to validate capture, then the bounded cohort. Retain every attempt.
6. Compile the exact generated projects in the real Editor and run advancing
   mechanic-specific probes. Existing smoke tests alone do not establish intent.
7. Produce every requirement trace, deterministic replay and bounded comparison.

The preflight's explicit `pending` and `unverified` fields must remain in reports
until the corresponding work actually executes.
