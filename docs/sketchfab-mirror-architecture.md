# Sketchfab Local Mirror — Architecture Decision Record

Status: **proposed**
Scope: **metadata index only.** Archives stay lazy — downloaded on demand
through the existing `src/sketchfab_freeze.py` path. See §11 for why.
Companion to: `docs/model-pipeline-architecture.md` (frozen-artifact boundary),
`docs/FREE_ASSETS.md`.

---

## 1. Corrections to the proposed plan

The plan as sketched (`scrape("https://sketchfab.com/tags")`, then
`?page={n}` per tag) does not work against Sketchfab as it exists today.
Four specific problems:

**1.1 — HTML scraping is unnecessary and strictly worse.**
Sketchfab exposes an official REST API that already returns everything the
plan wants as JSON. Verified against the live OpenAPI spec at
`https://docs.sketchfab.com/data-api/v3/swagger.json`:

| Need | Endpoint |
|---|---|
| tag list | `GET /v3/tags` |
| category list | `GET /v3/categories` (`sort_by=slug\|-slug`) |
| license list | `GET /v3/licenses` |
| model search | `GET /v3/search?type=models` |
| download URLs | `GET /v3/models/{uid}/download` |

`src/sketchfab_client.py` already calls three of these. Scraping the HTML
would give strictly less metadata (no `faceCount`, no `archives`, no
license uid) for strictly more work.

**1.2 — `page={n}` does not exist.** The v3 API is **cursor-paginated**.
The spec's own preamble: *"When a request gives many results, results are
paginated using cursors. Each response will contain these fields you can use
to make subsequent requests: `next`: full URL containing the next results;
`previous`: full URL…"*. A crawler must follow `next` opaquely; it cannot
compute page N.

**1.3 — tags are the wrong partition key.** Tags on Sketchfab are
user-authored free text. They are:

- *unbounded in count* — not a fixed vocabulary you can enumerate cheaply,
- *heavily overlapping* — a single model carries 5–20 tags, so a tag-major
  sweep re-fetches the same model many times over,
- *not total* — a model with zero tags is invisible to a tag-major sweep,
  and untagged uploads are common.

A crawl keyed on tags is therefore neither exhaustive nor efficient. §4
replaces it with a partition key that is provably both.

**1.4 — `downloadable` is a search filter, not a stored flag to discover.**
`downloadable=true` is passed on every request (already done in
`SketchfabClient.search()`), so the "downloadable" field the plan wants to
store is constant-true across the entire mirror and carries no information.
Store the *archive facts* instead (`archives.gltf.size`,
`archives.gltf.faceCount`, `textureMaxResolution`) — those are what actually
decide whether a model is usable.

---

## 2. What already exists — do not rebuild

| Component | File | State |
|---|---|---|
| v3 client (search / download-urls / archive fetch) | `src/sketchfab_client.py` | working, WAF-tolerant via Playwright |
| license uid → slug table | `src/sketchfab_client.py::_LICENSE_UID_TO_SLUG` | 10 slugs, verified 2026-07 |
| license gate | `filter_licensed()` + `config.yaml sketchfab.accepted_licenses` | `['cc0','by','by-sa','by-nd']` |
| fetch → convert → freeze | `src/sketchfab_freeze.py` | working |
| frozen catalog writer (dedupe by uid) | `src/unity_asset_catalog.py::write_sketchfab_entry` | read-merge-write, keyed on `sketchfab_uid` |
| frozen catalog | `lookups/unity/sketchfab_catalog.json` | 317 entries, all with `sketchfab_uid` |
| style coherence | `src/sketchfab_style.py::SketchfabStyleSession` | tag-Jaccard + same-author anchor |
| resolver Tier S | `src/unity_entity_asset_resolver.py` | ranks Kenney + Sketchfab with one score fn |
| embedder | `rag/embedder.py` — `BAAI/bge-small-en-v1.5` (fastembed ONNX) | working |
| vector store | ChromaDB, `rag/index/chroma.sqlite3`, collections `rag_gta5` / `rag_unity` / `rag_shared` | working |

The mirror is an **addition below Tier S**, not a replacement for any of it.

---

## 3. Two catalogs, one join key

Keep these separate. They answer different questions and have different
determinism requirements.

```
lookups/unity/sketchfab_catalog.json      lookups/unity/sketchfab_mirror.db
= FROZEN catalog                          = MIRROR index
  what we have on disk, converted           what exists on Sketchfab
  post-Blender facts:                       pre-download facts:
    relpath, bbox_meters, scale,              faceCount, archive size,
    scale_corrected, format                   textureMaxResolution, tags
  317 entries                               10^5–10^6 rows
  byte-deterministic JSON (git-ignored,     SQLite (git-ignored)
  regenerable via `sketchfab-fetch`)
                    \                      /
                     \____ sketchfab_uid _/
```

Merging them would break the frozen catalog's byte-idempotency guarantee
(`write_sketchfab_entry`'s sort_keys/no-ambient-timestamp discipline) and put
a million rows through a whole-file read-merge-write on every single insert.
Join on `sketchfab_uid` at query time instead.

---

## 4. The partition problem — the core of the design

### 4.1 The constraint

Search backends cap cursor depth. Sketchfab's cap is **not documented** and
must be measured (§10, Probe P1). Assume a cap exists — call it `D` results
per query. Any partition of the corpus that contains more than `D` models
cannot be fully drained.

So the crawl is not "iterate pages until done". It is: **tile the corpus with
disjoint, exhaustive filter predicates, each small enough to drain, and
recursively bisect any tile that isn't.**

### 4.2 Choosing the tiling axis

Requirements for a good partition key: every model has exactly one value
(total), no model has two (disjoint), and the value space is orderable so it
can be bisected.

| Candidate | Total? | Disjoint? | Bisectable? | Verdict |
|---|---|---|---|---|
| `tags` | no (untagged models) | no (multi-valued) | no | **reject** |
| `categories` | no (uncategorised models) | no (multi-valued) | no | reject as primary |
| `user` | yes | yes | no (unbounded set) | reject |
| `license` | yes | yes | n/a (9 values) | **accept — outer axis** |
| `min/max_face_count` | yes | yes (half-open bands) | yes | **accept — inner axis** |

`license` and face-count are the only two axes on `/v3/search?type=models`
that are simultaneously total and disjoint. Together they form a rectangular
tiling that is exhaustive by construction.

### 4.3 The tiling

```
downloadable=true                       (global, constant)
  × license ∈ {cc0, by, by-sa, by-nd}   (4 tiles — from accepted_licenses)
    × face-count band [lo, hi)          (log-spaced, adaptively bisected)
      sort_by=-publishedAt              (stable order for resume)
```

Log-spaced seed bands (face counts are power-law distributed, so linear
bands would put 90% of models in the first one):

```
0  500  1k  2k  5k  10k  20k  50k  100k  200k  500k  1M  ∞
```

12 bands × 4 licenses = **48 seed partitions**. Each drains independently.

### 4.4 Adaptive bisection

```python
def sweep(license_slug, lo, hi, depth=0):
    """Drain one tile; split it if it hits the cursor cap."""
    n, exhausted = drain(license=license_slug,
                         min_face_count=lo, max_face_count=hi)
    if exhausted:
        return                       # tile fully consumed
    if depth >= MAX_DEPTH or hi - lo <= 1:
        log.warning("unsplittable saturated tile %s [%d,%d) — %d rows, "
                    "corpus coverage is incomplete here", license_slug, lo, hi, n)
        return                       # honest gap, recorded not hidden
    mid = lo + (hi - lo) // 2        # integer bisection, deterministic
    sweep(license_slug, lo,  mid, depth + 1)
    sweep(license_slug, mid, hi,  depth + 1)
```

`exhausted` is true when the response's `next` is null **before** the drain
has emitted `D` results. If the drain stops because it hit `D`, the tile is
saturated and gets split. The `MAX_DEPTH` branch is the one place the mirror
can be incomplete — it is logged loudly and recorded in `crawl_partition`
rather than silently swallowed.

**Caveat, stated plainly:** face-count bands rely on `min_face_count` /
`max_face_count` boundary semantics (inclusive vs exclusive) that the spec
does not define. Probe P2 (§10) must pin this down, or adjacent bands will
either double-count or drop a boundary row. Double-counting is harmless
(dedupe on uid, §5); dropping is not.

### 4.5 What tags become instead

Tags stop being the crawl driver and become **stored output** — the
`model_tag` table (§5). Every model's full tag list arrives free in the
search response. The result is the tag index the original plan wanted,
built as a by-product of a crawl that is actually exhaustive.

`GET /v3/tags` is still worth calling once, as a vocabulary source for
`asset_vocabulary()` in `unity_asset_catalog.py` — which today exposes only 23
categories and 40 tags to the schema-fill prompt — but not as a crawl seed.

---

## 5. Storage schema

SQLite, not JSON. At 317 entries a 640 KB JSON blob is fine; at 10^5–10^6
rows, read-merge-write of the whole file per entry is not.

```sql
-- lookups/unity/sketchfab_mirror.db          (add to .gitignore)
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS model (
    uid                 TEXT PRIMARY KEY,      -- dedupe key; == sketchfab_uid
    name                TEXT NOT NULL,
    description         TEXT,
    viewer_url          TEXT,
    -- license
    license_uid         TEXT,
    license_slug        TEXT,                  -- via _LICENSE_UID_TO_SLUG; NULL = unknown
    license_label       TEXT,
    -- geometry / cost
    face_count          INTEGER,
    vertex_count        INTEGER,
    archive_gltf_size   INTEGER,               -- bytes; archives.gltf.size
    archive_face_count  INTEGER,
    archive_texture_max_res INTEGER,
    -- capability flags
    is_animated         INTEGER DEFAULT 0,
    is_rigged           INTEGER DEFAULT 0,
    pbr_type            TEXT,
    -- provenance
    user_uid            TEXT,
    user_username       TEXT,                  -- the handle search(user=) needs
    user_display_name   TEXT,
    thumbnail_url       TEXT,
    published_at        TEXT,                  -- ISO8601
    view_count          INTEGER,
    like_count          INTEGER,
    -- mirror bookkeeping
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    raw                 TEXT                   -- full JSON, for schema drift recovery
);

CREATE TABLE IF NOT EXISTS model_tag (
    uid  TEXT NOT NULL REFERENCES model(uid) ON DELETE CASCADE,
    tag  TEXT NOT NULL,
    PRIMARY KEY (uid, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_model_tag_tag ON model_tag(tag);

CREATE TABLE IF NOT EXISTS model_category (
    uid       TEXT NOT NULL REFERENCES model(uid) ON DELETE CASCADE,
    category  TEXT NOT NULL,
    PRIMARY KEY (uid, category)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_model_cat_cat ON model_category(category);

-- resumability: one row per tile, survives a killed crawl
CREATE TABLE IF NOT EXISTS crawl_partition (
    license      TEXT NOT NULL,
    face_lo      INTEGER NOT NULL,
    face_hi      INTEGER NOT NULL,             -- -1 == unbounded
    state        TEXT NOT NULL,                -- pending|running|drained|saturated|failed
    next_cursor  TEXT,                         -- opaque `next` URL, resume point
    n_seen       INTEGER DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    note         TEXT,
    PRIMARY KEY (license, face_lo, face_hi)
);

-- lexical half of hybrid retrieval
CREATE VIRTUAL TABLE IF NOT EXISTS model_fts USING fts5(
    uid UNINDEXED, name, description, tags,
    tokenize = 'porter unicode61'
);
```

**Dedupe** is `INSERT ... ON CONFLICT(uid) DO UPDATE SET ..., last_seen=?` —
the same uid-keyed replace semantics `write_sketchfab_entry` already uses,
just at the row level instead of the file level. Overlapping tiles from §4.4
therefore cost bandwidth, never correctness.

**`license_slug` fails closed.** An unrecognised `license.uid` stores NULL,
and every downstream query filters `license_slug IN (accepted)`. An unknown
license is never silently treated as permissive. (The frozen catalog already
has 8 of 317 entries at `license: unknown` — those should be re-checked
against the mirror once it exists.)

---

## 6. Required client change — persistent browser session

**This is the single biggest cost item and it is a bug-shaped problem, not a
scale problem.**

`SketchfabClient._browser_request_ctx()` currently launches a fresh headless
Chromium **per request** and closes it on exit. That is correct and cheap for
the current Tier-S usage (a handful of calls per generation run). For a
mirror crawl it is fatal:

```
~10^5–10^6 downloadable models  ÷  24 results/page  =  ~4k–40k+ requests
× ~1–3 s cold Chromium launch each                  =  ~2–33 hours of pure
                                                       browser startup,
                                                       before any network time
```

Fix: add a session-scoped context that launches **once** and is reused for
the whole crawl, leaving the existing per-call path untouched for Tier S.

```python
@contextmanager
def session(self) -> Iterator[Optional["SketchfabClient"]]:
    """Hold ONE browser open across many calls. Crawl-only — Tier S keeps
    using the per-call path so a generation run never leaves a browser alive.
    """
    with self._browser_request_ctx() as req:
        if req is None:
            yield None
            return
        prev, self._req = getattr(self, "_req", None), req
        try:
            yield self
        finally:
            self._req = prev
```

…and `_browser_request_ctx` gains a first line: `if getattr(self, "_req", None): yield self._req; return`.
Roughly 12 lines. Everything else in the client is reused as-is.

Two smaller client additions:

- `search()` must accept and return a **cursor**. Today it returns
  `r.json().get("results")` and throws away `next`. Add
  `search_page(url_or_params) -> (results, next_url)`.
- `search()` must expose `license`, `sort_by`, and `archives_flavours`
  (all real params, verified in the spec) so the tiling in §4.3 can be
  expressed server-side rather than filtered client-side.

---

## 7. Crawl driver

New file `src/sketchfab_mirror.py` (store + sweep), driven by
`tools/build_mirror.py` and wired to `main.py sketchfab-mirror`, matching the
existing `main.py unity-catalog` / `main.py rag-build` / `main.py sketchfab-fetch`
command shape.

```
python main.py sketchfab-mirror --probe        # §10 measurements only, no writes
python main.py sketchfab-mirror --build        # full sweep, resumable
python main.py sketchfab-mirror --refresh      # incremental, §8
python main.py sketchfab-mirror --stats        # rows, coverage, saturated tiles
python main.py sketchfab-mirror --index        # rebuild FTS5 + rag_sketchfab
```

Resume semantics: `--build` reads `crawl_partition`, skips `drained`, resumes
`running` from `next_cursor`, and re-queues `failed`. Killing the process at
any point loses at most one page.

---

## 8. Incremental refresh

A full sweep is a one-off. Steady state is cheap:

- **New models:** `sort_by=-publishedAt` with no face-count filter, drain
  until you hit a `published_at` older than `MAX(published_at)` from the last
  refresh. Bounded by upload rate, not corpus size. Nightly.
- **Cheap window:** the `date` param ("limit search to a specific period only,
  in days") gives a coarse recency filter for a targeted top-up.
- **Churn (deletions, license changes):** not detectable by search alone. A
  model that disappears simply stops appearing. Handle it at *use* time —
  `get_download_urls()` already returns `{}` for a model that has become
  unavailable, and Tier S already treats that as a clean miss. Do **not**
  try to detect deletions by re-crawling; mark rows stale by `last_seen` age
  instead.

---

## 9. Retrieval layer

Reuse the existing RAG stack rather than introducing a second one.

**Dense half** — new Chroma collection `rag_sketchfab`, built by a new
`rag/scrapers/sketchfab_mirror.py` in the same shape as the existing scrapers,
registered in `rag/indexer.py::COLLECTION_MAP` as domain `sketchfab`.
Same embedder (`BAAI/bge-small-en-v1.5`), same 256 batch size.

Document text per model — deliberately compact, since bge-small truncates:

```
{name}. {category}. {top 12 tags, comma-joined}. {description[:400]}
```

Metadata stored alongside for pre-filtering: `uid`, `license_slug`,
`face_count`, `user_username`, `archive_gltf_size`.

**Lexical half** — the `model_fts` FTS5 table. This directly addresses the
`objects_xxx.txt` failure mode already diagnosed on the GTA5 side: short,
low-semantic-density names ("crate01", "prop_bench_02") rank badly under pure
dense retrieval and well under BM25.

**Fusion** — reciprocal rank fusion, no tuned weights:

```python
score(uid) = Σ_r  1 / (k + rank_r(uid))      # k = 60, both retrievers
```

RRF is chosen over a weighted sum because it needs no score normalisation
between a cosine distance and a BM25 score, and no per-domain calibration.

**Hard filters applied before ranking** (this is the "full control" the plan
is actually after):

```sql
WHERE license_slug IN ('cc0','by','by-sa','by-nd')
  AND face_count BETWEEN :min_faces AND :max_faces   -- style band, from sketchfab_style
  AND archive_gltf_size < :max_bytes                 -- reject 70MB photogrammetry dumps
  AND (:require_rigged = 0 OR is_rigged = 1)         -- character entities
```

`src/sketchfab_style.py::search_params_for()` already computes the face-count
band per run. It now feeds a local SQL predicate instead of a remote API
param — same knob, applied to a corpus you control.

---

## 10. Unknowns that must be measured before committing to §4

Every number in §4 depends on values the spec does not publish. Phase 0 is a
read-only probe; **do not size the sweep before it runs.**

| # | Question | Method | Blocks |
|---|---|---|---|
| P1 | Cursor depth cap `D` — how deep does `next` keep working? | Drain one broad query, count results until `next` is null or errors | §4.4 bisection trigger |
| P2 | `min_face_count`/`max_face_count` boundary — inclusive or exclusive? | Query `[0,1000]` and `[1000,2000]`, check whether a known 1000-face model appears in both, one, or neither | §4.3 band tiling |
| P3 | Max `count` per page | Binary-search `count=24 → 48 → 100`; find where it clamps | request budget |
| P4 | Real 429 threshold on `/v3/search` | Ramp request rate until 429, back off, record | §11 pacing |
| P5 | Is `GET /v3/tags` paginated / enumerable at all? | Call it, count, follow `next` | vocabulary source only |
| P6 | Corpus size — how many `downloadable=true` models exist? | Read `totalCount` if present on the search response; else estimate from tile counts | whether §4 is hours or days |
| P7 | Does `license=` filter actually partition disjointly? | Sum per-license counts vs unfiltered count | §4.2 disjointness claim |

P7 is the one that can invalidate the whole design. If `license` turns out
not to be total (e.g. models with no license set are excluded from every
license-filtered query), the outer axis needs a fifth "no license" tile —
recoverable, but it must be known before the sweep, not after.

---

## 11. Rate, quota, and why metadata-only

**The download endpoint is the quota'd one.** `/v3/models/{uid}/download`
is what returns 429 under bulk use — this is the documented complaint in
`sketchfab/unity-plugin#27`. `/v3/search` is comparatively cheap. A
metadata-only mirror therefore sits almost entirely off the rate-limited path.

**Why not mirror the archives** (the option not chosen):

- The repo's own measurements in `sketchfab_client.py`: 25 MB archive in
  ~31 s, 70 MB in ~102 s, ≈0.7 MB/s. At ~10^5 downloadable models averaging
  ~20 MB, that is ~2 PB-seconds — on the order of **a month of continuous
  transfer for ~2 TB**, and that is the optimistic end of the model-count range.
- It would run straight into the 429 quota above.
- It is the part most likely to breach Sketchfab's terms (§12).
- It buys nothing the lazy path doesn't: a generation run touches tens of
  models, not 10^5.

**Pacing policy for the metadata sweep:**

- Single connection, strictly sequential. No parallelism. The Playwright
  session (§6) already serialises naturally.
- Fixed inter-request delay, floor 250 ms, tuned up from P4's measurement.
- Exponential backoff with jitter on 429 / 5xx: 2 s → 4 s → 8 s → … cap 5 min,
  then mark the tile `failed` and move on rather than hammering.
- Honour `Retry-After` when present.
- Checkpoint `next_cursor` after every page, so an abort costs one page.
- Budget the whole sweep to run overnight at a rate that would be
  unremarkable in Sketchfab's logs — not as fast as the API will allow.

---

## 12. Legal position — stated honestly

I have not read Sketchfab's current Terms of Service in preparing this, and
you should before running a full sweep. What I can say precisely:

- **The API is official and token-authenticated.** Using it is not
  circumvention. But "official API" ≠ "unlimited use", and near-total
  enumeration of a commercial marketplace's catalogue is a different activity
  from the per-entity lookups Tier S does today — expect the ToS to have
  something to say about it.
- **`src/sketchfab_client.py` documents a deliberate AWS WAF Bot Control
  bypass** (cold Chromium to defeat JA3/TLS fingerprinting). That mechanism
  exists to limit automated access. Using it at Tier-S volume is one thing;
  using it to sweep the catalogue is materially harder to characterise as
  intended use. This is worth a conscious decision, not a default.
- **The metadata is not the assets.** A local index of names, tags, and
  license slugs is derived factual data. Redistributing that index publicly
  is a separate question from building it for local use, and a riskier one.
- **Per-model licenses still govern the assets themselves.** The existing
  `accepted_licenses` gate and the `attribution` field in the frozen catalog
  are the right mechanism and must carry through to the mirror unchanged. The
  8 `license: unknown` entries currently in `sketchfab_catalog.json` should be
  resolved, not inherited.
- **A conservative alternative if any of the above gives you pause:**
  scope the sweep to `license=cc0` plus the categories that matter for game
  asset generation. That is a small fraction of the corpus, the most clearly
  permissive slice of it, and it very likely covers the actual failure mode
  you are trying to fix.

---

## 13. Why this fixes the failure it is meant to fix

The diagnosed problem is not that Sketchfab search is bad. It is that
**asset resolution falls back to a local library whose vocabulary lacks the
needed words** (2,953 FBX assets, but only 23 categories and 40 tags exposed
to the schema-fill prompt) — `ood_stack` hard-failing on `BackgroundProps`, `ood_sort`
resolving `skateboard.fbx` and `building-type-i.fbx` for a block-sorting
game, `ood_balance` pulling `alien.fbx` and `Spectator.fbx` despite
populated `model_hint`s.

A mirror fixes that specifically:

| Today | With the mirror |
|---|---|
| One live query, ~24 results, ranked by Sketchfab's relevance | Whole corpus, ranked by *your* function |
| Style band applied as a remote API param, post-hoc | Style band as a SQL predicate before ranking |
| Miss → fall through to 2,953 local FBX assets | Miss → 10^5 candidates with real tags |
| `model_hint` matched against a 23-category / 40-tag vocabulary | `model_hint` matched against open vocabulary |
| Nondeterministic (remote ranking can change) | Deterministic over a frozen DB snapshot |

That last row matters most for this codebase: a local mirror makes Tier S
**reproducible**. Today an identical prompt can resolve differently next week
because Sketchfab reranked. Against a pinned mirror snapshot it cannot — which
is exactly the frozen-artifact discipline `docs/model-pipeline-architecture.md`
already applies everywhere else.

---

## 14. Phases and verification gates

| Phase | Deliverable | Gate — must pass before the next phase |
|---|---|---|
| **0. Probe** | `tools/build_mirror.py --probe`, a numbers table | P1–P7 answered with measured values, not assumptions |
| **1. Client** | `session()`, cursor-returning `search_page()`, `license`/`sort_by` params | Existing `tests/test_sketchfab_client.py` still green; one session issues N requests with exactly 1 browser launch |
| **2. Store** | `src/sketchfab_mirror.py`, DDL from §5 | All 317 uids from the frozen catalog are found in a mirror built over their face-count bands — a round-trip that proves the tiling doesn't drop known-good rows |
| **3. Sweep** | adaptive tiling + resume | Sum of per-tile `n_seen` ≈ P6's corpus estimate; `crawl_partition` has zero `saturated` rows, or every one is logged with its gap |
| **4. Refresh** | `--refresh` | A refresh run immediately after a build inserts ~0 new rows and updates `last_seen` on all |
| **5. Retrieval** | FTS5 + `rag_sketchfab` + RRF | Same query, run 3×, returns byte-identical uid ordering |
| **6. Rewire** | Tier S queries mirror first, live search as fallback | The four OOD prompts (`ood_ring`, `ood_stack`, `ood_sort`, `ood_balance`) resolve on-prompt assets; `ood_stack`'s `BackgroundProps` no longer hard-fails |

Phase 6's gate is the real one. Phases 0–5 are infrastructure; if the OOD
prompts still resolve `alien.fbx` for a tray-balancing game, the mirror was
not the bottleneck and this document was wrong.

---

## 15. Known risks

1. **P7 invalidates the tiling** (§10) — recoverable, adds a tile.
2. **`D` is small** (say 1000). Then bisection depth grows and the request
   count with it. At `D = 1000` and 10^5 models, expect ~200+ tiles rather
   than 48. Still tractable; changes the sweep from overnight to a weekend.
3. **WAF escalation.** Sketchfab may respond to sustained automated access by
   tightening the fingerprint gate. The client already degrades to a clean
   miss on browser failure, so a hardened gate breaks the *crawl*, not
   generation. Do not "fix" that by escalating the bypass.
4. **Mirror rot.** A pinned snapshot is deterministic and progressively wrong.
   `last_seen` age is the signal; `get_download_urls()` returning `{}` at use
   time is the safety net.
5. **The classifier, not the catalogue, may be the real bottleneck.** The
   diagnosed chain (`classify_intent` called with one argument at
   `unity_pipeline.py:1038`, so the concept channel is dead code) means the
   query text reaching Sketchfab may itself be wrong. A perfect mirror
   searched with a wrong query returns wrong assets faster. Consider landing
   the concept-channel fix **before** Phase 6, so Phase 6's gate measures the
   mirror rather than the classifier.
