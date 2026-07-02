"""
rag/indexer.py — Embed corpus → ChromaDB vector index.

Run from the project root:
    python -m rag.indexer                   # build / update all domains
    python -m rag.indexer --reset           # drop and rebuild all domains
    python -m rag.indexer --domain unity    # rebuild only the unity collection
    python -m rag.indexer --stats           # show index counts by domain/kind

Collections (one per domain, fully isolated):
    rag_gta5   — peds, vehicles, animations, scenarios, audio, weapons, props
    rag_unity  — packages, components, api_verbs, scene_rules, genre_atoms
    rag_shared — atoms (engine-agnostic IR)
"""
import sys
import json
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"
INDEX_DIR  = Path(__file__).parent / "index"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
BATCH_SIZE  = 256

COLLECTION_MAP = {
    "gta5":   "rag_gta5",
    "unity":  "rag_unity",
    "shared": "rag_shared",
}

_CORPUS: dict[str, list[tuple[str, str]]] = {
    "gta5": [
        ("gta5/peds.jsonl",       "ped"),
        ("gta5/vehicles.jsonl",   "vehicle"),
        ("gta5/scenarios.jsonl",  "scenario"),
        ("gta5/audio.jsonl",      "audio"),
        ("gta5/weapons.jsonl",    "weapon"),
        ("gta5/props.jsonl",      "prop"),
        ("gta5/animations.jsonl", "animation"),
    ],
    "unity": [
        ("unity/packages.jsonl",    "package"),
        ("unity/components.jsonl",  "component"),
        ("unity/api_verbs.jsonl",   "api_verb"),
        ("unity/scene_rules.jsonl", "scene_rule"),
        ("unity/genre_atoms.jsonl", "genre_atom"),
    ],
    "shared": [
        ("shared/atoms.jsonl", "atom"),
    ],
}


# ── helpers ───────────────────────────────────────────────────────────────────

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
                seen[e["id"]] = e
    return list(seen.values())


def _make_metadata(e: dict) -> dict:
    return {
        "domain":    e["domain"],
        "kind":      e["kind"],
        "canonical": e["canonical"],
        "label":     e["label"],
        "tags":      ",".join(e.get("tags", [])),
    }


# ── stats ─────────────────────────────────────────────────────────────────────

def stats() -> None:
    import chromadb
    from collections import Counter

    INDEX_DIR.mkdir(exist_ok=True)
    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    for domain, col_name in COLLECTION_MAP.items():
        try:
            col    = client.get_collection(col_name)
            result = col.get(include=["metadatas"])
            counts = Counter(m["kind"] for m in result["metadatas"])
            print(f"\n[{domain}]  '{col_name}'  ({col.count()} total)")
            for kind, n in sorted(counts.items()):
                print(f"  {kind:<16} {n:>6}")
        except Exception:
            print(f"\n[{domain}]  '{col_name}'  — not built yet")

    print()


# ── build ─────────────────────────────────────────────────────────────────────

def build(reset: bool = False, domains: list[str] | None = None) -> None:
    import chromadb
    from fastembed import TextEmbedding

    INDEX_DIR.mkdir(exist_ok=True)
    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    target_domains = domains or list(_CORPUS.keys())

    print(f"[indexer] loading embedding model '{EMBED_MODEL}' ...")
    model = TextEmbedding(EMBED_MODEL)

    for domain in target_domains:
        col_name = COLLECTION_MAP[domain]
        files    = _CORPUS[domain]

        if reset:
            print(f"\n[indexer] dropping '{col_name}' ...")
            try:
                client.delete_collection(col_name)
            except Exception:
                pass

        collection = client.get_or_create_collection(
            col_name,
            metadata={"hnsw:space": "cosine"},
        )

        existing = set(collection.get(include=[])["ids"]) if not reset else set()
        print(f"\n[{domain}]  '{col_name}'  —  {len(existing)} existing entries")

        domain_added = 0
        for rel_path, kind in files:
            entries = _load_corpus(rel_path)
            new     = [e for e in entries if e["id"] not in existing]
            print(f"  [{kind}]  {len(entries)} corpus  /  {len(new)} new")

            if not new:
                continue

            batches = range(0, len(new), BATCH_SIZE)
            for i in batches:
                batch = new[i : i + BATCH_SIZE]
                texts = [e["chunk_text"] for e in batch]
                ids   = [e["id"]         for e in batch]
                metas = [_make_metadata(e) for e in batch]

                vecs = [v.tolist() for v in model.embed(texts)]

                collection.upsert(
                    ids=ids,
                    embeddings=vecs,
                    documents=texts,
                    metadatas=metas,
                )
                domain_added += len(batch)

                done = min(i + BATCH_SIZE, len(new))
                if len(batches) > 4 and (i // BATCH_SIZE) % 8 == 0:
                    print(f"    embedded {done}/{len(new)} ...")

            print(f"  [{kind}]  {len(new)} entries added")

        print(f"[{domain}]  added {domain_added}  |  collection total: {collection.count()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    argv     = sys.argv[1:]
    do_reset = "--reset" in argv
    do_stats = "--stats" in argv

    domains: list[str] | None = None
    if "--domain" in argv:
        idx = argv.index("--domain")
        if idx + 1 < len(argv) and argv[idx + 1] in _CORPUS:
            domains = [argv[idx + 1]]
        else:
            valid = ", ".join(_CORPUS.keys())
            print(f"[indexer] --domain requires one of: {valid}")
            sys.exit(1)

    if do_stats:
        try:
            stats()
        except Exception as e:
            print(f"[indexer] index not built yet: {e}")
            print("         Run: python -m rag.indexer")
        return

    build(reset=do_reset, domains=domains)


if __name__ == "__main__":
    main()
