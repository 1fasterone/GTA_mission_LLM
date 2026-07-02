"""
Unity-specific MicroPipeline subclass.

All Unity micro-generators inherit from this instead of MicroPipeline directly.
This keeps the 6-step base engine-agnostic while centralising every Unity-ism
(C# system prompts, template root, output root) in one place.

Adding a GTA5 domain is then: create GTA5MicroPipeline(MicroPipeline) with
Lua/SHVDN-appropriate system prompts and a different template root.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

from ..micro_pipeline import MicroPipeline

_console = Console()


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

    def _after_write(self, schema: BaseModel, variant: str, out_dir: Path, name: str) -> None:
        """Inject atom-driven behavior stubs after the micro's main files are written.

        Calls microgen_atoms(self.kind) to get the canonical atom sequence for this
        micro kind, then emits a <Kind>DefaultBehavior.cs coroutine stub alongside
        the generated micro files. Kinds with no atom sequence (controller, hud, ...)
        are silently skipped.
        """
        from ..atoms.behavior_map import microgen_atoms, behavior_loops
        from ..atoms.backends.unity import unity_coroutine_body

        atoms = microgen_atoms(self.kind)
        if not atoms:
            return

        atom_dicts = [inv.model_dump() for inv in atoms]
        atom_body  = unity_coroutine_body(atom_dicts)
        loops      = behavior_loops(atoms)

        kind_pascal = "".join(p.capitalize() for p in self.kind.split("_"))
        class_name  = f"{kind_pascal}DefaultBehavior"

        indent = "        "  # 8 spaces inside the method body
        indented_body = atom_body.replace("\n", f"\n{indent}")
        loop_line = f"\n{indent}StartCoroutine(RunDefaultBehavior());" if loops else ""

        cs = (
            f"// Auto-generated — DO NOT EDIT\n"
            f"// Default atom-driven behavior stubs for the '{self.kind}' micro-generator.\n"
            f"using System.Collections;\n"
            f"using UnityEngine;\n\n"
            f"public class {class_name} : MonoBehaviour\n"
            f"{{\n"
            f"    private void Start() => StartCoroutine(RunDefaultBehavior());\n\n"
            f"    public IEnumerator RunDefaultBehavior()\n"
            f"    {{\n"
            f"        {indented_body}{loop_line}\n"
            f"        yield return null;\n"
            f"    }}\n"
            f"}}\n"
        )
        out_path = out_dir / f"{class_name}.cs"
        out_path.write_text(cs, encoding="utf-8")
        _console.print(f"  [cyan]ATM[/]  atom stubs ({len(atoms)} atoms) -> {out_path.name}")
