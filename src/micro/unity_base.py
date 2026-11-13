"""
Unity-specific MicroPipeline subclass.

All Unity micro-generators inherit from this instead of MicroPipeline directly.
This keeps the 6-step base engine-agnostic while centralising every Unity-ism
(C# system prompts, template root, output root) in one place.

Adding a GTA5 domain is then: create GTA5MicroPipeline(MicroPipeline) with
Lua/SHVDN-appropriate system prompts and a different template root.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from pydantic import BaseModel

from ..micro_pipeline import MicroPipeline


_UNITY_CONCEPT_SYSTEM = (
    "You are a senior Unity engineer. Describe a focused Unity feature in concrete "
    "technical terms — no code, no JSON, no package IDs. Be specific about behaviour, "
    "inputs, and which Unity components are involved."
)

_UNITY_SCHEMA_FILL_SYSTEM = (
    "You are a Unity schema designer. Output ONLY valid JSON — no explanation, no "
    "markdown fences. Use plain English for description fields."
)


class UnityMicroPipeline(MicroPipeline):
    """Base for all Unity micro-generators."""

    engine             = "unity"
    concept_system     = _UNITY_CONCEPT_SYSTEM
    schema_fill_system = _UNITY_SCHEMA_FILL_SYSTEM

    def __init__(self, config: dict):
        super().__init__(config)
        unity_cfg = config.get("unity_pipeline", {})
        # Override output/template roots to the Unity-specific paths.
        self.output_root = (
            Path(unity_cfg.get("unity_output_dir", "output/unity")) / "micro" / self.kind
        )
        self.template_root = (
            Path(unity_cfg.get("unity_template_dir", "templates/unity"))
            / "micro"
            / (self.template_subdir or self.kind)
        )

    @classmethod
    def api_baseline_options(cls, options: dict) -> dict:
        """Clone structural options for API inspection, before default_fix.

        Writers with nested C# channels override this hook to neutralize those
        bodies without changing the branch choices that determine declarations.
        This never rewrites the persisted selection or production schema.
        """
        return deepcopy(options)

    def write_api_sources(self, schema, variant: str, out_dir: Path) -> int:
        """Render the production declarations for read-only API inspection.

        Deterministic writers can reuse write_files. Writers that fulfill assets
        must override this hook and omit synthesis, backend calls and batching.
        """
        return self.write_files(schema, variant, out_dir)

    def _after_write(self, schema: BaseModel, variant: str, out_dir: Path, name: str) -> None:
        """Retired in R2: Unity micros write no ``<Kind>DefaultBehavior`` companion.

        This hook used to emit one per mapped kind from MICROGEN_ATOM_MAP. No
        generated scene or prefab ever attached it (a standalone micro folder
        has no scene; composed builds call ``write_files`` directly), so each
        one was code that read like behavior and never ran. Attaching it would
        have been worse: the controller companion loops Look/Move(forward)/
        Sprint/Jump, fighting the template-owned controller it sits beside.
        MICROGEN_ATOM_MAP stays descriptive data; a micro's runtime owner is its
        own template. ``ownership_audit`` reads this body: a hook that writes
        a companion again reopens the route as ``connected_but_not_attached``,
        which fails the audit.
        """
        return None
