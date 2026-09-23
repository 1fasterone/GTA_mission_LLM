# Visual Catalog Freshness Handoff

Status: the Slice-2 visual reasoning boundary is working. The next fix is a
catalog refresh/index repair, not a weaker noun validator and not another
Concept prompt rewrite.

## Finding

The image inventory produced literal, high-signal asset nouns:

- `speedboat`
- `checkpoint archway`
- `ramp`

These are pure visible asset nouns. They are not roles, mechanics, inferred
game concepts, or vague descriptions. A healthy Sketchfab-aligned catalog
should have many speedboat models, racing checkpoint/archway props, and
multiple stunt, boat, or track ramp candidates.

The validator and deterministic resolver correctly refused the current
catalog result. The noun is not the error. The catalog/index state is stale or
misaligned with the current visual noun set.

## Reproduced evidence

The guided v8 run used the imported board and passed the following stages:

1. Slice 2: Qwen received the board image plus the validated visible noun
   labels and wrote `rallyboat.v8.inventory.json` and
   `rallyboat.v8.Concept.txt`.
2. Slice 3: deterministic reconciliation closed the image IR with zero LLM
   calls in `rallyboat.v8.closed-image-ir.json`.
3. Slice 4: deterministic asset fulfilment wrote
   `rallyboat.v8.asset-fulfilment.json` with status `rejected`.

Slice 4 fulfilled seven assets and rejected three because the catalog's best
deterministic scores tied:

| visual noun | rejected candidates | reason |
| --- | --- | --- |
| `speedboat` | `delivery.fbx`, `firetruck.fbx`, `sedan.fbx` | ambiguous catalog match at the best deterministic score |
| `checkpoint archway` | `banner.fbx`, `banner.fbx`, `structure-platform.fbx` | ambiguous catalog match at the best deterministic score |
| `ramp` | `ramp-end-a.fbx`, `ramp-end-a.fbx`, `ramp-long-b.fbx` | ambiguous catalog match at the best deterministic score |

The v8 fulfilment manifest recorded these catalog inputs:

- `lookups/unity/asset_catalog.json`
  - SHA-256: `22c69a6a6b3a00fcf7e9e4dcaef61b6efcc6eee5faa3dba237f746e3fd6fc462`
- `lookups/unity/sketchfab_catalog.json`
  - SHA-256: `1218dce059768612e92d347c7ca891d221f68265786301be5cb5bb252a6313ba`

The mismatched candidates are diagnostic: `speedboat` resolving to
`delivery`, `firetruck`, and `sedan` means the available index does not have a
strong current speedboat noun/category mapping. The checkpoint and ramp ties
show the same freshness/alignment problem at the prop and track boundaries.

## Rules for the next fix

Do not change the following to make this run pass:

- Do not replace `speedboat` with a vague query such as `boat`.
- Do not inject `player`, `rival`, `race`, `finish`, or other game semantics
  into the Sketchfab query.
- Do not make the resolver pick the first tied catalog entry.
- Do not weaken the visual inventory validator or Slice-3 reconciliation.

The correct repair is to refresh or rebuild the catalog/index from the current
asset roots and Sketchfab-aligned snapshot. The literal visual nouns should
remain unchanged. If refreshed candidates are still visually ambiguous, the
separate pre-freeze visual candidate-selection/refusal gate must choose or
refuse; keyword order must not silently decide.

## Handoff checklist

The next chat should:

1. Inspect how `lookups/unity/asset_catalog.json` and
   `lookups/unity/sketchfab_catalog.json` were generated, including their
   source roots, timestamps, and keyword/category population.
2. Rebuild or refresh the catalog so `speedboat`, checkpoint/archway terms,
   and ramp variants have current, category-compatible candidates.
3. Add a deterministic catalog freshness/preflight check that fails loudly
   when a high-signal visual noun has no category-compatible candidates or
   only cross-domain matches such as `delivery`, `firetruck`, or `sedan`.
4. Preserve catalog hashes and generation provenance in the fulfilment
   manifest.
5. Re-run Slice 4 against the existing v8 closed image IR. Only after asset
   fulfilment is `fulfilled` should the run continue to Slice 5 and Unity.
6. If the refreshed catalog produces multiple genuinely plausible candidates,
   invoke visual candidate selection/refusal and record the decision; do not
   call that a noun or Concept failure.

The implementation boundary is already recorded in PR #222:

- Slice 2 uses the image plus validated noun labels to write the Concept.
- Slice 3 remains deterministic and LLM-free.
- Slice 4 must treat catalog freshness and candidate ambiguity as an asset
  data-quality problem.

The v8 generated artifacts are intentionally untracked local evidence. They
remain in the shared workspace for reproduction but are not committed as
production fixtures.
