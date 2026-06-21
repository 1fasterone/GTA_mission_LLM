"""
Curated Unity component corpus — built-in and standard package components.

No network required. Data is maintained here so it tracks the compiler's
supported component set exactly (same philosophy as GTA5 weapons/props).
"""
import sys
from pathlib import Path

from ..schema.unity_component import make_component_entry

OUT = Path(__file__).parent.parent / "corpus" / "unity" / "components.jsonl"

# fmt: off
_COMPONENTS: list[dict] = [
    # ── Transform ────────────────────────────────────────────────────────────
    {
        "name": "Transform", "namespace": "UnityEngine", "category": "transform",
        "description": "Position, rotation and scale of a GameObject. Every GameObject has one and it cannot be removed.",
        "aliases": ["position", "rotation", "scale", "location", "move component"],
        "api_methods": ["SetPosition", "Translate", "Rotate", "LookAt", "SetParent", "GetChild"],
        "capabilities": ["transform"],
        "pipeline": "any",
    },

    # ── Rendering ────────────────────────────────────────────────────────────
    {
        "name": "Camera", "namespace": "UnityEngine", "category": "rendering",
        "description": "Renders the scene to the screen or a RenderTexture. Every scene needs at least one Main Camera.",
        "aliases": ["main camera", "view camera", "perspective camera", "orthographic camera"],
        "api_methods": ["ScreenToWorldPoint", "WorldToScreenPoint", "Render", "SetTargetBuffers"],
        "requires": [],
        "capabilities": ["camera"],
        "pipeline": "any",
    },
    {
        "name": "Light", "namespace": "UnityEngine", "category": "rendering",
        "description": "Illuminates the scene. Supports Directional, Point, Spot and Area light types.",
        "aliases": ["directional light", "point light", "spot light", "area light", "sun"],
        "api_methods": ["SetColor", "SetIntensity", "SetRange"],
        "capabilities": ["lighting"],
        "pipeline": "any",
    },
    {
        "name": "MeshRenderer", "namespace": "UnityEngine", "category": "rendering",
        "description": "Renders a mesh on the GameObject. Works together with MeshFilter.",
        "aliases": ["mesh renderer", "3d renderer", "object renderer", "render mesh"],
        "requires": ["MeshFilter"],
        "api_methods": ["SetMaterial", "SetPropertyBlock"],
        "capabilities": ["renderable"],
        "pipeline": "any",
    },
    {
        "name": "MeshFilter", "namespace": "UnityEngine", "category": "rendering",
        "description": "Holds a reference to the 3D mesh displayed by MeshRenderer.",
        "aliases": ["mesh filter", "mesh holder", "mesh data"],
        "api_methods": ["SetMesh"],
        "capabilities": ["mesh"],
        "pipeline": "any",
    },
    {
        "name": "SkinnedMeshRenderer", "namespace": "UnityEngine", "category": "rendering",
        "description": "Renders a mesh deformed by bone weights. Required for all animated character models.",
        "aliases": ["skinned mesh", "character renderer", "animated mesh", "skinned renderer"],
        "api_methods": ["SetBlendShapeWeight", "BakeMesh"],
        "capabilities": ["renderable", "skinned", "character"],
        "pipeline": "any",
    },
    {
        "name": "SpriteRenderer", "namespace": "UnityEngine", "category": "rendering",
        "description": "Renders a 2D Sprite in the scene. Core component for 2D games.",
        "aliases": ["sprite", "2d sprite", "sprite renderer", "2d renderer"],
        "api_methods": ["SetSprite", "SetColor", "SetFlipX", "SetFlipY"],
        "capabilities": ["renderable", "2d"],
        "pipeline": "any",
    },
    {
        "name": "ParticleSystem", "namespace": "UnityEngine", "category": "rendering",
        "description": "Simulates fluid-like particle effects: fire, smoke, sparks, rain, magic.",
        "aliases": ["particles", "particle emitter", "vfx particles", "smoke", "fire", "sparks", "magic effect"],
        "api_methods": ["Play", "Stop", "Pause", "Emit", "Clear"],
        "capabilities": ["vfx", "particle"],
        "pipeline": "any",
    },
    {
        "name": "TrailRenderer", "namespace": "UnityEngine", "category": "rendering",
        "description": "Draws a trail behind a moving object. Good for bullets, racing lines, magic trails.",
        "aliases": ["trail", "motion trail", "speed trail"],
        "api_methods": ["Clear", "SetPositions", "BakeMesh"],
        "capabilities": ["vfx"],
        "pipeline": "any",
    },
    {
        "name": "LineRenderer", "namespace": "UnityEngine", "category": "rendering",
        "description": "Draws a line through a set of 3D world-space points. Good for ropes, laser beams.",
        "aliases": ["line", "laser", "rope renderer", "beam"],
        "api_methods": ["SetPositions", "SetPosition", "SetWidth"],
        "capabilities": ["vfx"],
        "pipeline": "any",
    },
    {
        "name": "ReflectionProbe", "namespace": "UnityEngine", "category": "rendering",
        "description": "Captures a cubemap of the scene for reflections on metallic/specular surfaces.",
        "aliases": ["reflection", "cubemap probe", "environment probe"],
        "api_methods": ["RenderProbe", "BlendCubemap"],
        "capabilities": ["reflection"],
        "pipeline": "any",
    },
    {
        "name": "LODGroup", "namespace": "UnityEngine", "category": "lod",
        "description": "Controls Level of Detail: switches between mesh variants based on screen size.",
        "aliases": ["lod", "level of detail", "lod group"],
        "api_methods": ["ForceLOD", "RecalculateBounds"],
        "capabilities": ["lod"],
        "pipeline": "any",
    },

    # ── Physics 3D ───────────────────────────────────────────────────────────
    {
        "name": "Rigidbody", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Adds physics simulation to a 3D GameObject. Gravity, forces, collisions.",
        "aliases": ["physics body", "rigid body", "physics object", "physics simulation"],
        "requires": ["Collider"],
        "api_methods": ["AddForce", "AddTorque", "MovePosition", "MoveRotation", "Sleep", "WakeUp"],
        "capabilities": ["physics", "dynamic"],
        "pipeline": "any",
    },
    {
        "name": "BoxCollider", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Box-shaped collision volume. Most efficient collider for cubic objects.",
        "aliases": ["box collider", "cube collider", "box trigger", "rectangular collider"],
        "api_methods": ["SetCenter", "SetSize"],
        "capabilities": ["collider"],
        "pipeline": "any",
    },
    {
        "name": "SphereCollider", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Sphere-shaped collision volume. Most efficient collider for round objects.",
        "aliases": ["sphere collider", "circle collider 3d", "round collider"],
        "api_methods": ["SetCenter", "SetRadius"],
        "capabilities": ["collider"],
        "pipeline": "any",
    },
    {
        "name": "CapsuleCollider", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Capsule-shaped collision volume. Standard collider for humanoid characters.",
        "aliases": ["capsule collider", "character collider", "humanoid collider"],
        "api_methods": ["SetCenter", "SetRadius", "SetHeight"],
        "capabilities": ["collider", "character"],
        "pipeline": "any",
    },
    {
        "name": "MeshCollider", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Collision volume matching the exact mesh shape. Expensive; use for terrain and level geometry.",
        "aliases": ["mesh collider", "exact collider", "terrain collider shape"],
        "api_methods": ["SetMesh"],
        "capabilities": ["collider"],
        "pipeline": "any",
    },
    {
        "name": "CharacterController", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Capsule-based controller with step, slope, and skin-width. No Rigidbody needed.",
        "aliases": ["character controller", "player controller", "fps controller", "cc"],
        "api_methods": ["Move", "SimpleMove", "IsGrounded"],
        "capabilities": ["character", "controller"],
        "pipeline": "any",
    },
    {
        "name": "WheelCollider", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Specialized collider for vehicle wheels with suspension, friction, and motor torque.",
        "aliases": ["wheel collider", "car wheel", "vehicle wheel", "tyre"],
        "api_methods": ["GetWorldPose", "SetMotorTorque", "SetBrakeTorque", "SetSteerAngle"],
        "capabilities": ["vehicle", "wheel"],
        "pipeline": "any",
    },
    {
        "name": "HingeJoint", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Constrains two Rigidbodies to rotate around a shared axis. Good for doors, hinges.",
        "aliases": ["hinge joint", "door hinge", "pivot joint"],
        "api_methods": ["SetAxis", "SetLimits", "SetMotor"],
        "requires": ["Rigidbody"],
        "capabilities": ["joint"],
        "pipeline": "any",
    },
    {
        "name": "FixedJoint", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Locks two Rigidbodies together. Good for breakable connections.",
        "aliases": ["fixed joint", "weld joint", "rigid attachment"],
        "requires": ["Rigidbody"],
        "capabilities": ["joint"],
        "pipeline": "any",
    },
    {
        "name": "SpringJoint", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Connects two Rigidbodies with a spring. Good for bouncy platforms.",
        "aliases": ["spring joint", "elastic joint"],
        "requires": ["Rigidbody"],
        "capabilities": ["joint"],
        "pipeline": "any",
    },
    {
        "name": "Cloth", "namespace": "UnityEngine", "category": "physics_3d",
        "description": "Simulates cloth physics on a SkinnedMeshRenderer mesh.",
        "aliases": ["cloth simulation", "fabric physics", "cloth physics"],
        "requires": ["SkinnedMeshRenderer"],
        "capabilities": ["physics", "cloth"],
        "pipeline": "any",
    },

    # ── Physics 2D ───────────────────────────────────────────────────────────
    {
        "name": "Rigidbody2D", "namespace": "UnityEngine", "category": "physics_2d",
        "description": "Adds 2D physics simulation. Required for physics-driven 2D objects.",
        "aliases": ["2d physics", "2d rigid body", "2d physics body"],
        "requires": ["Collider2D"],
        "api_methods": ["AddForce", "AddTorque", "MovePosition", "MoveRotation"],
        "capabilities": ["physics", "2d", "dynamic"],
        "pipeline": "any",
    },
    {
        "name": "BoxCollider2D", "namespace": "UnityEngine", "category": "physics_2d",
        "description": "2D box-shaped collision area. Standard collider for rectangular 2D objects.",
        "aliases": ["2d box collider", "rectangle collider", "2d trigger box"],
        "capabilities": ["collider", "2d"],
        "pipeline": "any",
    },
    {
        "name": "CircleCollider2D", "namespace": "UnityEngine", "category": "physics_2d",
        "description": "2D circle-shaped collision area. Most efficient 2D collider for round sprites.",
        "aliases": ["2d circle collider", "circle trigger", "round 2d collider"],
        "capabilities": ["collider", "2d"],
        "pipeline": "any",
    },
    {
        "name": "PolygonCollider2D", "namespace": "UnityEngine", "category": "physics_2d",
        "description": "2D polygon-shaped collision defined by vertex points. For arbitrary sprite shapes.",
        "aliases": ["polygon collider", "custom shape collider 2d", "sprite collider"],
        "capabilities": ["collider", "2d"],
        "pipeline": "any",
    },
    {
        "name": "CapsuleCollider2D", "namespace": "UnityEngine", "category": "physics_2d",
        "description": "2D capsule-shaped collider. Standard for 2D platformer characters.",
        "aliases": ["capsule 2d", "2d character collider", "player collider 2d"],
        "capabilities": ["collider", "2d", "character"],
        "pipeline": "any",
    },

    # ── Audio ─────────────────────────────────────────────────────────────────
    {
        "name": "AudioSource", "namespace": "UnityEngine", "category": "audio",
        "description": "Plays back audio clips in 3D space. Controls volume, pitch, spatial blend.",
        "aliases": ["audio source", "sound source", "sound player", "audio player", "sfx player"],
        "api_methods": ["Play", "PlayOneShot", "Stop", "Pause", "UnPause", "SetClip", "SetVolume"],
        "capabilities": ["audio"],
        "pipeline": "any",
    },
    {
        "name": "AudioListener", "namespace": "UnityEngine", "category": "audio",
        "description": "The 3D 'ear' of the scene. Usually placed on the Main Camera. Only one active at a time.",
        "aliases": ["audio listener", "ear", "sound receiver"],
        "capabilities": ["audio_listener"],
        "pipeline": "any",
    },
    {
        "name": "AudioReverbZone", "namespace": "UnityEngine", "category": "audio",
        "description": "Applies reverb to audio when the AudioListener is inside the zone volume.",
        "aliases": ["reverb zone", "room acoustics", "reverb effect"],
        "capabilities": ["audio"],
        "pipeline": "any",
    },
    {
        "name": "AudioMixer", "namespace": "UnityEngine.Audio", "category": "audio",
        "description": "Hierarchical audio mixing graph with effects, sends and snapshots.",
        "aliases": ["audio mixer", "mixing board", "sound bus"],
        "capabilities": ["audio"],
        "pipeline": "any",
    },

    # ── Animation ─────────────────────────────────────────────────────────────
    {
        "name": "Animator", "namespace": "UnityEngine", "category": "animation",
        "description": "State-machine-driven animation controller. Required for character animations and blend trees.",
        "aliases": ["animator", "state machine", "animation controller", "mecanim", "blend tree"],
        "api_methods": ["SetTrigger", "SetBool", "SetFloat", "SetInteger", "Play", "CrossFade", "GetCurrentAnimatorStateInfo"],
        "requires": ["AnimatorController"],
        "capabilities": ["animation"],
        "pipeline": "any",
    },
    {
        "name": "Animation", "namespace": "UnityEngine", "category": "animation",
        "description": "Legacy animation system. Use Animator (Mecanim) for new projects.",
        "aliases": ["legacy animation", "classic animation", "animation legacy"],
        "api_methods": ["Play", "Stop", "IsPlaying", "CrossFade"],
        "capabilities": ["animation"],
        "pipeline": "any",
    },

    # ── Navigation ────────────────────────────────────────────────────────────
    {
        "name": "NavMeshAgent", "namespace": "UnityEngine.AI", "category": "navigation",
        "description": "Moves a GameObject along a baked NavMesh using pathfinding. Standard AI locomotion.",
        "aliases": ["navmesh agent", "ai agent", "pathfinding agent", "nav agent", "ai pathfinding"],
        "api_methods": ["SetDestination", "ResetPath", "Warp", "Stop", "Resume"],
        "capabilities": ["navigation", "ai"],
        "pipeline": "any",
    },
    {
        "name": "NavMeshObstacle", "namespace": "UnityEngine.AI", "category": "navigation",
        "description": "Creates a dynamic obstacle that agents path around. Good for moving objects.",
        "aliases": ["navmesh obstacle", "nav obstacle", "dynamic obstacle", "nav blocker"],
        "capabilities": ["navigation"],
        "pipeline": "any",
    },
    {
        "name": "NavMeshSurface", "namespace": "Unity.AI.Navigation", "category": "navigation",
        "description": "Runtime NavMesh baker (com.unity.ai.navigation). Replaces the baked NavMesh workflow.",
        "aliases": ["navmesh surface", "runtime navmesh", "ai surface"],
        "requires": ["com.unity.ai.navigation"],
        "capabilities": ["navigation", "baking"],
        "pipeline": "any",
    },
    {
        "name": "OffMeshLink", "namespace": "UnityEngine.AI", "category": "navigation",
        "description": "Defines a custom off-mesh link (jump, ladder, teleport) between two NavMesh areas.",
        "aliases": ["off mesh link", "nav jump link", "teleport link", "ladder link"],
        "capabilities": ["navigation"],
        "pipeline": "any",
    },

    # ── Post Processing / Volume ───────────────────────────────────────────────
    {
        "name": "Volume", "namespace": "UnityEngine.Rendering", "category": "postprocess",
        "description": "Triggers post-processing effect overrides (bloom, DoF, tone-mapping) via VolumeProfile.",
        "aliases": ["post processing volume", "pp volume", "effect volume", "bloom volume", "dof volume"],
        "api_methods": ["SetWeight", "SetProfile", "SetIsGlobal"],
        "capabilities": ["postprocess"],
        "pipeline": "urp",
    },

    # ── Event ─────────────────────────────────────────────────────────────────
    {
        "name": "EventSystem", "namespace": "UnityEngine.EventSystems", "category": "event",
        "description": "Manages input events and UI interaction. Required for all uGUI to work.",
        "aliases": ["event system", "ui events", "input events"],
        "capabilities": ["ui"],
        "pipeline": "any",
    },
    {
        "name": "EventTrigger", "namespace": "UnityEngine.EventSystems", "category": "event",
        "description": "Receives pointer and touch events and invokes UnityEvents. Attach to any UI element.",
        "aliases": ["event trigger", "pointer event", "ui event trigger"],
        "api_methods": ["AddTrigger", "RemoveTrigger"],
        "capabilities": ["ui"],
        "pipeline": "any",
    },
    {
        "name": "GraphicRaycaster", "namespace": "UnityEngine.UI", "category": "event",
        "description": "Casts rays against Canvas UI elements to detect pointer interaction.",
        "aliases": ["graphic raycaster", "ui raycaster", "canvas raycaster"],
        "requires": ["Canvas"],
        "capabilities": ["ui"],
        "pipeline": "any",
    },

    # ── UI (uGUI) ─────────────────────────────────────────────────────────────
    {
        "name": "Canvas", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Root container for all uGUI elements. Controls Screen Space vs World Space mode.",
        "aliases": ["ui canvas", "screen canvas", "hud canvas", "menu canvas"],
        "api_methods": ["SetRenderMode", "SetSortingOrder"],
        "capabilities": ["ui", "container"],
        "pipeline": "any",
    },
    {
        "name": "CanvasGroup", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Controls alpha, interactability and raycasting for a whole branch of UI.",
        "aliases": ["canvas group", "ui group", "fade group", "ui alpha"],
        "api_methods": ["SetAlpha", "SetInteractable"],
        "capabilities": ["ui"],
        "pipeline": "any",
    },
    {
        "name": "CanvasScaler", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Scales the Canvas to match target screen resolution and DPI.",
        "aliases": ["canvas scaler", "ui scaler", "resolution scaler", "responsive ui"],
        "requires": ["Canvas"],
        "capabilities": ["ui"],
        "pipeline": "any",
    },
    {
        "name": "Image", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Displays a Sprite or fill pattern in the UI. Core uGUI visual element.",
        "aliases": ["ui image", "sprite image", "icon", "panel", "background image"],
        "api_methods": ["SetSprite", "SetColor", "SetFillAmount"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "image"],
        "pipeline": "any",
    },
    {
        "name": "RawImage", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Displays any Texture2D or RenderTexture. Good for video playback and camera feeds.",
        "aliases": ["raw image", "texture image", "video preview", "camera feed ui"],
        "api_methods": ["SetTexture", "SetColor"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "texture"],
        "pipeline": "any",
    },
    {
        "name": "TextMeshProUGUI", "namespace": "TMPro", "category": "ui",
        "description": "High-quality SDF text rendering for UI. Preferred over legacy Text for all new projects.",
        "aliases": ["textmesh pro", "tmp text", "ui text", "text display", "label", "tmp"],
        "api_methods": ["SetText", "SetColor", "SetFontSize"],
        "requires": ["Canvas", "com.unity.textmeshpro"],
        "capabilities": ["ui", "text"],
        "pipeline": "any",
    },
    {
        "name": "Button", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Clickable UI button that invokes a UnityEvent on click.",
        "aliases": ["ui button", "click button", "menu button", "press button"],
        "api_methods": ["AddListener", "RemoveListener", "SetInteractable", "onClick"],
        "requires": ["Canvas", "Image"],
        "capabilities": ["ui", "interactive"],
        "pipeline": "any",
    },
    {
        "name": "Toggle", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Checkbox/on-off UI element that invokes onValueChanged event.",
        "aliases": ["toggle", "checkbox", "on off switch", "ui toggle"],
        "api_methods": ["SetIsOn", "AddListener"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "interactive"],
        "pipeline": "any",
    },
    {
        "name": "Slider", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Draggable slider for numeric value input. Good for volume, brightness controls.",
        "aliases": ["ui slider", "range slider", "progress slider", "volume slider", "health bar"],
        "api_methods": ["SetValue", "SetMinValue", "SetMaxValue", "AddListener"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "interactive"],
        "pipeline": "any",
    },
    {
        "name": "TMP_InputField", "namespace": "TMPro", "category": "ui",
        "description": "Text input field using TextMeshPro for high-quality rendering.",
        "aliases": ["input field", "text input", "text box", "text entry"],
        "api_methods": ["SetText", "AddListener", "ActivateInputField"],
        "requires": ["Canvas", "com.unity.textmeshpro"],
        "capabilities": ["ui", "interactive", "text"],
        "pipeline": "any",
    },
    {
        "name": "ScrollRect", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Scrollable content container. Used for lists, inventory grids, chat logs.",
        "aliases": ["scroll rect", "scroll view", "scrollable list", "inventory scroll"],
        "api_methods": ["SetNormalizedPosition", "SetVelocity"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "scroll"],
        "pipeline": "any",
    },
    {
        "name": "GridLayoutGroup", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Arranges child RectTransforms in a grid. Used for inventory, character selection.",
        "aliases": ["grid layout", "ui grid", "item grid", "inventory grid"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "layout"],
        "pipeline": "any",
    },
    {
        "name": "VerticalLayoutGroup", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Stacks child elements vertically with optional spacing and padding.",
        "aliases": ["vertical layout", "list layout", "stack vertical"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "layout"],
        "pipeline": "any",
    },
    {
        "name": "HorizontalLayoutGroup", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Arranges child elements in a horizontal row.",
        "aliases": ["horizontal layout", "row layout", "toolbar layout"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "layout"],
        "pipeline": "any",
    },
    {
        "name": "ContentSizeFitter", "namespace": "UnityEngine.UI", "category": "ui",
        "description": "Resizes a RectTransform to fit its content size. Used with layout groups.",
        "aliases": ["content size fitter", "auto size", "dynamic size"],
        "requires": ["Canvas"],
        "capabilities": ["ui", "layout"],
        "pipeline": "any",
    },

    # ── UI Toolkit ────────────────────────────────────────────────────────────
    {
        "name": "UIDocument", "namespace": "UnityEngine.UIElements", "category": "ui_toolkit",
        "description": "Renders a UXML/USS UI Toolkit panel at runtime. Modern alternative to uGUI Canvas.",
        "aliases": ["ui document", "ui toolkit", "uitoolkit panel", "uxml panel"],
        "api_methods": ["SetPanelSettings", "SetVisualTreeAsset"],
        "capabilities": ["ui", "ui_toolkit"],
        "pipeline": "any",
    },

    # ── Timeline & Playables ───────────────────────────────────────────────────
    {
        "name": "PlayableDirector", "namespace": "UnityEngine.Playables", "category": "timeline",
        "description": "Plays a Unity Timeline asset. Essential for cutscenes and sequenced events.",
        "aliases": ["timeline player", "cutscene player", "playable director", "sequence player"],
        "api_methods": ["Play", "Pause", "Stop", "SetTime", "Evaluate"],
        "capabilities": ["timeline", "cutscene"],
        "pipeline": "any",
    },
    {
        "name": "SignalReceiver", "namespace": "UnityEngine.Timeline", "category": "timeline",
        "description": "Receives Signal Emitter events from a Timeline track and fires UnityEvents.",
        "aliases": ["signal receiver", "timeline signal", "cutscene event trigger"],
        "requires": ["PlayableDirector"],
        "capabilities": ["timeline"],
        "pipeline": "any",
    },

    # ── Cinemachine ───────────────────────────────────────────────────────────
    {
        "name": "CinemachineBrain", "namespace": "Cinemachine", "category": "cinemachine",
        "description": "Drives the Unity Camera based on the highest-priority active Virtual Camera. Must be on the Camera.",
        "aliases": ["cinemachine brain", "virtual camera controller", "camera brain"],
        "requires": ["Camera", "com.unity.cinemachine"],
        "api_methods": ["SetDefaultBlend"],
        "capabilities": ["camera", "cinemachine"],
        "pipeline": "any",
    },
    {
        "name": "CinemachineVirtualCamera", "namespace": "Cinemachine", "category": "cinemachine",
        "description": "A virtual camera with Follow and LookAt targets. Most common Cinemachine component.",
        "aliases": ["virtual camera", "vcam", "follow camera", "orbit camera", "cinemachine cam"],
        "requires": ["com.unity.cinemachine"],
        "api_methods": ["SetPriority", "SetFollow", "SetLookAt"],
        "capabilities": ["camera", "cinemachine"],
        "pipeline": "any",
    },
    {
        "name": "CinemachineFreeLook", "namespace": "Cinemachine", "category": "cinemachine",
        "description": "Third-person orbit camera with three configurable rigs (top, mid, bottom).",
        "aliases": ["freelook camera", "third person camera", "orbit camera 3 rig", "freelook"],
        "requires": ["com.unity.cinemachine"],
        "api_methods": ["SetFollow", "SetLookAt", "SetAxis"],
        "capabilities": ["camera", "cinemachine", "tps"],
        "pipeline": "any",
    },
    {
        "name": "CinemachineStateDrivenCamera", "namespace": "Cinemachine", "category": "cinemachine",
        "description": "Selects a child virtual camera based on an Animator state. Good for gameplay state cameras.",
        "aliases": ["state driven camera", "animator camera", "state camera cinemachine"],
        "requires": ["Animator", "com.unity.cinemachine"],
        "api_methods": ["SetPriority"],
        "capabilities": ["camera", "cinemachine"],
        "pipeline": "any",
    },

    # ── Input (new system) ────────────────────────────────────────────────────
    {
        "name": "PlayerInput", "namespace": "UnityEngine.InputSystem", "category": "input",
        "description": "Links an Input Action Asset to a player. Sends messages or invokes events on input.",
        "aliases": ["player input", "input component", "input handler", "game input"],
        "requires": ["com.unity.inputsystem"],
        "api_methods": ["SwitchCurrentControlScheme", "SwitchCurrentActionMap"],
        "capabilities": ["input"],
        "pipeline": "any",
    },
    {
        "name": "PlayerInputManager", "namespace": "UnityEngine.InputSystem", "category": "input",
        "description": "Manages joining and splitting of players at runtime for local multiplayer.",
        "aliases": ["player input manager", "multiplayer input", "local multiplayer", "co-op input"],
        "requires": ["com.unity.inputsystem"],
        "capabilities": ["input", "multiplayer"],
        "pipeline": "any",
    },

    # ── Networking (Netcode for GameObjects) ──────────────────────────────────
    {
        "name": "NetworkObject", "namespace": "Unity.Netcode", "category": "network",
        "description": "Identifies a networked GameObject. Required on all objects synced over the network.",
        "aliases": ["network object", "networked object", "multiplayer object", "synced object"],
        "requires": ["com.unity.netcode.gameobjects"],
        "capabilities": ["network"],
        "pipeline": "any",
    },
    {
        "name": "NetworkTransform", "namespace": "Unity.Netcode.Components", "category": "network",
        "description": "Synchronises Transform (position/rotation/scale) across the network.",
        "aliases": ["network transform", "synced transform", "networked position"],
        "requires": ["NetworkObject", "com.unity.netcode.gameobjects"],
        "capabilities": ["network", "transform"],
        "pipeline": "any",
    },
    {
        "name": "NetworkAnimator", "namespace": "Unity.Netcode.Components", "category": "network",
        "description": "Synchronises Animator parameters and triggers across the network.",
        "aliases": ["network animator", "synced animator", "networked animation"],
        "requires": ["NetworkObject", "Animator", "com.unity.netcode.gameobjects"],
        "capabilities": ["network", "animation"],
        "pipeline": "any",
    },
    {
        "name": "NetworkRigidbody", "namespace": "Unity.Netcode.Components", "category": "network",
        "description": "Synchronises Rigidbody physics state across the network.",
        "aliases": ["network rigidbody", "synced physics", "networked rigidbody"],
        "requires": ["NetworkObject", "Rigidbody", "com.unity.netcode.gameobjects"],
        "capabilities": ["network", "physics"],
        "pipeline": "any",
    },

    # ── Terrain ────────────────────────────────────────────────────────────────
    {
        "name": "Terrain", "namespace": "UnityEngine", "category": "terrain",
        "description": "Renders a heightmap-based terrain with paint-able detail, trees and grass.",
        "aliases": ["terrain", "landscape", "ground mesh", "heightmap"],
        "api_methods": ["SetHeights", "SetAlphamaps", "SampleHeight"],
        "capabilities": ["terrain"],
        "pipeline": "any",
    },
    {
        "name": "TerrainCollider", "namespace": "UnityEngine", "category": "terrain",
        "description": "Physics collider that matches the Terrain heightmap. Required alongside Terrain.",
        "aliases": ["terrain collider", "ground collider", "landscape collider"],
        "requires": ["Terrain"],
        "capabilities": ["collider", "terrain"],
        "pipeline": "any",
    },

    # ── XR ────────────────────────────────────────────────────────────────────
    {
        "name": "XROrigin", "namespace": "Unity.XR.CoreUtils", "category": "xr",
        "description": "Sets the XR tracking origin. Required for any AR/VR project using the XR framework.",
        "aliases": ["xr origin", "vr origin", "ar origin", "tracking origin"],
        "requires": ["com.unity.xr.coreutils"],
        "capabilities": ["xr", "vr", "ar"],
        "pipeline": "any",
    },
    {
        "name": "ActionBasedController", "namespace": "UnityEngine.XR.Interaction.Toolkit", "category": "xr",
        "description": "Maps XR controller inputs to XR Interaction Toolkit actions.",
        "aliases": ["xr controller", "vr controller", "hand controller", "xr input controller"],
        "requires": ["com.unity.xr.interaction.toolkit"],
        "capabilities": ["xr", "input"],
        "pipeline": "any",
    },

    # ── Scripting (abstract / meta) ────────────────────────────────────────────
    {
        "name": "MonoBehaviour", "namespace": "UnityEngine", "category": "scripting",
        "description": "Base class for all user-written Unity scripts. Provides lifecycle hooks.",
        "aliases": ["script", "custom script", "monobehaviour", "behaviour", "component script"],
        "api_methods": ["Start", "Update", "FixedUpdate", "LateUpdate", "Awake", "OnEnable", "OnDisable", "OnDestroy", "OnTriggerEnter", "OnCollisionEnter", "StartCoroutine"],
        "capabilities": ["scripting"],
        "pipeline": "any",
    },
]
# fmt: on


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = sum(1 for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip())
        print(f"[unity_components] already present ({count} entries). Use --force to re-scrape.")
        return count

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with OUT.open("w", encoding="utf-8") as f:
        for raw in _COMPONENTS:
            try:
                entry = make_component_entry(raw)
                f.write(entry.to_jsonl() + "\n")
                ok += 1
            except Exception as e:
                print(f"  [skip] {raw.get('name','?')}: {e}", file=sys.stderr)
                err += 1

    print(f"[unity_components] {ok} written, {err} skipped --> {OUT}")
    return ok


if __name__ == "__main__":
    run(force="--force" in sys.argv)
