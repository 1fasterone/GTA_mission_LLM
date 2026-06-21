"""
rag/indexer.py — Embed corpus → ChromaDB vector index.

Run from the project root:
    python -m rag.indexer              # build / update index
    python -m rag.indexer --reset      # drop and rebuild from scratch
    python -m rag.indexer --stats      # show index counts by kind

Requires (already installed):
    pip install fastembed chromadb

On first run the ONNX model (~25 MB) is downloaded to the fastembed cache.
Subsequent runs are instant. Incremental: only embeds entries not already in the index.
"""
import sys
import json
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"
INDEX_DIR  = Path(__file__).parent / "index"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"  # 384-dim, ~25 MB ONNX, no PyTorch dep
BATCH_SIZE  = 256                    # ChromaDB upsert batch size
COLLECTION  = "rag_ontology"

_CORPUS = [
    ("gta5/peds.jsonl",       "ped"),
    ("gta5/vehicles.jsonl",   "vehicle"),
    ("gta5/scenarios.jsonl",  "scenario"),
    ("gta5/audio.jsonl",      "audio"),
    ("gta5/weapons.jsonl",    "weapon"),
    ("gta5/props.jsonl",      "prop"),
    ("gta5/animations.jsonl", "animation"),   # largest — 20K dicts, still fast
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_corpus(rel_path: str) -> list[dict]:
    path = CORPUS_DIR / rel_path
    if not path.exists():
        print(f"  [warn] missing: {path}  (run python -m rag.build first)")
        return []
    seen: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                seen[e["id"]] = e   # last-wins dedup on id
    return list(seen.values())


def _make_metadata(e: dict) -> dict:
    """ChromaDB metadata must be flat str/int/float/bool — no lists."""
    return {
        "domain":    e["domain"],
        "kind":      e["kind"],
        "canonical": e["canonical"],
        "label":     e["label"],
        "tags":      ",".join(e.get("tags", [])),
    }


# ── main ─────────────────────────────────────────────────────────────────────

def stats() -> None:
    import chromadb
    from collections import Counter

    client     = chromadb.PersistentClient(path=str(INDEX_DIR))
    collection = client.get_collection(COLLECTION)
    total      = collection.count()

    # peek at all metadata to count by kind (ChromaDB v0.4+ supports where filters)
    result  = collection.get(include=["metadatas"])
    counter = Counter(m["kind"] for m in result["metadatas"])

    print(f"\nIndex stats  ({INDEX_DIR})")
    for kind, n in sorted(counter.items()):
        print(f"  {kind:<15} {n:>6} entries")
    print(f"  {'Total':<15} {total:>6} entries\n")


def build(reset: bool = False) -> None:
    import chromadb

    INDEX_DIR.mkdir(exist_ok=True)
    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    if reset:
        print("[indexer] dropping existing collection ...")
        try:
            client.delete_collection(COLLECTION)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    existing = set(collection.get(include=[])["ids"]) if not reset else set()
    print(f"[indexer] existing entries in index: {len(existing)}")

    print(f"[indexer] loading embedding model '{EMBED_MODEL}' ...")
    from fastembed import TextEmbedding
    model = TextEmbedding(EMBED_MODEL)

    grand_total = 0

    for rel_path, kind in _CORPUS:
        entries = _load_corpus(rel_path)
        new     = [e for e in entries if e["id"] not in existing]
        print(f"\n[{kind}] {rel_path}:  {len(entries)} corpus entries,  {len(new)} new to embed")

        if not new:
            continue

        batches = range(0, len(new), BATCH_SIZE)
        for i in batches:
            batch = new[i : i + BATCH_SIZE]
            texts = [e["chunk_text"] for e in batch]
            ids   = [e["id"]         for e in batch]
            metas = [_make_metadata(e) for e in batch]

            # fastembed returns a generator of numpy arrays
            vecs = [v.tolist() for v in model.embed(texts)]

            collection.upsert(
                ids=ids,
                embeddings=vecs,
                documents=texts,
                metadatas=metas,
            )
            grand_total += len(batch)

            done = min(i + BATCH_SIZE, len(new))
            if len(batches) > 4 and (i // BATCH_SIZE) % 8 == 0:
                print(f"  embedded {done}/{len(new)} ...")

        print(f"[{kind}] done — {len(new)} entries added")

    total = collection.count()
    print(f"\n[indexer] complete.  Index total: {total} entries  ({grand_total} added this run)")


def main() -> None:
    argv     = sys.argv[1:]
    do_reset = "--reset" in argv
    do_stats = "--stats" in argv

    if do_stats:
        try:
            stats()
        except Exception as e:
            print(f"[indexer] index not built yet: {e}")
            print("         Run: python -m rag.indexer")
        return

    build(reset=do_reset)


if __name__ == "__main__":
    main()
