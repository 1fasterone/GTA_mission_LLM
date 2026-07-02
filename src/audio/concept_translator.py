"""
Concept-to-cue prompt translator for Stable Audio 3.

After the LLM writes the Step 2 concept brief, this module re-uses the same
loaded model to enrich each AudioCueSpec with a `gen_prompt` tuned for
Stable Audio 3's text conditioning.

Called while LM Studio is still resident in VRAM (before the unload step),
so the project-aware model can map high-level concept prose — e.g. "spatial
audio middleware so players locate threats by sound before visual confirmation"
— into per-cue prompts like:
  "Heavy armoured boots on wet concrete, slow and deliberate, stereo-panned
   left, reverberant corridor, tension-building, 1.5s, 44100hz"

Only cues with an empty gen_prompt are filled; the LLM's output merges with
any the schema step already set. Degrades silently on any failure — content.py
already uses `cue.gen_prompt or cue.descriptor` as the fallback.
"""

from __future__ import annotations

import json
import re


_SYSTEM = """\
You are a professional game audio designer specialising in Stable Audio 3 generation prompts.
Given a game concept and a list of sound cues, write one evocative Stable Audio 3 prompt for
each cue. Rules:
  - Draw atmosphere, setting, emotional tone, and sonic texture from the CONCEPT, not just the
    cue's literal descriptor. A "footstep" in a horror concept is different from one in a racer.
  - Describe: timbre, space/reverb, transient character, emotional register, stereo field hint.
  - End every prompt with the target duration ("X.Xs") and "44100hz".
  - One prompt per cue. One sentence. No lists, no explanation, no prose around the JSON.
Output ONLY a valid JSON object: {"cue_id": "prompt text", ...}
"""

_USER_TMPL = """\
GAME CONCEPT:
{concept}

CUES (id | category | descriptor | duration_s):
{cue_table}

Fill every cue. For generic descriptors, let the concept atmosphere guide you.
Output JSON only.
"""


def _build_table(cues: list) -> str:
    rows = []
    for c in cues:
        g = (lambda k, d: c.get(k, d)) if isinstance(c, dict) \
            else (lambda k, d: getattr(c, k, d))
        rows.append(
            f"  {g('id', '?')} | {g('category', 'sfx')} | "
            f"{g('descriptor', '')} | {g('duration_s', 0.5):.1f}s"
        )
    return "\n".join(rows)


def translate_concept(concept_text: str, cues: list, llm, seed: int = 0) -> int:
    """Enrich each cue's gen_prompt in-place using the project concept.

    Returns the number of cues updated. Silently degrades on any failure so
    the audio pipeline always continues; content.py falls back to descriptor.
    """
    if not concept_text or not cues:
        return 0

    try:
        raw = llm.chat(
            system=_SYSTEM,
            user=_USER_TMPL.format(
                concept=concept_text.strip(),
                cue_table=_build_table(cues),
            ),
            temperature=0.75,
            thinking=False,
            seed=seed or None,
        )
    except Exception:  # noqa: BLE001
        return 0

    try:
        # Strip think-blocks and markdown fences before parsing
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start = raw.find("{")
        end   = raw.rfind("}")
        if start == -1 or end == -1:
            return 0
        mapping: dict[str, str] = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return 0

    updated = 0
    for c in cues:
        is_dict = isinstance(c, dict)
        cid = c.get("id", "") if is_dict else getattr(c, "id", "")
        if cid not in mapping:
            continue
        prompt = str(mapping[cid]).strip()
        if not prompt:
            continue
        # Only fill if gen_prompt is currently empty
        current = c.get("gen_prompt", "") if is_dict else getattr(c, "gen_prompt", "")
        if current:
            continue
        try:
            if is_dict:
                c["gen_prompt"] = prompt
            else:
                c.gen_prompt = prompt
            updated += 1
        except Exception:  # noqa: BLE001
            pass

    return updated
