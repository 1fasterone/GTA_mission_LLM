"""
RagClient — domain-isolated vector search across gta5 / unity / shared collections.

Collections:
    rag_gta5   — peds, vehicles, animations, scenarios, audio, weapons, props
    rag_unity  — packages, components, api_verbs, scene_rules, genre_atoms
    rag_shared — atoms (engine-agnostic IR)

Usage:
    from rag.query import RagClient
    rag = RagClient()
    rag.resolve_model("corrupt cop", "peds")          -> "s_m_y_cop_01"
    rag.resolve_model("police car", "vehicles")       -> "police"
    rag.resolve_package("physics simulation", "2022") -> "com.unity.physics"
    rag.resolve_component("rigid body")               -> "Rigidbody"
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

INDEX_DIR   = Path(__file__).parent / "index"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
_THRESHOLD  = 0.55

COLLECTION_MAP = {
    "gta5":   "rag_gta5",
    "unity":  "rag_unity",
    "shared": "rag_shared",
}


class RagClient:
    """
    Local vector-search resolver with per-domain collection isolation.
    Collections are lazy-loaded on first access per domain.
    Thread-safe for reads (ChromaDB PersistentClient supports concurrent reads).
    """
    is_corpus_backed: bool = True

    def __init__(self, index_dir: str | None = None,
                 model_name: str = EMBED_MODEL,
                 threshold: float = _THRESHOLD):
        self._idx_path   = index_dir or str(INDEX_DIR)
        self._model_name = model_name
        self._threshold  = threshold
        self._model      = None
        self._client     = None
        self._collections: dict[str, object] = {}

    # ── lazy init ─────────────────────────────────────────────────────────────

    def _ensure_domain(self, domain: str):
        if domain in self._collections:
            return self._collections[domain]

        import chromadb
        from fastembed import TextEmbedding

        if self._client is None:
            self._client = chromadb.PersistentClient(path=self._idx_path)
        if self._model is None:
            self._model = TextEmbedding(self._model_name)

        col_name = COLLECTION_MAP[domain]
        self._collections[domain] = self._client.get_collection(col_name)
        return self._collections[domain]

    # ── core search ───────────────────────────────────────────────────────────

    def _search(self, text: str, domain: str, kind: str | None = None, n: int = 3) -> list[dict]:
        """Return top-n matches from the given domain collection."""
        collection = self._ensure_domain(domain)
        vec   = next(iter(self._model.embed([text]))).tolist()
        where = {"kind": kind} if kind else None

        result = collection.query(
            query_embeddings=[vec],
            n_results=n,
            where=where,
            include=["metadatas", "distances"],
        )
        hits = []
        for meta, dist in zip(result["metadatas"][0], result["distances"][0]):
            hits.append({**meta, "distance": dist})
        return hits

    def _best(self, text: str, domain: str, kind: str | None = None) -> Optional[str]:
        hits = self._search(text, domain=domain, kind=kind, n=1)
        if hits and hits[0]["distance"] < self._threshold:
            return hits[0]["canonical"]
        return None

    # ── GTA5 resolvers ────────────────────────────────────────────────────────

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
        return self._best(query, domain="gta5", kind=kind)

    def resolve_audio(self, context: str, audio_type: str) -> Optional[str]:
        query = f"GTA5 audio {audio_type} event: {context}"
        return self._best(query, domain="gta5", kind="audio")

    def resolve_animation(self, description: str) -> Optional[str]:
        return self._best(f"GTA5 animation: {description}", domain="gta5", kind="animation")

    def resolve_scenario(self, description: str) -> Optional[str]:
        return self._best(f"GTA5 ped behavior: {description}", domain="gta5", kind="scenario")

    # ── Unity resolvers ───────────────────────────────────────────────────────

    def resolve_package(self, description: str, unity_version: str = "") -> Optional[str]:
        suffix = f" (Unity {unity_version})" if unity_version else ""
        return self._best(f"Unity package for {description}{suffix}", domain="unity", kind="package")

    def resolve_component(self, description: str) -> Optional[str]:
        return self._best(f"Unity component: {description}", domain="unity", kind="component")

    def resolve_api_verb(self, description: str) -> Optional[str]:
        return self._best(f"Unity API verb: {description}", domain="unity", kind="api_verb")

    def resolve_scene_rule(self, description: str) -> Optional[str]:
        return self._best(f"Unity scene rule: {description}", domain="unity", kind="scene_rule")

    def resolve_genre_atom(self, description: str) -> Optional[str]:
        return self._best(f"Unity genre atom: {description}", domain="unity", kind="genre_atom")

    # ── Shared / atom resolvers ───────────────────────────────────────────────

    def resolve_atom(self, description: str) -> Optional[str]:
        return self._best(f"atom: {description}", domain="shared", kind="atom")

    # ── Debug / CLI search ────────────────────────────────────────────────────

    def search(self, text: str, domain: str = "gta5", kind: str | None = None, n: int = 5) -> list[dict]:
        """Multi-result search — useful for debugging and the rag-query CLI."""
        return self._search(text, domain=domain, kind=kind, n=n)

    def is_ready(self, domain: str = "gta5") -> bool:
        try:
            col = self._ensure_domain(domain)
            return col.count() > 0
        except Exception:
            return False
