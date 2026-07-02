"""NPC dialog atoms — SayLine, PlayDialogAudio, Gesture, LookAt."""

from .base import AtomDef, AtomParam

DIALOG_ATOMS: list[AtomDef] = [
    AtomDef(
        name="SayLine",
        category="dialog",
        description="Play an NPC speech line by line_id, firing an OnLine event with speaker + text.",
        duration="duration_param",
        side_effects=["audio", "dialog_state"],
        aliases=["say_line", "speak", "npc_speak", "dialog_say"],
        params=[
            AtomParam(name="line_id",  type="string", required=True,  description="Unique line identifier in the dialog bank"),
            AtomParam(name="volume",   type="float",  default=1.0,    description="Playback volume 0..1"),
            AtomParam(name="blocking", type="bool",   default=True,   description="Wait for line to finish before next atom"),
        ],
    ),
    AtomDef(
        name="PlayDialogAudio",
        category="dialog",
        description="Play a pre-generated dialog WAV clip by Resources-relative path.",
        duration="instant",
        side_effects=["audio"],
        aliases=["play_dialog_audio", "play_voice_clip", "dialog_audio"],
        params=[
            AtomParam(name="path",    type="sfx_id", required=True, description="Resources-relative path to the WAV clip"),
            AtomParam(name="volume",  type="float",  default=1.0,   description="Playback volume 0..1"),
            AtomParam(name="spatial", type="bool",   default=True,  description="3D positional audio"),
        ],
    ),
    AtomDef(
        name="Gesture",
        category="dialog",
        description="Trigger a gesture animation on the NPC while talking.",
        duration="duration_param",
        side_effects=["animation"],
        aliases=["gesture", "npc_gesture", "talk_gesture"],
        params=[
            AtomParam(name="type",     type="anim_clip", required=True,  description="Gesture name: point|wave|shrug|nod|shake_head|cross_arms"),
            AtomParam(name="duration", type="float",     default=1.5,    description="Gesture duration seconds"),
            AtomParam(name="blend",    type="float",     default=0.3,    description="Animation blend weight 0..1"),
        ],
    ),
    AtomDef(
        name="LookAt",
        category="dialog",
        description="Rotate the NPC's head to look at a target during dialog.",
        duration="duration_param",
        side_effects=["animation"],
        aliases=["look_at", "npc_look_at", "head_look"],
        params=[
            AtomParam(name="target",   type="entity_ref", required=True,  description="Target name (Player, Camera, or object name)"),
            AtomParam(name="duration", type="float",      default=2.0,    description="How long to maintain look-at (0 = until next atom)"),
            AtomParam(name="weight",   type="float",      default=0.8,    description="IK weight for look-at 0..1"),
        ],
    ),
    AtomDef(
        name="EndDialog",
        category="dialog",
        description="Fire OnDialogEnd event and release NPC back to idle state.",
        duration="instant",
        side_effects=["dialog_state"],
        aliases=["end_dialog", "dialog_end", "close_dialog"],
        params=[
            AtomParam(name="return_to_idle", type="bool", default=True,
                      description="Resume idle animation after dialog"),
        ],
    ),
]
