"""Input binding atoms — action/axis binding and event routing.

These are the primitive opcodes for hooking up input to game logic:
bind an action, read its value, map it to an event.

Target: Unity's legacy Input.GetButton / Input.GetAxis API by default.
For Unity Input System, the stubs generated use GetButton/GetAxis as readable
pseudocode; generated controllers should remap to UnityEngine.InputSystem.
"""

from .base import AtomDef, AtomParam

INPUT_ATOMS: list[AtomDef] = [
    AtomDef(
        name="BindAction",
        category="input",
        description="Document a named input action binding and broadcast it via SendMessage.",
        duration="instant",
        side_effects=["input_binding"],
        aliases=["bind_action", "map_action", "register_action"],
        engines=["unity"],
        params=[
            AtomParam(name="action_name", type="string", default="Jump",   required=True,
                      description="Input action name (matches Input Manager or InputActionAsset entry)"),
            AtomParam(name="event_name",  type="string", default="OnJump", required=True,
                      description="Message to send when the action fires"),
        ],
    ),
    AtomDef(
        name="BindAxis",
        category="input",
        description="Sample a named input axis and broadcast the scaled value via SendMessage.",
        duration="instant",
        side_effects=["input_binding"],
        aliases=["bind_axis", "map_axis", "register_axis"],
        engines=["unity"],
        params=[
            AtomParam(name="axis_name",   type="string", default="Horizontal", required=True,
                      description="Input axis name (matches Input Manager axis)"),
            AtomParam(name="param_name",  type="string", default="MoveX",
                      description="Message name to send with the axis value"),
            AtomParam(name="sensitivity", type="float",  default=1.0,
                      description="Axis scale multiplier before broadcasting"),
        ],
    ),
    AtomDef(
        name="ReadInput",
        category="input",
        description="Sample a button/action and fire an event when it is pressed.",
        duration="instant",
        side_effects=["event"],
        aliases=["read_input", "poll_action", "check_input"],
        engines=["unity"],
        params=[
            AtomParam(name="action_name", type="string", default="Fire1", required=True,
                      description="Button/action name to read (Input.GetButton)"),
            AtomParam(name="set_flag",    type="string", default="OnFire",
                      description="Event/message to send when the button is pressed"),
        ],
    ),
    AtomDef(
        name="MapInputToEvent",
        category="input",
        description="Fire a named event when a button is held and axis exceeds a threshold.",
        duration="instant",
        side_effects=["event"],
        aliases=["map_input_to_event", "input_event", "on_input"],
        engines=["unity"],
        params=[
            AtomParam(name="action_name", type="string", default="Fire1",   required=True,
                      description="Input action/button name to poll"),
            AtomParam(name="event_name",  type="string", default="OnAction", required=True,
                      description="Message to send when the condition is met"),
            AtomParam(name="threshold",   type="float",  default=0.5,
                      description="Axis magnitude threshold (for axis inputs; 0.5 = half-press)"),
        ],
    ),
]
