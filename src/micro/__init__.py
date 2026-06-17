"""Unity micro-generators — focused single-purpose tools on the shared pipeline."""

from .controller     import ControllerPipeline,    ControllerSchema
from .camera         import CameraRigPipeline,     CameraRigSchema
from .physics        import PhysicsPipeline,       PhysicsSchema
from .hud            import HUDPipeline,           HUDSchema
from .input_actions  import InputPipeline,         InputSchema
from .audio          import AudioPipeline,         AudioSchema
from .ai             import AIPipeline,            AISchema
from .spawner        import SpawnerPipeline,       SpawnerSchema
from .saveload       import SaveLoadPipeline,      SaveLoadSchema
from .inventory      import InventoryPipeline,     InventorySchema
from .dialogue       import DialoguePipeline,      DialogueSchema
from .menu           import MenuPipeline,          MenuSchema
from .interactable   import InteractablePipeline,  InteractableSchema
from .transition     import TransitionPipeline,    TransitionSchema
from .health         import HealthPipeline,        HealthSchema

__all__ = [
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
]


# Registry used by the CLI to enumerate / spawn micro-generators.
REGISTRY = {
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
}
