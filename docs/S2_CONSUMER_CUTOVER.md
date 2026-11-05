# S2 consumer cutover — concrete implementation map

Baseline: `6919e7ebb0e7d45a046af6486f700120fbf685a5` (#285). This document is a
compiler engineering plan. It does not establish a completed S2 cutover, a model
evaluation, Unity compilation, or advancing gameplay execution.

## Order and authority

1. Capture the existing decisions in shadow fixtures, including false positives
   and gameplay deletion. Use actual selected sources, not a new member table.
2. Supply a compiler-backed, source-preserving candidate check. A missing reference
   remains visible; SDK binding without Unity references is not Unity acceptance.
3. Render fragment-free selected sources and actual project support; use that one
   contract for guidance and checking. Independently approve additions in order.
4. Cut all production consumers over together. Remove the copied tables and every
   earlier call path that can erase the evidence before the new check sees it.
5. Re-extract the final persisted Step-5 selection, validate consumers/glue, then
   run replay, mutation, generated C# and the real Editor matrix before acceptance.

No production consumer is switched by the initial shadow-check preparation.
There must be no partial green cutover with old project API authority underneath.

## Consumers and mutations to replace

| Boundary | Current implementation | Coherent cutover |
| --- | --- | --- |
| Step 3.5 own-class guidance | `unity_micro_code._contract_for` reads template regex unions | Selected source manifest; include signatures, accessors, statics and events |
| Step 3.5 sibling guidance | `_script_contract` uses `_COMPOSED_CLASS_API` plus signatures from unapproved sibling bodies | Selected baseline plus earlier independently approved additions |
| Micro payload application | `_apply` calls `_drop_unresolvable_types`; `_validate` can drop keys | Retain raw payload, report shape/semantic rejection, never erase collaborator types |
| Micro correction | `_would_not_bind` uses mutating template binder on a copy; exhausted attempts bypass checks | Same extracted contract on every attempt, including final; retain last invalid response |
| Script application | `_apply_script` lints/drops methods; `_bind_script` invents fields and neutralizes reads/calls | Separate explicit structural repair from semantic findings; preserve originals |
| Earlier Step 5 | `_programmatic_fix._clean` calls `_sanitize_singleton_calls`; copied GameManager/Context member sets | Remove project member filtering before final selected-source validation |
| Per-micro resolve | `MicroComposer._resolve` invokes `default_fix` and `harden`; `harden_against_template` calls `bind_authored_code` | Detach authored channels before structural normalization; validate/reinsert without semantic erasure; retain nested channels |
| HUD | `hud_setters` and `_VARIANT_SETTERS`; `default_fix` neutralizes unavailable names | Derive exported setter availability from selected writer output without recursive full baseline extraction; collision policy remains explicit |
| Plan and wiring | `MicroComposer.plan`, `_emit_wiring`, `_emit_context` branch on variant/abilities/locked and assume exports | Derive availability from actual selected exports; validate emitted bridge consumers |
| Writer source ownership | `compose` indexes units by kind and scans a shared kind directory | Per selected writer ownership; include primary/helper sources and reject collisions |
| Context helper ownership | `_emit_context` emits GameContext, optional StatModifier and NPC bootstrap/registry | Include every emitted helper in the same contract; do not invent IInputReader |
| Generator member checks | `_make_composed_call_stripper`, composed/service/field/parameter sanitizers, `_custom_script_api` | Roslyn receiver binding; remove project API tables, regex API unions and Play*/Set* exceptions |
| Generator collaborator erasers | invented constructor/find/namespace/stub filters, undeclared self/condition/foreach filters, `_reject_unbalanced_custom_bodies` | Preserve rejected source and structured diagnostics; no phantom stubs or empty success bodies |
| Framework checks | `_MB_METHOD_ARITY`, `_MONO_INHERITED`, broad inherited-member allowlists | Separate from project API; only real ancestry and actual assembly references establish semantics |
| Final selection | `UnityPipeline._run_impl` persists selection before Step 6 merge/sanitization | Merge original authored data, re-extract final selection, revalidate before generation; persist compatible evidence sidecars |
| Saved replay | `cli.unity_project.unity_validate` reads `selected_subsystems`, merges code, plans | Reuse saved `meta.compose_selection`; same contract/check path; never raw-prompt reselection |
| Secondary consumers | `cli/unity_eval.py`, `src/micro_stress.py` invoke subsystem sanitizers | Exercise same source-preserving check; update legacy deletion expectations honestly |

The three generator tables are `_COMPOSED_CLASS_API`, `_COMPOSED_METHOD_ARITY`
and `_COMPOSED_CLASS_EVENTS`. Earlier copies are `_GAME_MANAGER_API` and
`_GAME_CONTEXT_API`. Broad `_ANIMATOR_DYNAMIC_MEMBER_RE` acceptance must disappear
with the production cutover. Compatibility wrappers must not keep table authority.

## Regression matrix

- Selected health HUD lacks ShowLine; dialogue HUD provides it. Mutate a real
  SetHealth export and check guidance, wiring and consumer decisions without
  changing any member table; restore the template and exact contract identity.
- Private access; static/instance mismatch; overload/optional/params/generic arity;
  valid inherited project API; event subscribe/unsubscribe versus external invoke/read.
- Receiver parameter, field, local, getter and null conditional. Invalid assignment
  conversion must be located even when Roslyn points at the RHS.
- Dynamic HUD/animator names reflect the exact writer output. Plain Data helpers
  cannot gain MonoBehaviour members. No invented helper/interface stubs.
- Canonical GameManager valid calls survive; invalid calls remain in evidence.
- Approved earlier extension becomes available to later consumers only after refresh;
  failed or unresolved additions never extend the contract.
- Step-5 variant change invalidates stale Step-3.5 assumptions; replay uses saved
  selection and compares runtime source/GUID/fileID/attachment identities twice.
- Non-emitted extra/extension/nested channels retain original source and a loud finding.
- Missing SDK, malformed C#, duplicate logical paths/types, and missing references
  retain exact sources/diagnostics and cannot silently fall back to copied APIs.
- Existing nested FSM, driven animator, sprite source-only and audio/dialog flag
  restoration probes remain required. Inspection calls no asset backend.

## Execution requirements

Pinned SDK 9.0.312 with roll-forward disabled; real Unity 6000.1.4f1 references and
Editor compilation for selected variants and authored consumers. Binding reports
must retain *all* compiler errors, including unresolved references; filtering to a
convenient diagnostic allowlist would hide invalid conversions and unresolved types.
The SDK-only check is bounded evidence, never a replacement for the Editor gate.

Run ruff, mypy, full pytest, shared guidance check, atom support audit, required
component API tests, unity-eval replay, both Roslyn tool builds, generated C#,
mutation/replay and actual Unity compile. Record the exact candidate SHA and CI
links. Keep S2 and the live-evaluation harness separately reviewable.
