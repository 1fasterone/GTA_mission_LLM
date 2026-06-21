"""
RagClient — drop-in replacement for AnythingLLMClient.

Uses the local ChromaDB index built by python -m rag.indexer.
Same public interface as AnythingLLMClient so src/resolver.py can swap it in.

Usage:
    from rag.query import RagClient
    rag = RagClient()
    rag.resolve_model("corrupt cop", "peds")   -> "s_m_y_cop_01"
    rag.resolve_model("police car", "vehicles") -> "police"
    rag.resolve_audio("heist complete", "mission_passed") -> "MISSION_PASSED_JINGLE"
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

INDEX_DIR   = Path(__file__).parent / "index"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"   # ONNX via fastembed, no PyTorch dep
COLLECTION  = "rag_ontology"

# Cosine distance threshold: below this → confident match.
# ChromaDB cosine distance = 1 - cosine_similarity, so 0.0 = identical, 2.0 = opposite.
_THRESHOLD = 0.55


class RagClient:
    """
    Local vector-search resolver. Lazy-loads model + collection on first query.
    Thread-safe for read (ChromaDB PersistentClient is safe for concurrent reads).
    """
    # The corpus comes from verified DurtyFree data — every returned canonical
    # is a real GTA5 model name. The ModelResolver should skip its valid_models
    # guard when this flag is True.
    is_corpus_backed: bool = True

    def __init__(self, index_dir: str | None = None,
                 model_name: str = EMBED_MODEL,
                 threshold: float = _THRESHOLD):
        self._idx_path  = index_dir or str(INDEX_DIR)
        self._model_name = model_name
        self._threshold  = threshold
        self._model      = None
        self._collection = None

    # ── lazy init ────────────────────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._collection is not None:
            return
        import chromadb
        from fastembed import TextEmbedding

        client = chromadb.PersistentClient(path=self._idx_path)
        self._collection = client.get_collection(COLLECTION)
        self._model      = TextEmbedding(self._model_name)

    # ── core search ──────────────────────────────────────────────────────────

    def _search(self, text: str, kind: str | None = None, n: int = 3) -> list[dict]:
        """Return top-n matches with .canonical, .label, .tags, .distance."""
        self._ensure_ready()
        # fastembed returns a generator of numpy arrays; take the first (and only) result
        vec   = next(iter(self._model.embed([text]))).tolist()
        where = {"kind": kind} if kind else None

        result = self._collection.query(
            query_embeddings=[vec],
            n_results=n,
            where=where,
            include=["metadatas", "distances"],
        )
        hits = []
        for meta, dist in zip(result["metadatas"][0], result["distances"][0]):
            hits.append({**meta, "distance": dist})
        return hits

    def _best(self, text: str, kind: str | None = None) -> Optional[str]:
        """Return the canonical of the closest entry under the distance threshold."""
        hits = self._search(text, kind=kind, n=1)
        if hits and hits[0]["distance"] < self._threshold:
            return hits[0]["canonical"]
        return None

    # ── AnythingLLMClient-compatible interface ────────────────────────────────

    def resolve_model(self, description: str, category: str) -> Optional[str]:
        """
        GTA5 model resolver.
        category: "peds" | "vehicles" | "objects" | "weapons"
        """
        kind_map = {
            "peds":     "ped",
            "vehicles": "vehicle",
            "objects":  "prop",
            "weapons":  "weapon",
            "audio":    "audio",
        }
        kind  = kind_map.get(category, category.rstrip("s"))
        query = f"GTA5 {category}: {description}"
        return self._best(query, kind=kind)

    def resolve_audio(self, context: str, audio_type: str) -> Optional[str]:
        query = f"GTA5 audio {audio_type} event: {context}"
        return self._best(query, kind="audio")

    def resolve_package(self, description: str, unity_version: str) -> Optional[str]:
        """Unity Package Manager resolver (populated when Unity corpus is indexed)."""
        query = f"Unity package for {description} (Unity {unity_version})"
        return self._best(query, kind="package")

    # ── extended interface (beyond AnythingLLM) ───────────────────────────────

    def resolve_animation(self, description: str) -> Optional[str]:
        """Find the best GTA5 animation dict for a plain-English action."""
        return self._best(f"GTA5 animation: {description}", kind="animation")

    def resolve_scenario(self, description: str) -> Optional[str]:
        """Find the best NPC scenario for a behavior description."""
        return self._best(f"GTA5 ped behavior: {description}", kind="scenario")

    def search(self, text: str, kind: str | None = None, n: int = 5) -> list[dict]:
        """
        Public multi-result search — useful for debugging and the rag-query CLI.
        Returns list of {canonical, label, kind, tags, distance}.
        """
        return self._search(text, kind=kind, n=n)

    # ── status ────────────────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        try:
            self._ensure_ready()
            return self._collection.count() > 0
        except Exception:
            return False
