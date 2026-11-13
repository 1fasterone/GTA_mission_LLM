"""GPU hand-off: evict every resident LM Studio model before a GPU-heavy stage.

On a single consumer card the Pipeline LLM (LM Studio) and the image/audio
generators (ComfyUI, Stable Audio 3) cannot be resident together. Confirmed
live (2026-11-10, cuphead_style_brawler): ComfyUI started its sprite batch
while qwen3.5-9b was still loaded in LM Studio.

This module is lifecycle only. It never samples a model, so calling it after
the Step 3.5 freeze boundary is allowed (src/llm_boundary.py lets `unload`
through for the same reason) and it cannot change compiler output.

It evicts EVERY loaded instance, not only the model named in config.yaml:
whatever is resident takes VRAM, whether it was loaded by this run, by a JIT
request, or by hand in the LM Studio UI.

Discovery order: native REST `/api/v1/models` (`loaded_instances`), then the
older `/api/v0/models` (`state == "loaded"`). Unload order per instance: native
`POST /api/v1/models/unload`, then the `lms unload` CLI. When neither listing
answers but the `lms` CLI exists, `lms unload --all` is the last resort. The
listing is re-read afterwards, so a model that is still resident is reported
by name rather than assumed gone.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_LIST_TIMEOUT = 10.0
_UNLOAD_TIMEOUT = 30.0


@dataclass
class EvictionResult:
    """What an eviction attempt found and did."""

    attempted: bool = False          # False: backend is not LM Studio, or disabled
    listed: bool = False             # True when a listing endpoint answered
    evicted: list[str] = field(default_factory=list)
    still_resident: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when nothing is known to remain resident."""
        return not self.still_resident and self.error is None

    def summary(self) -> str:
        if not self.attempted:
            return "LM Studio eviction skipped (backend is not lm_studio or disabled)"
        if self.still_resident:
            return ("LM Studio model(s) still resident after eviction: "
                    + ", ".join(self.still_resident))
        if self.error:
            return f"LM Studio eviction could not confirm state: {self.error}"
        if self.evicted:
            return "LM Studio model(s) evicted: " + ", ".join(self.evicted)
        return "LM Studio had no model loaded"


def _lm_cfg(config: Optional[dict]) -> dict:
    return dict((config or {}).get("lm_studio") or {})


def eviction_enabled(config: Optional[dict]) -> bool:
    """`lm_studio.evict_before_gpu_stages` (default True) on an lm_studio backend."""
    cfg = _lm_cfg(config)
    if str(cfg.get("backend", "lm_studio")).lower() != "lm_studio":
        return False
    return bool(cfg.get("evict_before_gpu_stages", True))


def native_root(base_url: str) -> str:
    """`http://host:1234/v1` -> `http://host:1234` (the native REST root)."""
    root = str(base_url or "http://localhost:1234/v1").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def _headers(api_key: Optional[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_loaded_instances(root: str, *, api_key: Optional[str] = None,
                          client: Optional[Any] = None) -> Optional[list[str]]:
    """Instance ids currently loaded in LM Studio, or None when no listing answers.

    An empty list means LM Studio answered and nothing is loaded.
    """
    own = client is None
    http = client or httpx.Client(timeout=_LIST_TIMEOUT)
    try:
        try:
            resp = http.get(f"{root}/api/v1/models", headers=_headers(api_key))
            if resp.status_code == 200:
                ids: list[str] = []
                for model in (resp.json() or {}).get("models", []) or []:
                    for inst in model.get("loaded_instances") or []:
                        iid = inst.get("id") if isinstance(inst, dict) else None
                        if iid:
                            ids.append(str(iid))
                return ids
        except Exception as exc:  # noqa: BLE001 - try the older listing next
            log.debug("LM Studio /api/v1/models failed: %s: %s", type(exc).__name__, exc)
        try:
            resp = http.get(f"{root}/api/v0/models", headers=_headers(api_key))
            if resp.status_code == 200:
                return [
                    str(m.get("id"))
                    for m in (resp.json() or {}).get("data", []) or []
                    if m.get("state") == "loaded" and m.get("id")
                ]
        except Exception as exc:  # noqa: BLE001 - LM Studio not running
            log.debug("LM Studio /api/v0/models failed: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        if own:
            http.close()


def _unload_native(root: str, instance_id: str, *, api_key: Optional[str],
                   client: Any) -> bool:
    try:
        resp = client.post(f"{root}/api/v1/models/unload",
                           json={"instance_id": instance_id},
                           headers=_headers(api_key))
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - CLI fallback follows
        log.debug("native unload of %s failed: %s: %s",
                  instance_id, type(exc).__name__, exc)
        return False


def _unload_cli(args: list[str]) -> bool:
    lms = shutil.which("lms")
    if not lms:
        return False
    try:
        result = subprocess.run([lms, "unload", *args], capture_output=True,
                                text=True, timeout=_UNLOAD_TIMEOUT, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def evict_lm_studio_models(config: Optional[dict], *,
                           client: Optional[Any] = None) -> EvictionResult:
    """Unload every model LM Studio holds. Best effort; never raises."""
    result = EvictionResult()
    if not eviction_enabled(config):
        return result
    result.attempted = True

    cfg = _lm_cfg(config)
    root = native_root(str(cfg.get("base_url", "http://localhost:1234/v1")))
    api_key = cfg.get("api_key") or None

    own = client is None
    http = client or httpx.Client(timeout=_UNLOAD_TIMEOUT)
    try:
        loaded = list_loaded_instances(root, api_key=api_key, client=http)
        if loaded is None:
            # LM Studio's REST surface did not answer. When the CLI exists it
            # can still reach a running server; otherwise nothing is running.
            if shutil.which("lms") and _unload_cli(["--all"]):
                result.evicted.append("--all")
                return result
            result.error = "LM Studio REST API unreachable"
            return result
        result.listed = True
        for iid in loaded:
            if (_unload_native(root, iid, api_key=api_key, client=http)
                    or _unload_cli([iid])):
                result.evicted.append(iid)
        if loaded:
            after = list_loaded_instances(root, api_key=api_key, client=http)
            if after is None:
                result.error = "LM Studio stopped answering after unload"
            else:
                result.still_resident = after
                result.evicted = [i for i in result.evicted if i not in after]
        return result
    finally:
        if own:
            http.close()
