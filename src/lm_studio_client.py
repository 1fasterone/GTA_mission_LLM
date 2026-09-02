"""
LM Studio client — synchronous, OpenAI-compatible chat client.
LM Studio exposes: POST /v1/chat/completions

Also serves Ollama and OpenRouter (src/backends.py's active_chat_cfg/
make_chat_client select between all three) — all three speak the same
OpenAI-compatible chat-completions contract, differing only in base_url,
model, and (OpenRouter only) an API key. LM Studio/Ollama are local servers
needing no auth; `api_key`, when set, adds the Authorization: Bearer header
OpenRouter requires. The class keeps its original name since every existing
call site and test imports it as LMStudioClient — renaming for the added
generality would be a repo-wide rename with no behavioral upside.

Debug output (--debug / -d flag):
  Set log level to DEBUG before creating the client; all request payloads and
  raw responses will be written via logging.debug() and visible in the console.
"""

import json
import logging
import re
import time
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


def _loads(text: str):
    """`json.loads`, falling back to the non-strict parser.

    A model asked to put C# inside a JSON string writes the code the way it
    writes code: with real newlines and tabs in it, rather than `\\n`. Strict
    JSON forbids a literal control character inside a string, so ONE unescaped
    newline in one method body rejects an otherwise perfect 90KB schema fill
    with "Invalid control character at: line 280 column 118" — which the caller
    can only report as a model failure and re-roll. Hit live on a Chromabreak
    schema fill.

    `strict=False` accepts those characters as themselves, which is exactly
    what they mean here: the newline IS part of the authored code. Tried second,
    never first, so genuinely malformed JSON still fails the same way it always
    did rather than being silently reinterpreted.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)


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


# Thinking toggle. Measured against LM Studio + qwen3.5-9b (same prompt, warm
# model, 4000 max_tokens):
#
#   reasoning_effort="none"   3.5s    27 completion tokens,    0 reasoning chars
#   field omitted             3.3s    27 completion tokens,    0 reasoning chars
#   reasoning_effort="high"  40.1s   993 completion tokens, 3668 reasoning chars
#
# So `reasoning_effort` is the ONLY control the server honours — the
# `enable_thinking` / `chat_template_kwargs` booleans and the `/no_think`
# prefix below are all silently IGNORED — and the server's DEFAULT is
# reasoning off. That default is why `thinking=True` was quietly a no-op: the
# concept step asked to reason and never did. Send the field explicitly in
# both directions rather than inheriting a default that can flip with a
# server, model or config change, in either direction.
_REASONING_OFF = "none"
_REASONING_ON  = "high"

# Models already caught ignoring reasoning_effort=none — warn once each, not
# once per call.
_REASONING_IGNORED: set[str] = set()


class LMStudioClient:
    def __init__(self, base_url: str, model: str = "local-model",
                 temperature: float = 0.7, max_tokens: int = 8192,
                 timeout: float = 600.0, seed: int | None = None,
                 reasoning_effort: str | None = None,
                 api_key: str | None = None):
        self.base_url    = base_url.rstrip("/")
        self.model       = model
        self.temperature = temperature
        self.max_tokens  = max_tokens
        self.timeout     = timeout
        # Effort used for the steps that DO reason (the concept step). Config
        # knob `lm_studio.reasoning_effort` — set "none" to run a whole build
        # without reasoning, or "low"/"minimal" to trade depth for speed.
        self.reasoning_effort = reasoning_effort or _REASONING_ON
        # Fixed sampler seed for reproducible generations. Both LM Studio and
        # Ollama's OpenAI-compatible endpoints honour "seed"; with it set, the
        # same (prompt, seed) yields the same output — the upstream half of the
        # pipeline's determinism story. None → server picks a random seed.
        self.seed        = seed
        # Only OpenRouter needs this — a local LM Studio/Ollama server takes
        # no auth. None -> no Authorization header sent (unchanged behaviour
        # for the two local backends).
        self.api_key     = api_key
        # Set by chat() after every call from the response's own
        # `finish_reason` — "length" means the SERVER confirms it cut the
        # reply off at max_tokens, as opposed to extract_json() having to
        # guess that from a JSONDecodeError after the fact. One client serves
        # one sequential pipeline run (no concurrent calls), so instance state
        # here is safe the same way _REASONING_IGNORED's module state is.
        self.last_finish_reason: str | None = None

    def _build_payload(self, system: str, user: str,
                       temperature: float | None, thinking: bool,
                       seed: int | None, max_tokens: int | None = None,
                       json_mode: bool = False,
                       json_schema: dict | None = None) -> dict:
        """Assemble the chat-completions request body. Split out so the seed/
        temperature/reasoning wiring is unit-testable without a live server.

        json_mode adds response_format={"type": "json_object"} — a request
        that the server enforce syntactically valid JSON at generation time,
        instead of this module's extract_json()/repair passes catching a
        malformed reply after the fact. Off by default: NOT every call site
        wants JSON (concept generation expects prose), and support varies by
        backend/model — see chat()'s fallback for what happens when a server
        rejects the parameter.

        json_schema, when given, takes precedence over json_mode and instead
        sends response_format={"type": "json_schema", "json_schema": <this
        dict>} — grammar-constrained decoding to an actual shape, not just
        valid syntax. Callers build the dict with
        unity_schema_jsonschema.response_format_schema()["json_schema"];
        this method stays schema-agnostic and just forwards it."""
        # Kept as a fallback for Qwen3 builds served by something that does not
        # implement `reasoning_effort`; the field below is what actually works
        # on LM Studio. Harmless where it is ignored.
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
            # Per-call ceiling, defaulting to the client-wide budget. A step
            # whose whole answer is a handful of small fragments has no use for
            # the schema step's 50k budget, and letting it inherit one is not
            # free: a model that runs away instead of stopping generates until
            # the CLIENT timeout (2600s) rather than until a sane cap, and a
            # caller that retries pays that in full, per attempt. Hit live —
            # one micro-code subsystem burned 1h54m as three silent 43-minute
            # timeouts before anything was logged.
            "max_tokens":  int(max_tokens or self.max_tokens),
            # Always explicit — see _REASONING_OFF/_REASONING_ON above.
            "reasoning_effort": (self.reasoning_effort if thinking
                                 else _REASONING_OFF),
        }
        eff_seed = seed if seed is not None else self.seed
        if eff_seed is not None:
            payload["seed"] = int(eff_seed)
        if json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    # Upstream conditions that are about the PROVIDER's moment, not about this
    # request — the same payload sent again a few seconds later routinely
    # succeeds. OpenRouter multiplexes each model across several upstream
    # providers, so a 402 "Insufficient balance" or a 404 with an empty body is
    # one provider having a bad minute, not a permanent verdict on the call.
    #
    # Live, on one Chromabreak build: a ~40-minute --compose run reached Step 3
    # and died on a single HTTP 402 from GMICloud. Every deterministic step
    # after it, the whole micro-code phase, and the asset resolution that had
    # already been paid for were all thrown away for one transient upstream
    # blip. Retrying three times over ~25s is cheap; re-running the pipeline is
    # not. A genuinely exhausted quota still fails — it just fails four times
    # first, and the caller still gets the provider's real message.
    _RETRY_STATUS = frozenset({402, 408, 409, 425, 429, 500, 502, 503, 504})
    _RETRY_BACKOFF = (5.0, 10.0, 15.0)

    def _post_with_retry(self, payload: dict, headers: dict) -> httpx.Response:
        """POST the completion, retrying only provider-transient statuses."""
        for attempt in range(len(self._RETRY_BACKOFF) + 1):
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(f"{self.base_url}/chat/completions",
                                json=payload, headers=headers)
            try:
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as e:
                if (e.response.status_code not in self._RETRY_STATUS
                        or attempt >= len(self._RETRY_BACKOFF)):
                    raise
                delay = self._RETRY_BACKOFF[attempt]
                log.warning(
                    "%s returned HTTP %d for '%s' — provider-transient, "
                    "retrying in %.0fs (attempt %d/%d): %s",
                    self.base_url, e.response.status_code, self.model, delay,
                    attempt + 1, len(self._RETRY_BACKOFF),
                    e.response.text[:200],
                )
                time.sleep(delay)
        # Unreachable: the final iteration's `attempt >= len(_RETRY_BACKOFF)`
        # re-raises instead of sleeping, so the loop always returns or raises.
        raise AssertionError("retry loop fell through without returning")

    def _post_translated(self, payload: dict, headers: dict) -> httpx.Response:
        """POST + translate httpx exceptions to this client's public exception
        contract (TimeoutError / RuntimeError). Shared by chat()'s primary
        attempt and its json_mode fallback retry, so a failure on either one
        surfaces identically to a caller."""
        try:
            return self._post_with_retry(payload, headers)
        except httpx.TimeoutException as e:
            # Raised as TimeoutError (not ValueError) so callers don't burn another
            # full timeout retrying. Reasoning models / CPU-offloaded models hit this.
            raise TimeoutError(
                f"{self.base_url} did not respond within {self.timeout:.0f}s for model "
                f"'{self.model}'. Reasoning models, or models partly offloaded to "
                f"CPU/RAM, can exceed this — raise lm_studio.timeout in config.yaml, "
                f"fit the model fully in VRAM, or pick a smaller / non-reasoning model. "
                f"A model that RUNS AWAY (never emitting a stop token) also lands "
                f"here, having generated up to max_tokens={payload['max_tokens']} "
                f"first; lower the caller's per-call max_tokens to fail fast."
            ) from e
        except httpx.HTTPStatusError as e:
            # Surfaces the provider's own structured error (rate limits, quota,
            # bad model id) instead of a bare "Client error '429 ...'" — this is
            # the only backend of the three where the request can fail for
            # reasons outside the caller's control (upstream provider capacity),
            # so the reason needs to reach the console, not just the status code.
            raise RuntimeError(
                f"{self.base_url} returned HTTP {e.response.status_code} for model "
                f"'{self.model}': {e.response.text[:500]}"
            ) from e

    def chat(self, system: str, user: str,
             temperature: float | None = None,
             thinking: bool = True,
             seed: int | None = None,
             max_tokens: int | None = None,
             json_mode: bool = False,
             json_schema: dict | None = None) -> str:
        """
        thinking=False → reasoning_effort="none": no chain-of-thought. Use for
                         every mechanical step (schema fill, cue resolution,
                         query translation, micro C# bodies) — they gain
                         nothing from reasoning and pay its full latency.
        thinking=True  → reasoning_effort=self.reasoning_effort ("high" unless
                         config says otherwise) — the story/concept steps.
        seed           → per-call override of the client's reproducibility seed.
        max_tokens     → per-call completion ceiling. Omit to inherit the
                         client-wide budget (config lm_studio.max_tokens).
        json_mode      → ask the server to enforce valid JSON syntax
                         (response_format={"type":"json_object"}). Pass this
                         only from a call site that actually wants JSON back —
                         it is NOT a client-wide setting, because this same
                         client also serves prose calls (concept generation)
                         that must not have it. If the server/model rejects
                         the parameter (an HTTP error), the request is retried
                         ONCE without it, transparently — that retry does not
                         consume any of the caller's own retry budget.
        json_schema    → ask the server to constrain generation to an actual
                         SHAPE, not just valid syntax
                         (response_format={"type":"json_schema","json_schema":
                         this dict}). Takes precedence over json_mode when
                         both are given. Same rejection handling as json_mode:
                         one transparent retry without it.
        """
        payload = self._build_payload(system, user, temperature, thinking, seed,
                                      max_tokens, json_mode=json_mode,
                                      json_schema=json_schema)
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

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = self._post_with_retry(payload, headers)
        except httpx.HTTPStatusError as e:
            if not (json_mode or json_schema):
                raise RuntimeError(
                    f"{self.base_url} returned HTTP {e.response.status_code} for model "
                    f"'{self.model}': {e.response.text[:500]}"
                ) from e
            log.warning(
                "%s rejected response_format=%s for model '%s' (HTTP %d) "
                "— retrying once without it",
                self.base_url, "json_schema" if json_schema else "json_object",
                self.model, e.response.status_code,
            )
            payload = self._build_payload(system, user, temperature, thinking, seed,
                                          max_tokens, json_mode=False, json_schema=None)
            r = self._post_translated(payload, headers)
        except httpx.TimeoutException as e:
            raise TimeoutError(
                f"{self.base_url} did not respond within {self.timeout:.0f}s for model "
                f"'{self.model}'. Reasoning models, or models partly offloaded to "
                f"CPU/RAM, can exceed this — raise lm_studio.timeout in config.yaml, "
                f"fit the model fully in VRAM, or pick a smaller / non-reasoning model. "
                f"A model that RUNS AWAY (never emitting a stop token) also lands "
                f"here, having generated up to max_tokens={payload['max_tokens']} "
                f"first; lower the caller's per-call max_tokens to fail fast."
            ) from e

        body    = r.json()
        choice  = body["choices"][0]
        message = choice["message"]
        content = (message.get("content") or "")

        # "length" is the server's OWN confirmation the reply was cut off at
        # max_tokens — the direct signal for the "context window too small
        # for the reply" failure mode, as opposed to extract_json() inferring
        # truncation after the fact from a JSONDecodeError. Stashed on the
        # instance (not returned) so extract_json() can use it without a
        # signature change, and every existing call site stays untouched.
        self.last_finish_reason = choice.get("finish_reason")
        if self.last_finish_reason == "length":
            log.warning(
                "%s cut off generation at max_tokens=%d for model '%s' "
                "(finish_reason=length) — the reply may be truncated.",
                self.base_url, payload["max_tokens"], self.model,
            )

        # A model that cannot switch reasoning OFF is unusable for this
        # pipeline, and nothing in the API response says so directly — the
        # refusal is a server-side warning ("Reasoning setting 'off' cannot be
        # converted to any custom KVs") the client never sees. The usage block
        # does report it, though: a call sent with reasoning_effort="none" that
        # comes back having spent reasoning tokens is proof the setting was
        # ignored. Say so ONCE per model, loudly, naming the remedy.
        #
        # Hit live on qwopus3.5-9b-coder-exp: every mechanical call reasoned to
        # the context ceiling (~30,600 tokens, ~43 min each) and returned no
        # content, three attempts per subsystem, with nothing logged. The run
        # looked hung for two hours.
        if not thinking:
            spent = (((body.get("usage") or {})
                      .get("completion_tokens_details") or {})
                     .get("reasoning_tokens") or 0)
            if spent and self.model not in _REASONING_IGNORED:
                _REASONING_IGNORED.add(self.model)
                log.warning(
                    "Model '%s' IGNORED reasoning_effort=none and spent %d "
                    "reasoning tokens on a mechanical step. Every such step "
                    "will burn its whole budget this way. Pick a model whose "
                    "reasoning can be disabled (LM Studio logs 'Supported "
                    "settings' for each), or expect stalls and empty answers.",
                    self.model, spent,
                )
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

        # Find the outermost balanced closing brace.
        #
        # STRING-AWARE, because this pipeline's whole job is to have a model
        # emit C# inside JSON and C# is brace-dense. A scanner that counts
        # braces inside string literals closes the object on the first `}` of
        # a method body:
        #
        #   "body": "foreach (var b in _blocks) { ... } CheckLines();"
        #                                       ^ counted     ^ closes object
        #
        # Hit live: an 87,677-char schema fill was sliced to 4,031 chars and
        # reported as the MODEL's syntax error ("Expecting ',' delimiter"),
        # discarding a response that was fine up to that point. Both repair
        # helpers below (_repair_truncated_json, _drop_surplus_closers) have
        # tracked string/escape state all along — only this scan did not.
        depth, end = 0, -1
        in_string, escape = False, False
        for i, ch in enumerate(text[start:], start):
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
                return _loads(repaired)
            except json.JSONDecodeError:
                pass
            try:
                return _loads(_rebalance(text, start))
            except json.JSONDecodeError:
                if self.last_finish_reason == "length":
                    # Server-confirmed, not inferred: the response really was
                    # cut off at max_tokens, not just malformed on its own.
                    raise ValueError(
                        f"JSON truncated and could not be repaired.\n"
                        f"CONFIRMED: the server stopped generation at "
                        f"max_tokens (finish_reason=length). Raise "
                        f"lm_studio.max_tokens in config.yaml, or the "
                        f"server's own loaded context (LM Studio: check "
                        f"`lms ps`).\n"
                        f"Tail: ...{text[-200:]}"
                    )
                raise ValueError(
                    f"JSON truncated and could not be repaired.\n"
                    f"Increase max_tokens in config.yaml.\n"
                    f"Tail: ...{text[-200:]}"
                )

        candidate = text[start : end + 1]
        try:
            return _loads(candidate)
        except json.JSONDecodeError:
            # Retry after repairing code-model contaminants (10f, trailing commas).
            try:
                return _loads(_sanitize_json_scalars(candidate))
            except json.JSONDecodeError as exc:
                # A surplus closer ends the balanced-brace scan early, so
                # `candidate` is both short and malformed — rebalance the FULL
                # text rather than the slice.
                try:
                    return _loads(_rebalance(text, start))
                except json.JSONDecodeError:
                    pass
                raise ValueError(
                    f"JSON parse error: {exc}\nRaw:\n{candidate[:400]}"
                ) from exc
