# LLM Freeze Boundary (Step 3.5)

The Pipeline LLM owns Steps 2-3. Everything it authors beyond the bare schema
fill happens in **one declared phase, Step 3.5**, and is written **into the
schema**. After Step 3.5 the client is frozen: Steps 4-7 read what was
persisted and never the model.

```
 Step 2  Concept ─┐
 Step 3  Schema  ─┤  LLM (warm)
 Step 3.5 Enrich ─┘  → results persisted into the schema
 ════════════════════ freeze(self.llm) — src/llm_boundary.py ════════════
 Step 4  Resolver ┐
 Step 5  Fixer    │  deterministic; llm.chat() raises PostSchemaLLMError
 Step 6  Generator│
 Step 7  Grade    ┘
```

## What Step 3.5 authors, and where it lands

| Enrichment | Function | Persisted to | Read back by |
|---|---|---|---|
| Entity Sketchfab query | `unity_sketchfab_translator.translate_concept` | `entity.sketchfab_query` | Step 6 asset resolver |
| `type: custom` script bodies | `unity_micro_code.enrich_scripts` | `script.extra_*` fields | Step 6 generator |
| Composed-micro bodies | `unity_micro_code.enrich_subsystems` | `schema.microgens[].input` (via `sync_microgens_from_subsystems`) | Step 6 `merge_authored_code` |
| NPC dialog lines | `audio.dialog_resolver.extract_dialog_lines` | `meta.llm_enrichment.npc_dialog_lines["<variant>:<speaker>"]` | `_inject_npc_dialog_lines` → `options.meta._pre_lines` |
| Diegetic audio cues (stable3) | `audio.resolver.extract_cues` | `meta.llm_enrichment.audio_cues` | audio unit `options.meta._pre_cues` |
| Concept Sketchfab collection | `unity_sketchfab_collection.collect_models` | hash-checked collection + entity bindings in `meta.llm_enrichment`; frozen assets in the configured cache | Step 6 locked bindings + collection copy |

`meta.llm_enrichment` is **compiler-owned**: a value the schema-fill LLM wrote
under that key is discarded (with a WARN) before Step 3.5 writes its own
record (`format: game-o-matic/llm-enrichment/v1`, `stage: "3.5"`).

Because every result lives in the schema, the saved `<Project>.schema.json`
replays the same project through `unity-validate --compose` with no model.

## Enforcement

- `UnityPipeline._run_impl` wraps `self.llm` with `freeze()` right after
  Step 3.5; `run()` `thaw()`s it in `finally`, so the freeze is per run.
- `FrozenLLM.chat()` raises `PostSchemaLLMError` naming the Step 3.5 remedy.
  `unload` and `extract_json` pass through (neither samples the model).
- Step 6 calls `generator.write(llm=None)` and forces both
  `sketchfab.vision_select` and `sketchfab.auto_fetch` off. Per-entity
  selection can read only frozen/local catalog entries; the explicitly enabled
  Step-3.5 collection is the sole pipeline acquisition path. `semantic_select`
  remains the deterministic re-rank.
- `MicroComposer._resolve` sets each composed micro's `pipeline.llm = None`,
  so `default_fix` takes its documented no-LLM branch.
- Tests: `tests/test_llm_freeze_boundary.py` — every `chat` precedes the first
  Step 4 resolver call (compose on and off), a Step 5 pass that calls the model
  fails the run, the record is persisted and cannot be forged.

## Known trade-offs

- Step 3.5 enriches the **Step-3** schema. Content Step 5 adds later (a
  concept-rescued entity, an injected script, a micro kind that only appears
  after fixing) is not enriched and takes the same deterministic fallback an
  enrichment failure always took (model_hint / name query, baseline bodies,
  preset lines/cues). The log names what was enriched
  (`llm_enrichment.sketchfab_query_entities`).
- Composed-micro bodies are keyed by **kind** (the existing
  `schema.microgens` granularity): two units of one kind share one body set.
- GTA5 first uses `resolver.pick_model_deterministic` for an unresolved asset;
  ties and no matches remain unresolved and are persisted as a
  `meta.unresolved_models` abstention. It never asks a third model call or
  selects an unrelated first candidate.
- `config.yaml` defaults Sketchfab acquisition off. A user who explicitly
  enables the Step-3.5 collection accepts network and marketplace ranking
  *before* the freeze; the downloaded entries, their byte hashes, and the
  Step-6 entity bindings are saved with the schema. Replay verifies those
  bytes and refuses catalog drift rather than selecting a replacement.
