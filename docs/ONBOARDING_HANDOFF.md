# Unity Projectiles and Asset Resolution — Onboarding Handoff

Snapshot: 2026-10-13. This is a state handoff, not a replacement for
`AGENTS.md`. Read `AGENTS.md` before editing or running the pipeline.

## Compiler contract

This repository is a compiler, not a chat chain. The Pipeline LLM may author
only the Concept, schema, and declared Step-3.5 enrichment. Resolution,
fixing, generation, layout, and playability are deterministic. Do not
hand-author a schema and describe its output as a raw-prompt E2E result.

## Current branch and PR state

- `origin/main`: `b8def842cee5ec748d7d0042b6cafcdb78a93cd3` (2026-10-13).
- Projectile roadmap: `docs/PROJECTILE_CONTROLLER_ROADMAP.md`.
- Slice 1 foundation is merged. Slice 2 was initially merged in PR #257, but
  the live arena review correctly found that it did not guarantee a visible
  weapon or physical bullets.
- PR #260, `fix(unity): wire physical ProjectileController delivery`, is open
  as a **draft**. It adds a deterministic physical-projectile contract for
  3D projectile prompts, preserves explicit hitscan as a separate path, adds
  a player weapon/muzzle, and verifies pool/controller code generation. It is
  not ready to merge until a fresh raw-prompt run and Unity Play Mode show a
  fired projectile.
- The Sketchfab shared-cache resolver correction is isolated on
  `codex/resolver-sketchfab-identity`. It must be reviewed and PR'd
  independently of projectile Slice 2.

## Why Skate-boy appeared

The resolver already had a Sketchfab-first Tier S, but fresh worktrees could
not see the user’s frozen Sketchfab catalog/cache. The catalog records roots
relative to the checkout where it was frozen (`Models/Sketchfab`), so a fresh
worktree skipped those candidates and fell back to generic local defaults.
That fallback includes the unwanted skate-boy model.

The resolver correction makes the shared, frozen catalog and its cache an
explicit configuration pair. It does not download, re-index, copy, or delete
the cache. A read-only real-cache probe resolved `ArenaRifle` to the licensed
Sketchfab `sci-fi-assault-rifle` FBX rather than a default character model.

After that resolver PR merges, configure the user-local `config.yaml` (do not
commit these machine-specific paths):

```yaml
sketchfab:
  enabled: true
  auto_fetch: false
  cache_dir: 'C:/Users/Owner/claude projects/GTA_mission_LLM/Models/Sketchfab'
  frozen_catalog_path: 'C:/Users/Owner/claude projects/GTA_mission_LLM/lookups/unity/sketchfab_catalog.json'
```

`frozen_catalog_path` is intentionally strict: if it is supplied without a
matching `cache_dir`, the resolver raises a clear configuration error instead
of silently selecting unrelated local models.

## E2E status and blockers

- The approved arena prompt is user-owned and is not in this checkout. At this
  snapshot it is
  `C:/Users/Owner/.codex/worktrees/f7e5/GTA_mission_LLM/prompts/projectile_test_arena.txt`.
  Do not commit it unless the user explicitly requests a tracked fixture.
- A previous deterministic replay proved the Slice-2 generated source contains
  a physical projectile pool, projectile prefab, muzzle resolution, and a
  real weapon path. It is not a fresh LLM E2E.
- A raw `unity-create --compose` attempt stopped at Step 2 because LM Studio
  reported that `qwen3.5-9b` was not loaded. No schema was produced.
- Unity Play Mode firing evidence is still required. It was paused because
  generated-project artifacts consumed disk space; disposable generated output
  directories were removed. Do not delete source worktrees or the shared
  Sketchfab cache as “artifacts.”

## Safe next sequence

1. Review/merge only green scoped PRs. Do not merge PR #260 while its live
   firing acceptance remains unproven.
2. Merge the resolver PR, then add the two local shared-cache settings above.
3. Load the configured LM Studio model, run the approved prompt raw through
   the pipeline, and capture Unity Play Mode evidence of a visible weapon and
   fired projectile. Keep hitscan tests separate.
4. If Slice 2 passes live acceptance, mark PR #260 ready and merge it.
5. Fetch updated `origin/main`, create a **new** Slice 3 branch from it, and
   implement only Slice 3. Do not build Slice 3 on stale projectile branches.

Each roadmap slice is deliberately a small leaf PR: merge its green result to
`main`, update from `origin/main`, then start the next leaf. This prevents an
unvalidated Slice 2 from becoming an inherited assumption in Slice 3.

## Validation expectations

- Run focused unit tests and `git diff --check` before committing.
- A model-resolution claim needs a real catalog/cache path and selected FBX
  evidence, not only a source-code assertion.
- A playable-projectile claim needs both the raw pipeline and Unity Play Mode
  evidence; compile success alone is not E2E.
- Stage explicit files only. Keep generated Unity output, caches, and
  user-owned work out of commits unless expressly requested.

Open a GitHub Pull Request immediately. No feature is considered complete
until it is PR'd.
