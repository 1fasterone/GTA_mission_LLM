# Persisted controller and micro selection

`unity-create --compose` now resolves its final controller and micro choices at
the end of Step 5 and stores them in `schema.meta.compose_selection`. Step 6 and
`unity-validate --compose` read this record rather than selecting again from the
raw brief. No root schema fields or LLM calls were added.

The version-1 record contains:

- `version` and `stage` (`step5`).
- `subsystems`: the ordered list of kind, variant, target, name and complete
  options. Order matters for deterministic scene file IDs.
- `disagreements`: classifier/schema conflicts (original value, classifier
  value, matched keywords, score, chosen source and reason), plus differences
  between schema-derived default selections and brief-derived selections.
- `atmosphere` and `climbing_contract`: generator gates that previously read
  raw brief text independently of composition. The climbing scene contract now
  follows the selected climbing controller.

Brief-derived options needed by lighting, audio and board helpers travel inside
this record. Authored micro code continues to travel in `schema.microgens` and is
merged onto fresh copies of the saved subsystems. Existing geometry passes still
refresh cover points and projectile presentation after model-aware layout.
Codegen selection is excluded from spatial-sidecar identity because it is
persisted after spatial preparation and adds no spatial constraints.

A schema without the record uses schema-only micro selection and prints a warning
that its original selection cannot be recovered. This fallback never mines
`meta.user_prompt` or `meta.concept` for controller/micro choices. An invalid or
unsupported record fails validation instead of silently selecting another game.
To make different choices, rerun selection before generation; editing raw prompt
metadata on a saved schema does not reselect micros.

This change preserves the existing pre-generation classifier and keyword
precedence policies. Recording a conflict does not establish that its chosen
side is correct. In particular, existing substring/negation limitations remain
in the selection pass; this is persistence and provenance, not a claim-fidelity
or model-quality improvement. Schema-derived defaults are labelled as defaults,
not represented as explicit LLM decisions.

Step 3.5 still makes a preliminary selection for enrichment. Step 5 selects the
final plan after repairs and entity/mission rescue; Step 6 reads that plan only.
The brief is not reformatted or replaced before the Pipeline LLM sees it. Tests
use engineer-authored schemas and mocked fills, not live held-out evaluations.

Regression coverage compares emitted micro trees byte-for-byte (including audio
and metadata), compares full generator output after changing raw brief/concept
metadata, exercises the live Step-6 boundary and CLI replay, and checks conflict
provenance, JSON round trips, malformed records, legacy fallback and spatial
identity stability. These tests establish compiler behavior, not Unity runtime
playability or LLM brief fidelity.
