"""Slices 1-2 of the image-led Visual Reasoning Compiler.

This module owns the boundary from a visual concept prompt to one canonical
ComfyUI screenshot and the subsequent image-only Qwen reasoning boundary.
It does not inspect gameplay, resolve assets, or create a Unity project. Later
slices consume the recorded board, inventory, Concept, and provenance
artifacts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


FORMAT = "visual-reasoning-compiler/vizconcept-board/v1"
IMPORT_FORMAT = "visual-reasoning-compiler/imported-board/v1"
REASONING_FORMAT = "visual-reasoning-compiler/image-reasoning/v1"
INVENTORY_FORMAT = "visual-reasoning-compiler/visual-inventory/v1"
DEFAULT_CHECKPOINT = "sd15ModelLab_realV001_1651730.safetensors"
DEFAULT_SEED = 978075547277410
DEFAULT_WIDTH = 768
DEFAULT_HEIGHT = 512
DEFAULT_STEPS = 20
DEFAULT_CFG = 8.0
DEFAULT_SAMPLER = "dpmpp_2m_sde_gpu"
DEFAULT_SCHEDULER = "karras"

# Do not suppress text or UI: a game screenshot may intentionally contain both,
# and Slice 2 must be able to reason over them.  These are only generic failure
# suppressors, not hidden gameplay instructions.
DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low resolution, duplicate objects, malformed geometry, "
    "cropped major objects, watermark, signature, gore, nsfw"
)

# Slice 2 deliberately has no prompt-file input.  These instructions are
# checked-in production policy: the board pixels are the only creative input
# sent to the Pipeline LLM.  The original vizconcept is preserved by Slice 1
# provenance, but it must not become hidden semantic context here.
IMAGE_ONLY_SYSTEM = """\
You are the image-reasoning stage of a deterministic game-content compiler.
You receive exactly one rendered game board image. Treat its visible pixels,
including legible UI text, as the only creative evidence. Do not infer or
recover an unavailable source prompt, backstory, asset library, filenames,
engine schema, or gameplay requirements. Distinguish what is visibly present
from what is a cautious game-design inference. Never choose a specific local
file, model ID, download URL, or Unity/GTA implementation.
"""

INVENTORY_USER = """\
Inspect the attached board image and return ONLY one JSON object with exactly
these keys:
{
  "scene_summary": "one concise description grounded in the image",
  "visible_elements": [
    {
      "label": "one literal visible noun; add a numeric suffix only to distinguish duplicate nouns",
      "category": "character | vehicle | prop | environment | landmark | ui | effect | other",
      "description": "what this visible element looks like",
      "model_hint": "noun-only visual asset phrase, or null when no 3D model is appropriate"
    }
  ],
  "sketchfab_queries": [
    {
      "label": "exact label from visible_elements",
      "query": "noun-only visual phrase suitable for a 3D model search"
    }
  ],
  "visible_text": ["legible text that is actually visible in the image"],
  "uncertainties": ["brief visual ambiguities, if any"]
}

List discrete, asset-worthy visible elements, not abstract game systems. For
every visible element, the label must be one literal visible noun (or the same
noun with a numeric suffix when duplicate instances need distinct labels).
Classify a physical object by what it is, not by a visual effect applied to it:
use "prop", "environment", or "landmark" for a visible lantern, lamp, torch,
sign, or gate even when it glows or emits light. Reserve "effect" for a
standalone VFX such as smoke, fire, sparks, or an explosion with no physical
object carrying it.
For card-like interfaces, use the pictured unit or object noun (for example,
"peasant", "knight", or "dragon"), never the card position, tier, rank, or
another numeric-only identifier.
Do not use adjectives, pronouns, articles, spatial/depth modifiers, gameplay
roles, or inferred names in labels. Examples include "aircraft", "missile",
"warship", "mountain", "ocean", "smoke", "explosion", and "watermark".
Keep visual detail in description. Keep model_hint and query noun-only too;
do not use style, era, color, distance, or combat adjectives in them. Do not
create a Sketchfab query for UI text, a glow, or an effect unless the image
visibly contains a separate 3D object behind it. A query must refer to one
listed visible element by its exact label, and every queried element must have
a non-empty model_hint. Omit the query when no honest model hint exists;
never emit an empty query string. Use [] when a list has no honest entries.
Output JSON only; do not add markdown or explanation.
"""

CONCEPT_USER = """\
Inspect the attached board image and write the contents of Concept.txt as
plain natural-language prose. The image is the sole creative source.

Before the prose, emit this exact plain-text reference block. Include every
concrete visible entity that the game concept uses, one short image-grounded
phrase per bullet. Do not list abstract mechanics or generic words such as
"player" or "game":

VISIBLE ENTITY REFERENCES:
- <short visible entity phrase>
END VISIBLE ENTITY REFERENCES

Describe a small playable game built from the visible characters, objects,
landmarks, environment, and readable UI. State the player's role, the core
interaction, progression or objective, and concise win/lose conditions. You
may infer mechanics from visible controls, composition, repetition, and UI,
but do not invent a new named entity, location, faction, prop, vehicle, or
system that has no visible support. If a detail is inferred rather than
shown, keep the inference modest and grounded in the image.

After the reference block, write 3-6 short paragraphs. Do not mention this
instruction, the board, the source prompt, visual reasoning, Qwen, Sketchfab,
model files, asset IDs, Unity, GTA, or any unavailable context. Do not output
JSON, code, or a markdown fenced block. Output only the Concept.txt text.
"""

INVENTORY_TEMPERATURE = 0.2
CONCEPT_TEMPERATURE = 0.55
INVENTORY_SEED = 731947
CONCEPT_SEED = 731948
INVENTORY_MAX_TOKENS = 3500
CONCEPT_MAX_TOKENS = 2200
INVENTORY_CATEGORIES = frozenset({
    "character", "vehicle", "prop", "environment", "landmark", "ui",
    "effect", "other",
})
CONCEPT_REFERENCE_HEADER = "VISIBLE ENTITY REFERENCES:"
CONCEPT_REFERENCE_END = "END VISIBLE ENTITY REFERENCES"
CONCEPT_NOUN_LIST_HEADER = "CANONICAL VISIBLE ENTITY LABELS:"


class VisualReasoningCompilerError(RuntimeError):
    """Raised when an image-led compiler contract cannot be completed."""


def build_vizconcept_prompt(source_text: str) -> str:
    """Wrap a visual concept without turning it into a semantic game brief."""
    text = source_text.strip()
    if not text:
        raise VisualReasoningCompilerError("vizconcept.txt is empty")
    return (
        "Create one readable game screenshot or game-concept frame. "
        "Treat the visual concept below as the authoritative art direction. "
        "Preserve its setting, style, composition, visible objects, and UI "
        "where requested. Give discrete objects clear silhouettes and enough "
        "separation for later visual inventory. Do not add unrelated objects "
        "or gameplay systems.\n\n"
        "VISUAL CONCEPT:\n"
        + text
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def _encode_image_data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Extract one JSON object without repairing its semantic contents.

    Some OpenAI-compatible local servers ignore ``response_format`` and Qwen
    still wraps an otherwise valid JSON response in one Markdown code fence.
    Accept that exact envelope, but continue rejecting any other text around
    the object and leave all field validation to ``validate_visual_inventory``.
    """
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise VisualReasoningCompilerError(
            "visual inventory response contained no JSON object"
        )
    try:
        value, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise VisualReasoningCompilerError(
            f"visual inventory response is not valid JSON: {exc}"
        ) from exc
    if text[start + end:].strip():
        raise VisualReasoningCompilerError(
            "visual inventory response contains text outside the JSON object"
        )
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(
            "visual inventory response must be a JSON object"
        )
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if extra:
            detail.append(f"extra={extra}")
        raise VisualReasoningCompilerError(
            f"{label} has the wrong fields ({', '.join(detail)})"
        )


def _require_string(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise VisualReasoningCompilerError(f"{label} must be a non-empty string")
    return value.strip() if not allow_empty else value


def extract_visible_entity_references(concept: str) -> tuple[list[str], str]:
    """Parse the deterministic entity-reference block from Concept.txt."""
    lines = concept.replace("\r\n", "\n").split("\n")
    headers = [
        index for index, line in enumerate(lines)
        if line.strip() == CONCEPT_REFERENCE_HEADER
    ]
    endings = [
        index for index, line in enumerate(lines)
        if line.strip() == CONCEPT_REFERENCE_END
    ]
    if len(headers) != 1 or len(endings) != 1 or endings[0] <= headers[0]:
        raise VisualReasoningCompilerError(
            "Concept.txt must contain one ordered VISIBLE ENTITY REFERENCES block"
        )
    header, end = headers[0], endings[0]
    references: list[str] = []
    for index, line in enumerate(lines[header + 1:end], start=header + 2):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- "):
            raise VisualReasoningCompilerError(
                f"Concept.txt entity reference line {index} must start with '- '")
        reference = _require_string(
            stripped[2:], label=f"Concept.txt entity reference line {index}"
        )
        if reference in references:
            raise VisualReasoningCompilerError(
                f"Concept.txt repeats entity reference {reference!r}"
            )
        references.append(reference)
    if not references:
        raise VisualReasoningCompilerError(
            "Concept.txt entity reference block is empty"
        )
    prose = "\n".join(lines[end + 1:]).strip()
    if not prose:
        raise VisualReasoningCompilerError(
            "Concept.txt has no prose after the entity reference block"
        )
    return references, prose


def build_concept_user(inventory: dict[str, Any]) -> tuple[str, list[str]]:
    """Constrain Concept entity references to the preceding image inventory.

    The inventory is still image-derived Pipeline LLM output, not engineer or
    catalog context. Passing its exact labels to the second call prevents the
    two sequential image-only calls from inventing different names for the
    same visible object.
    """
    elements = inventory.get("visible_elements")
    if not isinstance(elements, list) or not elements:
        raise VisualReasoningCompilerError(
            "cannot build Concept prompt without visible inventory elements"
        )
    labels: list[str] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            raise VisualReasoningCompilerError(
                f"visual inventory element {index} is not an object"
            )
        label = _require_string(
            element.get("label"), label=f"visible_elements[{index}].label"
        )
        labels.append(label)
    noun_list = "\n".join(f"- {label}" for label in labels)
    prompt = (
        CONCEPT_USER
        + "\n\n"
        + CONCEPT_NOUN_LIST_HEADER
        + "\n"
        + noun_list
        + "\n\n"
        + "Reference-block rules: copy every label above exactly once, in the "
        + "same order, and do not add, remove, rename, or paraphrase a label. "
        + "Use these labels as the only named visible entities in the concept."
    )
    return prompt, labels


def _require_inventory_reference_labels(
    concept: str, labels: list[str]
) -> None:
    """Reject a Concept that did not copy the canonical noun list."""
    references, _ = extract_visible_entity_references(concept)
    expected = set(labels)
    actual = set(references)
    if actual != expected or len(references) != len(labels):
        missing = [label for label in labels if label not in actual]
        extra = [reference for reference in references if reference not in expected]
        detail: list[str] = []
        if missing:
            detail.append(f"missing={missing}")
        if extra:
            detail.append(f"extra={extra}")
        raise VisualReasoningCompilerError(
            "image-derived Concept.txt must copy the canonical inventory "
            "labels exactly once (" + ", ".join(detail) + ")"
        )


def validate_visual_inventory(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the image-derived inventory contract.

    This is intentionally a strict boundary.  It does not infer missing
    fields, rename labels, or turn malformed LLM output into a partial asset
    list; later reconciliation needs to know exactly what the model claimed.
    """
    payload = _extract_json_object(raw) if isinstance(raw, str) else raw
    if payload.get("format") == INVENTORY_FORMAT:
        payload = {key: value for key, value in payload.items() if key != "format"}
    elif "format" in payload:
        raise VisualReasoningCompilerError(
            f"visual inventory format must be {INVENTORY_FORMAT!r}"
        )
    _require_exact_keys(
        payload,
        {"scene_summary", "visible_elements", "sketchfab_queries",
         "visible_text", "uncertainties"},
        label="visual inventory",
    )
    scene_summary = _require_string(payload["scene_summary"], label="scene_summary")

    elements_raw = payload["visible_elements"]
    if not isinstance(elements_raw, list) or not elements_raw:
        raise VisualReasoningCompilerError(
            "visual inventory must contain at least one visible element"
        )
    if len(elements_raw) > 64:
        raise VisualReasoningCompilerError("visual inventory has more than 64 elements")
    elements: list[dict[str, Any]] = []
    labels: set[str] = set()
    for index, item in enumerate(elements_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(
                f"visible_elements[{index}] must be an object"
            )
        _require_exact_keys(
            item, {"label", "category", "description", "model_hint"},
            label=f"visible_elements[{index}]",
        )
        label = _require_string(item["label"], label=f"visible_elements[{index}].label")
        if label.isdecimal():
            raise VisualReasoningCompilerError(
                f"visible_elements[{index}].label must be a visible noun, not a numeric-only identifier"
            )
        if label in labels:
            raise VisualReasoningCompilerError(
                f"visual inventory repeats element label {label!r}"
            )
        labels.add(label)
        category = _require_string(
            item["category"], label=f"visible_elements[{index}].category"
        )
        if category not in INVENTORY_CATEGORIES:
            raise VisualReasoningCompilerError(
                f"visible_elements[{index}].category {category!r} is unsupported"
            )
        description = _require_string(
            item["description"], label=f"visible_elements[{index}].description"
        )
        model_hint = item["model_hint"]
        if model_hint is not None:
            model_hint = _require_string(
                model_hint, label=f"visible_elements[{index}].model_hint"
            )
        elements.append({
            "label": label,
            "category": category,
            "description": description,
            "model_hint": model_hint,
        })
    elements_by_label = {str(element["label"]): element for element in elements}

    queries_raw = payload["sketchfab_queries"]
    if not isinstance(queries_raw, list):
        raise VisualReasoningCompilerError("sketchfab_queries must be a list")
    if len(queries_raw) > 64:
        raise VisualReasoningCompilerError("visual inventory has more than 64 queries")
    queries: list[dict[str, str]] = []
    query_labels: set[str] = set()
    for index, item in enumerate(queries_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(
                f"sketchfab_queries[{index}] must be an object"
            )
        _require_exact_keys(
            item, {"label", "query"}, label=f"sketchfab_queries[{index}]"
        )
        label = _require_string(
            item["label"], label=f"sketchfab_queries[{index}].label"
        )
        if label not in labels:
            raise VisualReasoningCompilerError(
                f"Sketchfab query refers to unknown visible element {label!r}"
            )
        if label in query_labels:
            raise VisualReasoningCompilerError(
                f"visual inventory repeats Sketchfab query for {label!r}"
            )
        if elements_by_label[label]["model_hint"] is None:
            raise VisualReasoningCompilerError(
                f"Sketchfab query target {label!r} has no model hint"
            )
        query_labels.add(label)
        query = _require_string(
            item["query"], label=f"sketchfab_queries[{index}].query"
        )
        queries.append({"label": label, "query": query})

    def string_list(value: Any, *, label: str) -> list[str]:
        if not isinstance(value, list):
            raise VisualReasoningCompilerError(f"{label} must be a list")
        if len(value) > 64:
            raise VisualReasoningCompilerError(f"{label} has more than 64 entries")
        return [
            _require_string(item, label=f"{label}[{index}]")
            for index, item in enumerate(value)
        ]

    return {
        "format": INVENTORY_FORMAT,
        "scene_summary": scene_summary,
        "visible_elements": elements,
        "sketchfab_queries": queries,
        "visible_text": string_list(payload["visible_text"], label="visible_text"),
        "uncertainties": string_list(
            payload["uncertainties"], label="uncertainties"
        ),
    }


def _clean_concept_response(raw: str) -> str:
    """Remove a hidden reasoning block while rejecting format drift."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if not text:
        raise VisualReasoningCompilerError("image-derived Concept.txt is empty")
    if "```" in text or text.startswith("{") or text.startswith("["):
        raise VisualReasoningCompilerError(
            "image-derived Concept.txt must be plain prose, not code or JSON"
        )
    forbidden = ("vizconcept.txt", "sketchfab", "qwen", "board.png")
    lowered = text.lower()
    found = [term for term in forbidden if term in lowered]
    if found:
        raise VisualReasoningCompilerError(
            "image-derived Concept.txt mentions forbidden pipeline context: "
            + ", ".join(found)
        )
    extract_visible_entity_references(text)
    if len(text) < 120:
        raise VisualReasoningCompilerError(
            "image-derived Concept.txt is too short to describe a playable game"
        )
    return text.replace("\r\n", "\n")


def _board_manifest_record(board_manifest: Path | None, board_hash: str) -> dict[str, str] | None:
    if board_manifest is None:
        return None
    if not board_manifest.is_file():
        raise VisualReasoningCompilerError(
            f"board manifest not found: {board_manifest}"
        )
    raw = board_manifest.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReasoningCompilerError(
            f"board manifest is not valid UTF-8 JSON: {board_manifest}"
        ) from exc
    if not isinstance(payload, dict):
        raise VisualReasoningCompilerError(
            f"board manifest must contain a JSON object: {board_manifest}"
        )
    board_record = payload.get("board")
    if not isinstance(board_record, dict):
        raise VisualReasoningCompilerError(
            f"board manifest has no board hash record: {board_manifest}"
        )
    recorded_hash = board_record.get("sha256")
    if recorded_hash != board_hash:
        raise VisualReasoningCompilerError(
            "board manifest hash does not match the supplied board image"
        )
    return {
        "path": str(board_manifest),
        "sha256": sha256_bytes(raw),
        "board_sha256": board_hash,
    }


def reason_over_board(*, board_image: Path, inventory_output: Path,
                      concept_output: Path, manifest: Path,
                      llm: Any, board_manifest: Path | None = None,
                      context_output: Path | None = None) -> Path:
    """Run Slice 2 using only the board image and fixed instructions.

    The two calls are sequential so one 9B vision model can remain loaded on
    an 8 GB card.  Nothing from the Slice-1 source prompt is read or sent to
    the model; an optional board manifest is checked only for hash agreement
    and linked by metadata.
    """
    if not board_image.is_file():
        raise VisualReasoningCompilerError(f"board image not found: {board_image}")
    image_bytes = board_image.read_bytes()
    if not _looks_like_png(image_bytes):
        raise VisualReasoningCompilerError("board image must be a valid-looking PNG")

    raw_paths = (
        inventory_output.with_suffix(".raw.txt"),
        concept_output.with_suffix(".raw.txt"),
    )
    requested = [inventory_output, concept_output, manifest, *raw_paths]
    if context_output is not None:
        requested.append(context_output)
    resolved: list[Path] = []
    for path in requested:
        path_resolved = path.resolve()
        if path_resolved in resolved:
            raise VisualReasoningCompilerError(
                f"Slice-2 output paths collide: {path}"
            )
        resolved.append(path_resolved)
        if path.exists():
            raise VisualReasoningCompilerError(
                f"refusing to overwrite visual-reasoning artifact: {path}"
            )

    board_hash = sha256_bytes(image_bytes)
    source_manifest = _board_manifest_record(board_manifest, board_hash)
    image_uri = _encode_image_data_uri(image_bytes)

    inventory_raw = llm.chat(
        system=IMAGE_ONLY_SYSTEM,
        user=INVENTORY_USER,
        images=[image_uri],
        temperature=INVENTORY_TEMPERATURE,
        thinking=False,
        seed=INVENTORY_SEED,
        max_tokens=INVENTORY_MAX_TOKENS,
        json_mode=True,
    )
    inventory = validate_visual_inventory(inventory_raw)
    concept_user, inventory_labels = build_concept_user(inventory)

    concept_raw = llm.chat(
        system=IMAGE_ONLY_SYSTEM,
        user=concept_user,
        images=[image_uri],
        temperature=CONCEPT_TEMPERATURE,
        thinking=False,
        seed=CONCEPT_SEED,
        max_tokens=CONCEPT_MAX_TOKENS,
    )
    concept = _clean_concept_response(concept_raw)
    _require_inventory_reference_labels(concept, inventory_labels)

    context_bytes: bytes | None = None
    if context_output is not None:
        from .unity_visual_context import render_image_context

        context_bytes = render_image_context(
            board_image, inventory, concept
        ).encode("utf-8")

    inventory_bytes = (
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    concept_bytes = (concept + "\n").encode("utf-8")
    inventory_raw_bytes = inventory_raw.encode("utf-8")
    concept_raw_bytes = concept_raw.encode("utf-8")
    model = str(getattr(llm, "model", "unknown"))
    request = {
        "board_sha256": board_hash,
        "model": model,
        "inventory": {
            "system": IMAGE_ONLY_SYSTEM,
            "user": INVENTORY_USER,
            "temperature": INVENTORY_TEMPERATURE,
            "thinking": False,
            "seed": INVENTORY_SEED,
            "max_tokens": INVENTORY_MAX_TOKENS,
        },
        "concept": {
            "system": IMAGE_ONLY_SYSTEM,
            "user": concept_user,
            "inventory_labels": inventory_labels,
            "temperature": CONCEPT_TEMPERATURE,
            "thinking": False,
            "seed": CONCEPT_SEED,
            "max_tokens": CONCEPT_MAX_TOKENS,
        },
    }
    manifest_data: dict[str, Any] = {
        "format": REASONING_FORMAT,
        "stage": "2_image_reasoning",
        "authority": {
            "input": "board.png",
            "board_role": "visual_intermediate_representation",
            "original_vizconcept_sent_to_llm": False,
            "semantic_context_sent_to_llm": True,
            "semantic_context_source": "validated_image_inventory_labels",
            "external_semantic_context_sent_to_llm": False,
            "llm_called_here": True,
        },
        "board": {
            "path": str(board_image),
            "mime_type": "image/png",
            "sha256": board_hash,
        },
        "board_manifest": source_manifest,
        "llm": {
            "model": model,
            "inventory": {
                "raw_response_sha256": sha256_bytes(inventory_raw_bytes),
                "settings": request["inventory"],
            },
            "concept": {
                "raw_response_sha256": sha256_bytes(concept_raw_bytes),
                "settings": request["concept"],
            },
        },
        "request_fingerprint": _canonical_hash(request),
        "outputs": {
            "visual_inventory": {
                "path": str(inventory_output),
                "sha256": sha256_bytes(inventory_bytes),
                "raw_response_path": str(raw_paths[0]),
            },
            "concept": {
                "path": str(concept_output),
                "sha256": sha256_bytes(concept_bytes),
                "raw_response_path": str(raw_paths[1]),
            },
        },
    }
    if context_output is not None and context_bytes is not None:
        manifest_data["outputs"]["context"] = {
            "path": str(context_output),
            "sha256": sha256_bytes(context_bytes),
        }

    output_paths = [inventory_output, concept_output, *raw_paths, manifest]
    if context_output is not None:
        output_paths.append(context_output)
    for path in output_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    inventory_output.write_bytes(inventory_bytes)
    concept_output.write_bytes(concept_bytes)
    raw_paths[0].write_bytes(inventory_raw_bytes)
    raw_paths[1].write_bytes(concept_raw_bytes)
    if context_output is not None and context_bytes is not None:
        context_output.write_bytes(context_bytes)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _looks_like_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 32


def build_workflow(*, checkpoint: str, positive: str, negative: str,
                   seed: int, width: int, height: int, steps: int, cfg: float,
                   sampler: str, scheduler: str,
                   filename_prefix: str = "visual_reasoning_board") -> dict:
    """Build the exact API-format graph recorded in the run manifest."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(0, seed), "steps": steps, "cfg": cfg,
            "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
    }


def import_existing_board(*, board_image: Path, output: Path, manifest: Path,
                          vizconcept_file: Path | None = None) -> Path:
    """Import a known-good render without pretending to regenerate it.

    This is the hardware-aware companion to the Comfy generation boundary.
    The imported PNG remains the visual intermediate representation; its
    original generation settings are unknown unless the caller supplies a
    separate historical manifest.
    """
    if not board_image.is_file():
        raise VisualReasoningCompilerError(
            f"board image not found: {board_image}"
        )
    if output.exists() or manifest.exists():
        raise VisualReasoningCompilerError(
            "refusing to overwrite visual-reasoning artifact: "
            f"{output if output.exists() else manifest}"
        )
    image_bytes = board_image.read_bytes()
    if not _looks_like_png(image_bytes):
        raise VisualReasoningCompilerError(
            "board image must be a valid-looking PNG"
        )

    prompt_record: dict[str, Any] | None = None
    if vizconcept_file is not None:
        if not vizconcept_file.is_file():
            raise VisualReasoningCompilerError(
                f"vizconcept file not found: {vizconcept_file}"
            )
        prompt_bytes = vizconcept_file.read_bytes()
        try:
            prompt_text = prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VisualReasoningCompilerError(
                f"vizconcept file must be UTF-8: {vizconcept_file}"
            ) from exc
        if not prompt_text.strip():
            raise VisualReasoningCompilerError("vizconcept.txt is empty")
        prompt_record = {
            "path": str(vizconcept_file),
            "sha256": sha256_bytes(prompt_bytes),
            "text": prompt_text,
        }

    board_hash = sha256_bytes(image_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image_bytes)
    manifest_data = {
        "format": IMPORT_FORMAT,
        "stage": "1_visual_board_import",
        "authority": {
            "input": "existing_rendered_board",
            "board_role": "visual_intermediate_representation",
            "concept_generated_here": False,
            "llm_called_here": False,
        },
        "source_board": {
            "path": str(board_image),
            "sha256": board_hash,
            "mime_type": "image/png",
        },
        "vizconcept": prompt_record,
        "generation": {
            "known": False,
            "note": "Imported existing render; original Comfy workflow "
                    "and settings were not supplied.",
        },
        "request_fingerprint": _canonical_hash({
            "source_board_sha256": board_hash,
            "vizconcept": prompt_record,
        }),
        "board": {
            "path": str(output),
            "mime_type": "image/png",
            "sha256": sha256_file(output),
        },
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output


class ComfyVizConceptClient:
    """Synchronous ComfyUI API client for the Slice-1 board boundary."""

    def __init__(self, *, base_url: str = "http://127.0.0.1:8188",
                 timeout: float = 30.0, poll_timeout: float = 900.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.poll_timeout = float(poll_timeout)

    def checkpoint_names(self) -> list[str]:
        info = self._get_json("/object_info/CheckpointLoaderSimple")
        return list(
            info.get("CheckpointLoaderSimple", {})
            .get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
        )

    def free_models(self) -> bool:
        """Best-effort Comfy unload used before the next GPU-heavy stage."""
        payload = json.dumps({
            "unload_models": True,
            "free_memory": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/free", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def generate(self, *, vizconcept_file: Path, output: Path,
                 manifest: Path, checkpoint: str = DEFAULT_CHECKPOINT,
                 seed: int = DEFAULT_SEED, width: int = DEFAULT_WIDTH,
                 height: int = DEFAULT_HEIGHT, steps: int = DEFAULT_STEPS,
                 cfg: float = DEFAULT_CFG,
                 sampler: str = DEFAULT_SAMPLER,
                 scheduler: str = DEFAULT_SCHEDULER,
                 negative: str = DEFAULT_NEGATIVE_PROMPT) -> Path:
        """Generate a board and write its immutable provenance manifest."""
        if not vizconcept_file.is_file():
            raise VisualReasoningCompilerError(
                f"vizconcept file not found: {vizconcept_file}"
            )
        if output.exists() or manifest.exists():
            raise VisualReasoningCompilerError(
                "refusing to overwrite visual-reasoning artifact: "
                f"{output if output.exists() else manifest}"
            )
        raw_bytes = vizconcept_file.read_bytes()
        try:
            source_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VisualReasoningCompilerError(
                f"vizconcept file must be UTF-8: {vizconcept_file}"
            ) from exc
        positive = build_vizconcept_prompt(source_text)
        available = self.checkpoint_names()
        if checkpoint not in available:
            raise VisualReasoningCompilerError(
                f"checkpoint {checkpoint!r} is not visible to ComfyUI; "
                f"available count={len(available)}"
            )
        workflow = build_workflow(
            checkpoint=checkpoint, positive=positive, negative=negative,
            seed=seed, width=width, height=height, steps=steps, cfg=cfg,
            sampler=sampler, scheduler=scheduler,
        )
        client_id = str(uuid.uuid4())
        response = self._post_json(
            "/prompt", {"prompt": workflow, "client_id": client_id}
        )
        if response.get("node_errors"):
            raise VisualReasoningCompilerError(
                f"ComfyUI rejected graph: {response['node_errors']}"
            )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise VisualReasoningCompilerError(
                f"ComfyUI returned no prompt_id: {response}"
            )
        history = self._poll_history(prompt_id)
        image_ref = _first_image_ref(history.get("outputs", {}))
        if image_ref is None:
            raise VisualReasoningCompilerError(
                f"ComfyUI produced no image for prompt {prompt_id}"
            )
        image_bytes = self._fetch_image(image_ref)
        if not _looks_like_png(image_bytes):
            raise VisualReasoningCompilerError(
                "ComfyUI returned a non-PNG or truncated board image"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image_bytes)
        settings = {
            "checkpoint": checkpoint, "seed": seed, "width": width,
            "height": height, "steps": steps, "cfg": cfg,
            "sampler": sampler, "scheduler": scheduler,
        }
        manifest_data = {
            "format": FORMAT,
            "stage": "1_visual_concept_to_board",
            "authority": {
                "input": "vizconcept.txt",
                "board_role": "visual_intermediate_representation",
                "concept_generated_here": False,
                "llm_called_here": False,
            },
            "source": {
                "path": str(vizconcept_file),
                "sha256": sha256_bytes(raw_bytes),
                "text": source_text,
            },
            "prompt": {"positive": positive, "negative": negative},
            "settings": settings,
            "request_fingerprint": _canonical_hash({
                "source_sha256": sha256_bytes(raw_bytes),
                "prompt": {"positive": positive, "negative": negative},
                "settings": settings,
                "workflow": workflow,
            }),
            "comfy": {
                "base_url": self.base_url,
                "prompt_id": prompt_id,
                "client_id": client_id,
                "output_ref": image_ref,
            },
            "workflow": workflow,
            "board": {
                "path": str(output),
                "mime_type": "image/png",
                "sha256": sha256_file(output),
            },
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return output

    def _post_json(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise VisualReasoningCompilerError(
                f"ComfyUI HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI request {path} failed: {exc}"
            ) from exc

    def _get_json(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI request {path} failed: {exc}"
            ) from exc

    def _fetch_image(self, image_ref: dict) -> bytes:
        query = urllib.parse.urlencode(image_ref)
        try:
            with urllib.request.urlopen(
                self.base_url + "/view?" + query, timeout=self.timeout
            ) as response:
                return response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI image fetch failed: {exc}"
            ) from exc

    def _poll_history(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            try:
                history = self._get_json(f"/history/{prompt_id}")
            except VisualReasoningCompilerError:
                time.sleep(1.0)
                continue
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {}) or {}
                if status.get("status_str") == "error":
                    raise VisualReasoningCompilerError(
                        f"ComfyUI generation failed for {prompt_id}: {status}"
                    )
                if entry.get("outputs"):
                    return entry
            time.sleep(1.0)
        raise VisualReasoningCompilerError(
            f"ComfyUI generation timed out after {self.poll_timeout:g}s "
            f"({prompt_id})"
        )


def _first_image_ref(outputs: dict) -> dict | None:
    for node_output in outputs.values():
        images = node_output.get("images") or []
        if images:
            image = images[0]
            return {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
    return None
