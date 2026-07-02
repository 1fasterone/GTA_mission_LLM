"""Logic / timing atoms — flow control, flags, events."""

from .base import AtomDef, AtomParam

LOGIC_ATOMS: list[AtomDef] = [
    AtomDef(
        name="Wait",
        category="logic",
        description="Pause execution for a fixed number of seconds.",
        duration="duration_param",
        side_effects=[],
        aliases=["wait", "pause", "delay", "sleep", "hold"],
        params=[
            AtomParam(name="seconds", type="float", default=1.0, required=True,
                      description="How many seconds to wait"),
        ],
    ),
    AtomDef(
        name="SetFlag",
        category="logic",
        description="Write a named boolean flag to persistent storage (PlayerPrefs).",
        duration="instant",
        side_effects=["flags"],
        aliases=["set_flag", "flag", "set_bool", "set_checkpoint", "mark"],
        params=[
            AtomParam(name="flag",  type="string", required=True, description="Flag key name"),
            AtomParam(name="value", type="bool",   default=True,  description="True or False"),
        ],
    ),
    AtomDef(
        name="TriggerEvent",
        category="logic",
        description="Broadcast a named game event via SendMessage to all listeners.",
        duration="instant",
        side_effects=["events"],
        aliases=["trigger_event", "send_event", "fire_event", "broadcast", "send_message"],
        params=[
            AtomParam(name="event", type="string", required=True, description="Event name to broadcast"),
        ],
    ),
    AtomDef(
        name="ConditionCheck",
        category="logic",
        description="Read a named flag and branch: skip the next atom if the condition is false.",
        duration="instant",
        side_effects=[],
        aliases=["if_flag", "check_flag", "condition", "branch_on_flag"],
        params=[
            AtomParam(name="flag",   type="string", required=True, description="Flag key to read"),
            AtomParam(name="expect", type="bool",   default=True,  description="Expected value to pass"),
        ],
    ),
]
