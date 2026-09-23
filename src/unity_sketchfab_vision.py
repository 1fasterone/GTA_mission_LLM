"""
Vision-based Sketchfab candidate selection.

Keyword-overlap scoring (src/unity_entity_asset_resolver.py's _score,
reused by sketchfab_freeze.fetch_and_freeze's score_fn) ranks candidates by
tag/name TEXT alone — it cannot tell a "skydiving helmet" search from a
"baseball helmet" result that merely shares the one token "helmet". Live
regression: LeadSkydiverJumpsuit (model_hint "lead skydiver jumpsuit
helmet") resolved to a baseball sports-asset helmet, because whatever
narrowed the query down to something Sketchfab's own marketplace search
would answer lost the "skydiver"/"jumpsuit" context, and every candidate
Sketchfab handed back for the surviving bare "helmet" scored on that one
shared word — text scoring can only pick the best of the candidate pool it's
given, never tell the pool itself is off-target.

This module asks a VISION-capable Pipeline LLM to look at the top
keyword-ranked candidates' own thumbnails and say which one (if any)
actually looks like the entity, before the download+Blender-convert loop in
sketchfab_freeze.py spends real time on a keyword-plausible but
visually-wrong pick.

The result is an explicit pre-freeze decision. A "none match" verdict is a
refusal, and a missing thumbnail/model/parse result is unavailable. Both
block freezing; neither falls through to the caller's keyword-ranked order.
Keyword ranking may propose the candidate pool, but it may not make the
visual decision.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionSelection:
    """Authoritative result of pre-freeze visual adjudication."""

    decision: str
    uid: Optional[str] = None
    reason: str = ""
    candidate_uids: tuple[str, ...] = ()
    preview_sha256: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in {"select", "refuse", "unavailable"}:
            raise ValueError(f"unsupported visual decision: {self.decision!r}")
        if self.decision == "select" and not self.uid:
            raise ValueError("select decision requires a candidate uid")
        if self.decision != "select" and self.uid is not None:
            raise ValueError("non-select visual decision cannot carry a uid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "uid": self.uid,
            "reason": self.reason,
            "candidate_uids": list(self.candidate_uids),
            "preview_sha256": dict(sorted(self.preview_sha256.items())),
        }


_SYSTEM = """\
You are a visual quality-control step in a 3D asset pipeline. You are shown \
several candidate 3D model thumbnails, each numbered, sourced for ONE entity \
that needs a real 3D model in a game. A separate keyword search already \
picked these candidates by tag/name text overlap, which cannot tell a \
correct object from a same-word coincidence (a "baseball helmet" matches a \
"skydiving helmet" search on the single word "helmet" despite being the \
wrong object entirely) — your job is the honest visual check text search \
cannot do.

Pick the ONE candidate whose thumbnail actually looks like the entity \
described. If NONE of the candidates plausibly show that kind of object —
every one is visually the wrong category, not just an imperfect style match —
say so rather than forcing a pick; a wrong-object model in the game is worse
than falling through to the next source.

Output ONLY a valid JSON object, no other text:
{"pick": <candidate number, or null if none genuinely match>, "reason": "one short sentence"}
"""

_USER_TMPL = """\
ENTITY: {name} ({kind})
WHAT IT SHOULD LOOK LIKE: {description}

{count} candidate thumbnails follow, numbered 1 to {count} in the order shown.

Which number is the best real visual match? Output JSON only.
"""

# Sketchfab's own search-result thumbnail set (`thumbnails.images[]`, each
# {"url", "width", "height", "size"}) offers several resolutions — a vision
# call needs just enough pixels to tell object CATEGORY apart, not a full-res
# render, so this picks the entry closest to a small target width rather than
# always the largest (which would waste tokens/bandwidth on say a 2048px shot
# LM Studio downsamples internally anyway).
_TARGET_THUMBNAIL_PX = 256


def _thumbnail_url(model: dict) -> Optional[str]:
    """The search result's own thumbnail URL closest to _TARGET_THUMBNAIL_PX
    wide, or None when the result carries no thumbnails at all — an older/
    malformed API response shape this never assumes, only checks for."""
    images = ((model.get("thumbnails") or {}).get("images") or [])
    images = [im for im in images if isinstance(im, dict) and im.get("url")]
    if not images:
        return None
    images.sort(key=lambda im: abs(int(im.get("width") or 0) - _TARGET_THUMBNAIL_PX))
    return images[0]["url"]


def _encode_data_uri(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _extract_json(raw: str) -> Optional[dict]:
    """Same defensive parse unity_sketchfab_translator.py uses: strip
    think-blocks and markdown fences, take the outermost {...}, degrade to
    None on anything that isn't a clean JSON object."""
    try:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return None
        payload = json.loads(raw[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def select_best_candidate(entity_name: str, entity_kind: str, description: str,
                          candidates: list[dict], *, client: Any, llm: Any,
                          max_images: int = 6, seed: int = 0) -> VisionSelection:
    """Ask the vision LLM which of `candidates` (raw Sketchfab model dicts,
    already keyword-ranked best-first by the caller) visually matches the
    entity. Returns a `VisionSelection` decision. The caller must freeze only
    the selected UID and must reject both refusal and unavailable decisions.
    """
    pool = candidates[:max(1, max_images)]
    images: list[str] = []
    uids: list[str] = []
    preview_sha256: dict[str, str] = {}
    for model in pool:
        url = _thumbnail_url(model)
        if not url:
            continue
        raw_bytes = client.fetch_image_bytes(url)
        if not raw_bytes:
            continue
        uid = str(model.get("uid") or "")
        if not uid:
            continue
        images.append(_encode_data_uri(raw_bytes))
        uids.append(uid)
        preview_sha256[uid] = hashlib.sha256(raw_bytes).hexdigest()

    if len(images) < 1:
        return VisionSelection(
            "unavailable", reason="no candidate thumbnail was available",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )

    try:
        reply = llm.chat(
            system=_SYSTEM,
            user=_USER_TMPL.format(
                name=entity_name, kind=entity_kind or "prop",
                description=description or entity_name, count=len(images),
            ),
            images=images,
            temperature=0.2,
            thinking=False,
            seed=seed or None,
        )
    except Exception as exc:  # noqa: BLE001 — a model with no attached mmproj,
        # a server that rejects image_url content, or a timeout on a slower
        # vision pass — all land here as an unavailable decision.
        log.debug("sketchfab vision select(%r) LLM call failed: %s", entity_name, exc)
        return VisionSelection(
            "unavailable", reason="vision model call failed",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )

    payload = _extract_json(reply)
    if payload is None:
        return VisionSelection(
            "unavailable", reason="vision response was not valid JSON",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    if set(payload) != {"pick", "reason"}:
        return VisionSelection(
            "unavailable", reason="vision response had the wrong fields",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    reason = str(payload.get("reason") or "").strip()
    pick = payload.get("pick")
    if pick is None:
        log.debug("sketchfab vision select(%r): explicit refusal (%s)",
                  entity_name, reason or "no reason given")
        return VisionSelection(
            "refuse", reason=reason or "no candidate visually matches",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    if isinstance(pick, bool):
        return VisionSelection(
            "unavailable", reason="vision pick must be an integer or null",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    try:
        idx = int(pick) - 1
    except (TypeError, ValueError):
        return VisionSelection(
            "unavailable", reason="vision pick was not an integer",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    if not (0 <= idx < len(uids)) or not uids[idx]:
        return VisionSelection(
            "unavailable", reason="vision pick was outside the candidate list",
            candidate_uids=tuple(uids), preview_sha256=preview_sha256,
        )
    log.debug("sketchfab vision select(%r): picked candidate %d/%d (%s)",
              entity_name, idx + 1, len(images), reason or "no reason given")
    return VisionSelection(
        "select", uid=uids[idx], reason=reason or "visual match",
        candidate_uids=tuple(uids), preview_sha256=preview_sha256,
    )
