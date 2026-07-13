"""
GTA5 micro-generators — the minigame-serving set.

A deliberate subset (NOT a mirror of the 21 Unity kinds): these five are the
building blocks the minigame skeleton composes — menu, hud, spawner, waves,
saveload. Each module follows the Unity micro pattern: a GtaCodeExtensibleSchema
subclass, concept/schema prompts, and a GTA5MicroPipeline subclass whose
write_files renders templates/gta5/micro/<kind>/.
"""

from .menu     import GtaMenuPipeline
from .hud      import GtaHudPipeline
from .spawner  import GtaSpawnerPipeline
from .waves    import GtaWavesPipeline
from .saveload import GtaSaveLoadPipeline

GTA_REGISTRY = {
    "menu":     GtaMenuPipeline,
    "hud":      GtaHudPipeline,
    "spawner":  GtaSpawnerPipeline,
    "waves":    GtaWavesPipeline,
    "saveload": GtaSaveLoadPipeline,
}

__all__ = [
    "GTA_REGISTRY",
    "GtaMenuPipeline", "GtaHudPipeline", "GtaSpawnerPipeline",
    "GtaWavesPipeline", "GtaSaveLoadPipeline",
]
