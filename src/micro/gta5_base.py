"""
GTA5-specific MicroPipeline subclass.

The SHVDN twin of unity_base.UnityMicroPipeline: keeps the 6-step base
engine-agnostic while centralising every GTA-ism (SHVDN system prompts,
template root, output root, atom-stub emission) in one place.

GTA micros are authored as Jinja macro partials (templates/gta5/micro/<kind>/
_partial.j2) plus a thin standalone Script wrapper (<Kind>.3.cs.j2). The CLI
path emits the standalone drop-in .3.cs; the minigame skeleton can `include`
the same partial macros to compile a micro INTO a single mod file — SHVDN3
compiles each raw .cs into its own assembly, so cross-file class references
are not possible and inlining is the only composition mechanism.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

from ..micro_pipeline import MicroPipeline

_console = Console()


_GTA5_CONCEPT_SYSTEM = (
    "You are a senior GTA5 ScriptHookVDotNet (SHVDN3) scripter. Describe a "
    "focused, self-contained GTA5 script feature in concrete technical terms — "
    "no code, no JSON. Be specific about behaviour, native mechanics involved "
    "(peds, vehicles, blips, wanted level, HUD drawing), and player-facing "
    "feel. This is GTA5, NOT Unity — never mention Unity concepts."
)

_GTA5_SCHEMA_FILL_SYSTEM = (
    "You are a GTA5 script schema designer. Output ONLY valid JSON — no "
    "explanation, no markdown fences. Use plain English for description fields."
)


class GTA5MicroPipeline(MicroPipeline):
    """Base for all GTA5 micro-generators."""

    engine             = "gta5"
    concept_system     = _GTA5_CONCEPT_SYSTEM
    schema_fill_system = _GTA5_SCHEMA_FILL_SYSTEM

    def __init__(self, config: dict):
        super().__init__(config)
        pipe_cfg = config.get("pipeline", {})
        self.output_root = (
            Path(pipe_cfg.get("gta_micro_output_dir", "output/gta5/micro")) / self.kind
        )
        self.template_root = (
            Path(pipe_cfg.get("gta_micro_template_dir", "templates/gta5/micro"))
            / (self.template_subdir or self.kind)
        )

    def _after_write(self, schema: BaseModel, variant: str, out_dir: Path, name: str) -> None:
        """Emit the kind's canonical atom sequence as a standalone SHVDN stub.

        Mirrors UnityMicroPipeline._after_write: microgen_atoms(kind, "gta5")
        supplies the sequence, gta5_tick_body assembles it, and the stub Script
        runs it once behind a _ran guard (looping sequences keep themselves
        alive inside that first invocation — Script.Wait yields to the game).
        Kinds with no gta5 atom sequence are silently skipped.
        """
        from ..atoms.behavior_map import microgen_atoms, behavior_loops
        from ..atoms.backends.gta5 import gta5_tick_body

        atoms = microgen_atoms(self.kind, engine="gta5")
        if not atoms:
            return

        atom_dicts = [inv.model_dump() for inv in atoms]
        body = gta5_tick_body(atom_dicts, loop=behavior_loops(atoms))

        kind_pascal = "".join(p.capitalize() for p in self.kind.split("_"))
        class_name  = f"{kind_pascal}DefaultBehavior"

        indent = "        "
        indented_body = body.replace("\n", f"\n{indent}")

        cs = (
            f"// Auto-generated — DO NOT EDIT\n"
            f"// Default atom-driven behavior stub for the '{self.kind}' GTA5 micro.\n"
            f"using System;\n"
            f"using System.Collections.Generic;\n"
            f"using GTA;\n"
            f"using GTA.Math;\n"
            f"using GTA.Native;\n\n"
            f"public class {class_name} : Script\n"
            f"{{\n"
            f"    private bool _ran = false;\n\n"
            f"    public {class_name}()\n"
            f"    {{\n"
            f"        Tick += OnTick;\n"
            f"    }}\n\n"
            f"    private void OnTick(object sender, EventArgs e)\n"
            f"    {{\n"
            f"        if (_ran) return;\n"
            f"        _ran = true;\n"
            f"        {indented_body}\n"
            f"    }}\n"
            f"}}\n"
        )
        out_path = out_dir / f"{class_name}.3.cs"
        out_path.write_text(cs, encoding="utf-8")
        _console.print(f"  [cyan]ATM[/]  atom stubs ({len(atoms)} atoms) -> {out_path.name}")
