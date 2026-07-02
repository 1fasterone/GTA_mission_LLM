"""Audio atoms — play / stop sounds."""

from .base import AtomDef, AtomParam

AUDIO_ATOMS: list[AtomDef] = [
    AtomDef(
        name="PlaySFX",
        category="audio",
        description="Play a one-shot sound effect at the entity's position.",
        duration="instant",
        side_effects=["audio"],
        aliases=["play_sfx", "play_sound", "sfx", "sound_effect"],
        params=[
            AtomParam(name="sound",  type="sfx_id", required=True, description="Sound effect identifier"),
            AtomParam(name="volume", type="float",  default=1.0,   description="Playback volume 0..1"),
            AtomParam(name="pitch",  type="float",  default=1.0,   description="Playback pitch"),
        ],
    ),
    AtomDef(
        name="PlayMusic",
        category="audio",
        description="Start a looping background music track, replacing any current track.",
        duration="instant",
        side_effects=["audio", "music_state"],
        aliases=["play_music", "start_music", "bgm", "set_music"],
        params=[
            AtomParam(name="track",  type="sfx_id", required=True, description="Music track identifier"),
            AtomParam(name="volume", type="float",  default=0.8,   description="Music volume 0..1"),
            AtomParam(name="loop",   type="bool",   default=True,  description="Loop the track"),
        ],
    ),
    AtomDef(
        name="StopMusic",
        category="audio",
        description="Fade out and stop the current background music.",
        duration="duration_param",
        side_effects=["audio", "music_state"],
        aliases=["stop_music", "silence_music", "fade_music"],
        params=[
            AtomParam(name="duration", type="float", default=0.5, description="Fade-out duration seconds"),
        ],
    ),
]
