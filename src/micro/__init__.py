"""Unity micro-generators — focused single-purpose tools on the shared pipeline."""

from typing import Any

from .controller    import ControllerPipeline,    ControllerSchema
from .camera        import CameraRigPipeline,     CameraRigSchema
from .physics       import PhysicsPipeline,       PhysicsSchema
from .hud           import HUDPipeline,           HUDSchema
from .input_actions import InputPipeline,         InputSchema
from .audio         import AudioPipeline,         AudioSchema
from .ai            import AIPipeline,            AISchema
from .spawner       import SpawnerPipeline,       SpawnerSchema
from .saveload      import SaveLoadPipeline,      SaveLoadSchema
from .inventory     import InventoryPipeline,     InventorySchema
from .dialogue      import DialoguePipeline,      DialogueSchema
from .menu          import MenuPipeline,          MenuSchema
from .interactable  import InteractablePipeline,  InteractableSchema
from .transition    import TransitionPipeline,    TransitionSchema
from .health        import HealthPipeline,        HealthSchema
from .model         import ModelPipeline
from .statemachine  import StateMachinePipeline,  StateMachineSchema
from .trigger       import TriggerPipeline,       TriggerSchema
from .animator      import AnimatorPipeline,      AnimatorSchema
from .npc_dialog    import NpcDialogPipeline,     NpcDialogSchema,   DialogLine
from .vfx           import VfxPipeline,           VfxSchema
from .lighting      import LightingPipeline,      LightingSchema,   LightSpec
from .shader        import ShaderPipeline,        ShaderSchema
from .objectives    import ObjectivesPipeline,    ObjectivesSchema
from .rules         import RulesPipeline,         RulesSchema
from .board         import BoardPipeline,         BoardSchema
from .behavior_tree  import BehaviorTreePipeline,  BehaviorTreeSchema

__all__ = [
    "ModelPipeline",
    "ControllerPipeline", "ControllerSchema",
    "CameraRigPipeline",  "CameraRigSchema",
    "PhysicsPipeline",    "PhysicsSchema",
    "HUDPipeline",        "HUDSchema",
    "InputPipeline",      "InputSchema",
    "AudioPipeline",      "AudioSchema",
    "AIPipeline",         "AISchema",
    "SpawnerPipeline",    "SpawnerSchema",
    "SaveLoadPipeline",   "SaveLoadSchema",
    "InventoryPipeline",  "InventorySchema",
    "DialoguePipeline",   "DialogueSchema",
    "MenuPipeline",       "MenuSchema",
    "InteractablePipeline", "InteractableSchema",
    "TransitionPipeline",   "TransitionSchema",
    "HealthPipeline",       "HealthSchema",
    "StateMachinePipeline", "StateMachineSchema",
    "TriggerPipeline",      "TriggerSchema",
    "AnimatorPipeline",     "AnimatorSchema",
    "NpcDialogPipeline",    "NpcDialogSchema",    "DialogLine",
    "VfxPipeline",          "VfxSchema",
    "LightingPipeline",     "LightingSchema",     "LightSpec",
    "ShaderPipeline",       "ShaderSchema",
    "ObjectivesPipeline",   "ObjectivesSchema",
    "RulesPipeline",        "RulesSchema",
    "BoardPipeline",        "BoardSchema",
    "BehaviorTreePipeline", "BehaviorTreeSchema",
]


# Registry used by the CLI to enumerate / spawn micro-generators. Values are a
# mix of MicroPipeline subclasses and the structurally-similar-but-unrelated
# ModelPipeline (Blender-backed, no schema_cls/default_fix) — annotate loosely
# so mypy doesn't widen the common type to `type[object]` (and flag every
# attribute access on it) or misread heterogeneous subclasses as instantiating
# an abstract base.
REGISTRY: dict[str, type[Any]] = {
    "controller": ControllerPipeline,
    "camera":     CameraRigPipeline,
    "physics":    PhysicsPipeline,
    "hud":        HUDPipeline,
    "input":      InputPipeline,
    "audio":      AudioPipeline,
    "ai":         AIPipeline,
    "spawner":    SpawnerPipeline,
    "saveload":   SaveLoadPipeline,
    "inventory":  InventoryPipeline,
    "dialogue":   DialoguePipeline,
    "menu":         MenuPipeline,
    "interactable": InteractablePipeline,
    "transition":   TransitionPipeline,
    "health":       HealthPipeline,
    "model":        ModelPipeline,
    "statemachine": StateMachinePipeline,
    "trigger":      TriggerPipeline,
    "animator":     AnimatorPipeline,
    "npc_dialog":   NpcDialogPipeline,
    "vfx":          VfxPipeline,
    "lighting":     LightingPipeline,
    "shader":       ShaderPipeline,
    "objectives":   ObjectivesPipeline,
    "rules":        RulesPipeline,
    "board":        BoardPipeline,
    "behavior_tree": BehaviorTreePipeline,
}


# GTA5 micro registry — separate namespace so kind names ("menu", "hud", ...)
# never collide with the Unity kinds above. CLI prefix: gta-<kind>.
# Redundant alias marks this as an intentional re-export (main.py imports it
# from here), not a stray unused import.
from .gta5 import GTA_REGISTRY as GTA_REGISTRY  # noqa: E402
