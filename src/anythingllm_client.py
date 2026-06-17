"""
AnythingLLM client — synchronous RAG workspace queries.
Used in Step 4 to resolve plain-English descriptions → GTA5 model IDs.

Debug output (--debug / -d flag):
  Set log level to DEBUG before creating the client; all RAG queries and
  raw responses will be written via logging.debug().
"""

import logging
import re
import httpx
from typing import Optional

log = logging.getLogger(__name__)


class AnythingLLMClient:
    def __init__(self, base_url: str, api_key: str, workspace_slug: str,
                 timeout: float = 60.0):
        self.base_url       = base_url.rstrip("/")
        self.workspace_slug = workspace_slug
        self.timeout        = timeout
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }

    def workspace_details(self, slug: str | None = None) -> dict:
        """
        GET /api/v1/workspace/{slug} — returns the workspace payload (incl.
        documents list). Used by rag-status / the Backends page to surface a
        per-workspace document count. Returns {} on any failure.
        """
        slug = slug or self.workspace_slug
        url = f"{self.base_url}/api/v1/workspace/{slug}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.get(url, headers=self._headers)
                r.raise_for_status()
            return r.json() or {}
        except Exception as exc:  # noqa: BLE001 — diagnostic helper
            log.debug("workspace_details(%s) failed: %s", slug, exc)
            return {}

    def query(self, question: str) -> str:
        """Send a query to the workspace and return the text response."""
        url = f"{self.base_url}/api/v1/workspace/{self.workspace_slug}/chat"
        log.debug("→ RAG REQUEST  workspace=%s\n  %s", self.workspace_slug, question)
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(url, headers=self._headers,
                            json={"message": question, "mode": "query"})
            r.raise_for_status()
        answer = r.json().get("textResponse", "")
        log.debug("← RAG RESPONSE  %s", answer[:300] + ("…" if len(answer) > 300 else ""))
        return answer

    def resolve_model(self, description: str, category: str) -> Optional[str]:
        """
        Ask the RAG workspace for the GTA5 model ID matching a description.
        category: "ped" | "vehicle" | "object" | "weapon"
        """
        question = (
            f"What is the exact GTA5 {category} model name for: {description}? "
            "Reply with only the model ID in lowercase with underscores. Nothing else."
        )
        answer = self.query(question)
        match = re.search(r"\b([a-z][a-z0-9_]{2,})\b", answer.lower())
        return match.group(1) if match else None

    def resolve_package(self, description: str, unity_version: str) -> Optional[str]:
        """
        Ask the RAG workspace for the Unity Package Manager ID matching a
        plain-English description. Returns 'com.unity.<id>' or None.
        """
        question = (
            f"What is the exact Unity Package Manager ID for: {description}? "
            f"Target Unity version: {unity_version}. "
            "Reply with only the package ID starting with 'com.' (e.g. "
            "com.unity.render-pipelines.universal). Nothing else."
        )
        answer = self.query(question)
        match = re.search(r"\bcom\.[a-z][a-z0-9._-]+\b", answer.lower())
        return match.group(0) if match else None

    def resolve_audio(self, context: str, audio_type: str) -> Optional[str]:
        """Ask for an audio name matching context and type."""
        question = (
            f"What GTA5 audio name should play for a {audio_type} event: {context}? "
            "Reply with only the audio name string. Nothing else."
        )
        answer = self.query(question).strip().strip('"').strip("'")
        return answer if answer else None
