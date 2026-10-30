# RFC-0001: Authority model for the Unity pipeline

Status: **proposed target**. Current implementation has known violations;
this document does not itself enforce the boundaries. Scope: Unity generation.
The existing GTA5 backend is not migrated by this RFC.

## Goal

For each gameplay decision, retain the evidence, the actor that proposed it,
the resolution, and the consumers that used it. One canonical decision must
reach a schema, composition plan, asset bindings, generated project and
validation report without downstream components silently deciding it again.
No validation stage changes gameplay. A valid input schema and diagnostic
report must remain available when acceptance fails.

## Current baseline and transition

On main at `7e41326e027a70565386dc9d164e86c7b387cd56`, Step 3 fills a schema;
the classifier can subsequently override `game_type` and `locomotion`, compose
and the projectile bridge can inspect raw brief text, and multiple fixer and
reconciliation passes mutate the schema. This is existing behavior, not
permission for new hidden ownership. PR #279 proposes persisted compose
choices and conflict reporting, and PR #280 proposes removing the 4,000-char
Step-3 brief cap; both were drafts when this RFC was written.

Until the roadmap replaces the old paths, every affected PR must identify any
remaining mutation and its owner. Do not label this model enforced because an
RFC exists. Migrate one boundary at a time with frozen fixtures, explicit
provenance and compatibility tests. Preserve saved-schema replay and identify
legacy behavior when its original evidence is unavailable.

## Authority boundaries

| Area | Authorized responsibility | Must not do |
|---|---|---|
| User brief and intent evidence | Preserve the unmodified source; record claims from the brief, Pipeline LLM, classifier and rules, including source, confidence, rationale and explicit exclusions. | Silently drop a claim, turn an exclusion into an inclusion, or present inferred evidence as user-authored fact. |
| Intent resolver | Resolve claims once per decision under documented precedence; retain conflicts, chosen value and reasons, or abstain on insufficient evidence. | Generate code, consult an LLM after the freeze, or conceal a conflict. |
| Schema generation and enrichment | Represent resolved intent using the existing schema contract; Step 3.5 persists any LLM-authored content before the freeze. | Invent new gameplay intent after resolution or add unapproved root schema fields. |
| Composition | Consume canonical schema and resolved decisions; persist controller/micro choices and declared dependencies. | Reinterpret raw prompt text or override a resolved decision. |
| Projectile mechanic | Validate the canonical projectile contract and report incompatible or missing settings. | Parse raw brief text to add weapons/mechanics or mutate the schema during validation. |
| Asset binding | Bind a declared entity/role to a selected compatible asset; report provenance and unresolved selections. | Change gameplay roles or create mechanics as a side effect of matching an asset. |
| Generation | Render and wire the selected schema, composition and bindings deterministically. | Reclassify the game, introduce intent, or make a post-freeze LLM call. |
| Validation | Read snapshots/artifacts and produce findings, warnings or errors. | Mutate the schema, add gameplay or silently repair/override intent. |
| Testing and evaluation | Separate engineer-authored fixtures, captured LLM outputs and held-out natural-language briefs; record model, commit and run provenance. | Present a hand-authored fixture as an LLM evaluation or overwrite the only record of earlier runs. |

A deterministic repair is not automatically a validator. During migration,
repairs that change structure or gameplay must be named as transformations,
assigned an owner, and attributed in the ledger. Move structural consistency
repairs into a declared transformation stage; move gameplay decisions to intent
resolution. Validators receive a read-only snapshot or prove input equality
before and after execution.

## Target pipeline and freeze

`brief → intent extraction → claim aggregation → intent resolution → canonical
schema → validation → composition → asset binding → generation → compile →
evaluation`.

The Pipeline LLM is limited to Steps 2–3 and declared Step 3.5 enrichment. Any
LLM-generated claim must be captured before the existing freeze; Steps 4–7
remain deterministic and LLM-free. The resolution stage consumes persisted
claims and uses deterministic rules; it does not call the model or write code.
The target order must not be mistaken for a claim that today's pipeline already
implements it.

The original brief is retained in full. A model server's context window can
still reject a request; the compiler must not silently shorten the brief. The
independently requested removal of the character cap is in draft PR #280.
If an explicit future input transformation is authorized, record it in the run
report and evaluation provenance, including any omitted span.

## Evidence and snapshots

The roadmap's claim fields are `field`, `value`, `source`, `confidence` and
`rationale`. The implementation must also provide stable claim identity and
source span or another traceable source reference, explicit polarity for
exclusions, and a resolution record. Do not fabricate confidence when the
source cannot supply it; make its absence explicit. Record every mutation's
stage, field/path, before/after value and claim or declared transformation
responsible. Preserve conflicting and unresolved claims.

Capture snapshots before Step 3, after Step 3, after reconciliation, after
composition, and immediately before generation. The pre-Step-3 snapshot is an
intent/input snapshot, not a fabricated schema. Diffs must be deterministic and
redact secrets; they must not be mistaken for a complete ledger until all
mutation paths are instrumented. Evidence is an observation layer, not a second
unreviewed source of authority.

## Enforcement and exit criteria

1. Branch protection requires a non-author approval and all CI checks for
   everyone, with no direct-push bypass; see `.github/branch-protection.md`.
2. .NET checks build real semantic failure controls and check generated syntax.
   A separate licensed Unity job compiles a generated project and requires an
   executed passing smoke test. Syntax-only parsing is not semantic compilation.
3. Claim registration and snapshots cover the named classifier, composition and
   projectile paths before those paths stop rewriting intent. Measure coverage;
   do not claim all changes are traced while uninstrumented passes remain.
4. Explicit exclusions, including `no bikes`, survive extraction, resolution,
   schema and composition. Conflicts are visible and resolved in one place.
5. Validators leave schema bytes unchanged. Generation reads schema/intent and
   frozen bindings only. Schema and metadata migrations are versioned.
6. Stage extraction, consolidation, registry, evaluation and cleanup follow the
   supplied roadmap in order, with regression fixtures and rollback per slice.
   Deletions are archived first and only files actually identified as stale are
   removed.

A green structural test is not evidence of Unity compilation or model fidelity.
Each workstream records its own required evidence and any infrastructure still
missing. The overall roadmap remains incomplete until its live branch policy,
Unity compile gates and end-to-end claim trace are actually verified.
