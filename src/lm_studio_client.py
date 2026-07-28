"""
LM Studio client — synchronous, OpenAI-compatible local API.
LM Studio exposes: POST /v1/chat/completions

Debug output (--debug / -d flag):
  Set log level to DEBUG before creating the client; all request payloads and
  raw responses will be written via logging.debug() and visible in the console.
"""

import json
import logging
import re
import httpx
from rich.console import Console

console = Console()
log = logging.getLogger(__name__)


# C# numeric literals bleed into JSON from code-trained models: 10f, 1.5f, 100L,
# 2.0d, 5u, 3ul. Strip the suffix when the number sits in a JSON value/array slot
# (preceded by : [ or ,) and is followed by a closing delimiter — never inside a
# quoted string, where the number wouldn't be directly preceded by : [ or ,.
_NUM_SUFFIX_RE = re.compile(
    r'([:\[,]\s*-?\d+(?:\.\d+)?)[fFdDlLuU]+(?=\s*[,}\]])'
)
# Trailing comma before a closing brace/bracket — another common model slip.
_TRAILING_COMMA_RE = re.compile(r',(\s*[}\]])')


def _sanitize_json_scalars(text: str) -> str:
    """Repair common code-model JSON contaminants that break `json.loads`:
    C#-style numeric suffixes (e.g. `"interval": 10f` → `10`) and trailing commas.
    Surfaced by Llama 3.1 / deepseek-coder during model-gauntlet testing."""
    text = _NUM_SUFFIX_RE.sub(r'\1', text)
    text = _TRAILING_COMMA_RE.sub(r'\1', text)
    return text


def _rebalance(text: str, start: int) -> str:
    """Last-resort repair: drop surplus closers, close anything still open,
    then clean up the scalar/comma contaminants the edits can expose."""
    fixed = _drop_surplus_closers(text, start)
    fixed = _repair_truncated_json(fixed, 0)
    return _sanitize_json_scalars(fixed)


def _repair_truncated_json(text: str, start: int) -> str:
    """
    Close any unclosed brackets/braces left by a truncated LLM response.
    Walks the fragment tracking string/array/object state, then appends the
    missing closing delimiters in reverse order.
    """
    stack: list[str] = []
    in_string = False
    escape    = False

    for ch in text[start:]:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    # Truncation may leave a dangling open string — close it
    suffix = ('"' if in_string else "") + "".join(reversed(stack))
    return text[start:] + suffix


def _drop_surplus_closers(text: str, start: int) -> str:
    """Drop closing delimiters that have no matching opener.

    The mirror image of _repair_truncated_json: that one appends closers a
    truncated response never wrote, this one removes closers a response wrote
    too many of. Local models emit an extra array terminator after a nested
    object fairly readily — hit live on the "throws rings onto a box" prompt,
    where every list section came out as

        "vfx": [ { ... "parameters":{...}} ]  ],
                                             ^ no opener left to match

    The surplus `}` also makes extract_json's outermost-balanced-brace scan
    bottom out early, so the candidate it slices is short AND malformed; that
    surfaces as a plain "Expecting ',' delimiter", not as a truncation.

    Walks with the same string/escape handling as the truncation repair and
    copies every character through except closers that don't match the top of
    the open stack. Stops at the point the root object closes, so trailing
    prose after the JSON is dropped too. Well-formed input is returned
    unchanged (no closer is ever surplus in balanced JSON).
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escape = False

    for ch in text[start:]:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\" and in_string:
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            out.append(ch)
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                continue                      # surplus closer — drop it
            stack.pop()
            out.append(ch)
            if not stack:                     # root closed; ignore any tail
                break
            continue
        out.append(ch)

    return "".join(out)


class LMStudioClient:
    def __init__(self, base_url: str, model: str = "local-model",
                 temperature: float = 0.7, max_tokens: int = 8192,
                 timeout: float = 600.0, seed: int | None = None):
        self.base_url    = base_url.rstrip("/")
        self.model       = model
        self.temperature = temperature
        self.max_tokens  = max_tokens
        self.timeout     = timeout
        # Fixed sampler seed for reproducible generations. Both LM Studio and
        # Ollama's OpenAI-compatible endpoints honour "seed"; with it set, the
        # same (prompt, seed) yields the same output — the upstream half of the
        # pipeline's determinism story. None → server picks a random seed.
        self.seed        = seed

    def _build_payload(self, system: str, user: str,
                       temperature: float | None, thinking: bool,
                       seed: int | None) -> dict:
        """Assemble the chat-completions request body. Split out so the seed/
        temperature wiring is unit-testable without a live server."""
        user_msg = user if thinking else f"/no_think\n\n{user}"
        payload: dict = {
            "model":       self.model,
            "messages":    [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            # Qwen3 (and several other local models) return an empty response at
            # temperature=0.0.  Clamp to 0.05 as the effective greedy minimum.
            "temperature": max(0.05, temperature if temperature is not None else self.temperature),
            "max_tokens":  self.max_tokens,
        }
        eff_seed = seed if seed is not None else self.seed
        if eff_seed is not None:
            payload["seed"] = int(eff_seed)
        return payload

    def chat(self, system: str, user: str,
             temperature: float | None = None,
             thinking: bool = True,
             seed: int | None = None) -> str:
        """
        thinking=False → prepend /no_think to skip Qwen3 chain-of-thought.
        thinking=True  → let the model reason freely (story steps).
        seed           → per-call override of the client's reproducibility seed.
        """
        payload = self._build_payload(system, user, temperature, thinking, seed)
        user_msg = payload["messages"][1]["content"]
        log.debug(
            "→ LLM REQUEST  model=%s  temperature=%.2f  max_tokens=%d\n"
            "  [system] %s\n"
            "  [user]   %s",
            payload["model"],
            payload["temperature"],
            payload["max_tokens"],
            system[:300] + ("…" if len(system) > 300 else ""),
            user_msg[:300] + ("…" if len(user_msg) > 300 else ""),
        )

        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.base_url}/chat/completions", json=payload)
                r.raise_for_status()
        except httpx.TimeoutException as e:
            # Raised as TimeoutError (not ValueError) so callers don't burn another
            # full timeout retrying. Reasoning models / CPU-offloaded models hit this.
            raise TimeoutError(
                f"LM Studio did not respond within {self.timeout:.0f}s for model "
                f"'{self.model}'. Reasoning models, or models partly offloaded to "
                f"CPU/RAM, can exceed this — raise lm_studio.timeout in config.yaml, "
                f"fit the model fully in VRAM, or pick a smaller / non-reasoning model."
            ) from e

        message = r.json()["choices"][0]["message"]
        content = (message.get("content") or "")
        log.debug(
            "← LLM RESPONSE  %d chars\n%s",
            len(content),
            content[:600] + ("…" if len(content) > 600 else ""),
        )

        if not content.strip():
            # Reasoning models (Phi-4-reasoning, etc.) stream chain-of-thought into a
            # separate `reasoning_content` field and only fill `content` with the final
            # answer. An empty content WITH reasoning means the token budget was spent
            # before the answer arrived.
            if (message.get("reasoning_content") or "").strip():
                raise ValueError(
                    "The model spent its entire token budget reasoning and never "
                    "emitted a final answer. Raise lm_studio.max_tokens in config.yaml, "
                    "or use a non-reasoning model for the schema step."
                )
            raise ValueError(
                "LM Studio returned an empty response. "
                "Check that a model is loaded and the server is running."
            )
        return content

    # ── JSON helpers ──────────────────────────────────────────────────────────

    def extract_json(self, text: str) -> dict:
        """Strip model artifacts and parse the first balanced JSON object."""
        # Qwen3 reasoning blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        # Markdown fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip()

        start = text.find("{")
        if start == -1:
            raise ValueError(f"No JSON object in LLM response:\n{text[:400]}")

        # Find the outermost balanced closing brace
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            # Response was truncated mid-JSON — try to repair it
            repaired = _sanitize_json_scalars(_repair_truncated_json(text, start))
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
            try:
                return json.loads(_rebalance(text, start))
            except json.JSONDecodeError:
                raise ValueError(
                    f"JSON truncated and could not be repaired.\n"
                    f"Increase max_tokens in config.yaml.\n"
                    f"Tail: ...{text[-200:]}"
                )

        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Retry after repairing code-model contaminants (10f, trailing commas).
            try:
                return json.loads(_sanitize_json_scalars(candidate))
            except json.JSONDecodeError as exc:
                # A surplus closer ends the balanced-brace scan early, so
                # `candidate` is both short and malformed — rebalance the FULL
                # text rather than the slice.
                try:
                    return json.loads(_rebalance(text, start))
                except json.JSONDecodeError:
                    pass
                raise ValueError(
                    f"JSON parse error: {exc}\nRaw:\n{candidate[:400]}"
                ) from exc
