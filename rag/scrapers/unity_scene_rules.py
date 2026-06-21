"""
Unity scene grammar rule corpus.

These constraints prevent the LLM from generating broken scene setups:
missing cameras, render pipeline mismatches, physics gaps, UI failures, etc.
"""
import sys
from pathlib import Path

from ..schema.unity_scene_rule import make_scene_rule_entry

OUT = Path(__file__).parent.parent / "corpus" / "unity" / "scene_rules.jsonl"

# fmt: off
_RULES: list[dict] = [
    # ── Camera ────────────────────────────────────────────────────────────────
    {
        "id": "requires_main_camera",
        "label": "Scene Requires a Main Camera",
        "rule_type": "required",
        "pipeline": "any",
        "description": "Every scene must have at least one Camera tagged 'MainCamera'.",
        "violation": "Black screen / no render output at runtime.",
        "fix": "Add a Camera GameObject and set tag to 'MainCamera'.",
        "affects": ["camera", "rendering"],
        "aliases": ["main camera required", "camera missing", "no camera in scene"],
    },
    {
        "id": "audio_listener_one_active",
        "label": "Only One AudioListener Active",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Exactly one AudioListener should be active at any time. Multiple listeners cause warnings and distortion.",
        "violation": "Unity warning: 'There are 2 audio listeners in the scene'. Possible audio distortion.",
        "fix": "Disable or remove extra AudioListeners. Keep only the one on the Main Camera.",
        "affects": ["audio", "camera"],
        "aliases": ["duplicate audio listener", "multiple audio listeners"],
    },
    {
        "id": "cinemachine_requires_brain",
        "label": "Cinemachine Requires Brain on Main Camera",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A CinemachineBrain must be on the Camera for Virtual Cameras to drive it.",
        "violation": "Virtual Cameras exist but the Camera ignores them. No camera movement.",
        "fix": "Add CinemachineBrain component to the Main Camera GameObject.",
        "affects": ["camera", "cinemachine"],
        "aliases": ["cinemachine brain missing", "virtual camera not working", "cinemachine setup"],
    },
    {
        "id": "camera_depth_ordering",
        "label": "Camera Depth Ordering",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "When using multiple cameras (e.g. UI overlay camera + world camera), depths must be distinct. Higher depth renders on top.",
        "violation": "Cameras with equal depth: undefined render order; flickering or missing layers.",
        "fix": "Set unique Depth values. World camera: -1, UI camera: 0 is a common convention.",
        "affects": ["camera", "rendering", "ui"],
        "aliases": ["camera depth", "multiple cameras", "camera layer order"],
    },

    # ── Render Pipeline ────────────────────────────────────────────────────────
    {
        "id": "urp_requires_pipeline_asset",
        "label": "URP Requires UniversalRenderPipelineAsset",
        "rule_type": "required",
        "pipeline": "urp",
        "description": "URP projects must have a UniversalRenderPipelineAsset assigned in Project Settings > Graphics.",
        "violation": "Pink/magenta materials; renderer won't initialize; shaders won't compile.",
        "fix": "Create a UniversalRenderPipelineAsset and assign it in Graphics settings.",
        "affects": ["rendering", "shaders"],
        "aliases": ["urp setup", "urp missing asset", "pink materials"],
    },
    {
        "id": "hdrp_requires_pipeline_asset",
        "label": "HDRP Requires HDRenderPipelineAsset",
        "rule_type": "required",
        "pipeline": "hdrp",
        "description": "HDRP projects must have an HDRenderPipelineAsset assigned in Project Settings > Graphics.",
        "violation": "Black or pink render. HDRP volume effects don't work.",
        "fix": "Create an HDRenderPipelineAsset and assign it in Graphics settings.",
        "affects": ["rendering", "shaders"],
        "aliases": ["hdrp setup", "hdrp missing asset"],
    },
    {
        "id": "urp_hdrp_cannot_mix",
        "label": "URP and HDRP Cannot Mix",
        "rule_type": "forbidden",
        "pipeline": "any",
        "description": "A single project cannot use both URP and HDRP simultaneously.",
        "violation": "Shader compilation errors; render pipeline conflicts; package conflicts.",
        "fix": "Choose one render pipeline per project. Migrating between them requires re-authoring materials.",
        "affects": ["rendering"],
        "aliases": ["urp hdrp mix", "render pipeline conflict"],
    },
    {
        "id": "builtin_shaders_incompatible_urp",
        "label": "Built-in Shaders Are Incompatible with URP/HDRP",
        "rule_type": "warning",
        "pipeline": "urp",
        "description": "Standard/Legacy shaders (Standard, Diffuse, Specular) do not render in URP/HDRP.",
        "violation": "Pink/magenta materials on objects using Standard shader.",
        "fix": "Use Universal Render Pipeline/Lit shader (URP) or HDRP/Lit (HDRP) instead.",
        "affects": ["rendering", "shaders", "materials"],
        "aliases": ["standard shader urp", "pink material urp", "shader incompatible"],
    },
    {
        "id": "urp_post_processing_volume_required",
        "label": "URP Post-Processing Requires a Volume",
        "rule_type": "required",
        "pipeline": "urp",
        "description": "Post-processing effects (bloom, DoF, tone-mapping) in URP require a Volume component with a VolumeProfile.",
        "violation": "Post-processing effects are invisible; no bloom/DoF in scene.",
        "fix": "Add a Global Volume (or trigger Volume) with a VolumeProfile asset. Enable Post Processing on the camera.",
        "affects": ["rendering", "postprocess"],
        "aliases": ["post processing not working", "bloom not visible", "volume missing"],
    },
    {
        "id": "urp_camera_post_processing_enabled",
        "label": "URP Camera Must Have Post Processing Enabled",
        "rule_type": "required",
        "pipeline": "urp",
        "description": "In URP, the Camera component has a 'Post Processing' checkbox that must be enabled.",
        "violation": "Volume effects defined but not visible on the camera's output.",
        "fix": "Enable 'Post Processing' in the Universal Additional Camera Data component on the Camera.",
        "affects": ["rendering", "camera", "postprocess"],
        "aliases": ["urp post processing camera", "post processing camera checkbox"],
    },

    # ── Physics ────────────────────────────────────────────────────────────────
    {
        "id": "rigidbody_needs_collider",
        "label": "Rigidbody Needs a Collider for Physics",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A Rigidbody without a Collider will fall through all geometry.",
        "violation": "Object falls through the floor; no collision detection.",
        "fix": "Add a matching Collider (BoxCollider, CapsuleCollider, etc.) to the same or child GameObject.",
        "affects": ["physics"],
        "aliases": ["no collider rigidbody", "fall through floor", "rigidbody setup"],
    },
    {
        "id": "static_collider_no_rigidbody",
        "label": "Static Colliders Must Not Have a Rigidbody",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "A Collider on a moving GameObject without a Rigidbody is a 'static collider'. Moving it every frame is very expensive.",
        "violation": "Severe performance drop; Unity rebuilds the static collision tree every frame.",
        "fix": "Add a Rigidbody (set to kinematic if script-driven) to any moved Collider.",
        "affects": ["physics", "performance"],
        "aliases": ["moving static collider", "performance physics warning"],
    },
    {
        "id": "navmesh_must_be_baked",
        "label": "NavMeshAgent Requires a Baked NavMesh Surface",
        "rule_type": "required",
        "pipeline": "any",
        "description": "NavMeshAgent pathfinding only works after the NavMesh has been baked for the scene geometry.",
        "violation": "Agent stays still; SetDestination has no effect; console error 'No NavMesh in scene'.",
        "fix": "Bake NavMesh: Window > AI > Navigation > Bake. Or use NavMeshSurface.BuildNavMesh() at runtime.",
        "affects": ["navigation", "ai"],
        "aliases": ["navmesh not baked", "ai not moving", "navmesh error"],
    },
    {
        "id": "physics_layers_must_be_defined",
        "label": "Physics Layer Masks Must Reference Defined Layers",
        "rule_type": "required",
        "pipeline": "any",
        "description": "LayerMask values must reference layers defined in Project Settings > Tags and Layers.",
        "violation": "Raycast ignores intended objects; collision filtering broken.",
        "fix": "Define all needed layers in Tags and Layers settings. Layers 0–7 are reserved; custom layers start at 8.",
        "affects": ["physics"],
        "aliases": ["layer mask", "layer undefined", "physics layer"],
    },
    {
        "id": "character_controller_vs_rigidbody",
        "label": "CharacterController and Rigidbody Must Not Coexist",
        "rule_type": "forbidden",
        "pipeline": "any",
        "description": "A CharacterController and Rigidbody on the same GameObject cause physics conflicts.",
        "violation": "Unpredictable movement; jitter; the character may clip through geometry.",
        "fix": "Use either CharacterController OR Rigidbody for the character, not both.",
        "affects": ["physics", "character"],
        "aliases": ["character controller rigidbody conflict", "physics character conflict"],
    },

    # ── UI ─────────────────────────────────────────────────────────────────────
    {
        "id": "ui_requires_event_system",
        "label": "UI Interaction Requires an EventSystem",
        "rule_type": "required",
        "pipeline": "any",
        "description": "All uGUI interactive elements (Button, Toggle, InputField) require an EventSystem in the scene.",
        "violation": "Buttons are visible but clicking does nothing; no hover state; no input events.",
        "fix": "Add a GameObject with an EventSystem component. Unity adds one automatically when adding a Canvas.",
        "affects": ["ui"],
        "aliases": ["button not working", "ui click not working", "event system missing"],
    },
    {
        "id": "canvas_requires_graphic_raycaster",
        "label": "Canvas Needs GraphicRaycaster for Pointer Events",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A Canvas requires a GraphicRaycaster to detect pointer/touch input on its UI elements.",
        "violation": "UI visible but non-interactive; click events never fire.",
        "fix": "Add GraphicRaycaster to the Canvas GameObject.",
        "affects": ["ui"],
        "aliases": ["canvas not clickable", "graphic raycaster missing"],
    },
    {
        "id": "world_space_canvas_requires_camera",
        "label": "World Space Canvas Must Reference an Event Camera",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A Canvas in World Space render mode needs an 'Event Camera' assigned to receive pointer events.",
        "violation": "World-space UI elements are not interactable; clicks don't register.",
        "fix": "Assign the scene's Main Camera to the 'Event Camera' field on the Canvas.",
        "affects": ["ui", "camera"],
        "aliases": ["world space canvas", "3d ui not clickable", "world ui camera"],
    },
    {
        "id": "ui_elements_must_be_under_canvas",
        "label": "uGUI Elements Must Be Under a Canvas",
        "rule_type": "required",
        "pipeline": "any",
        "description": "All uGUI components (Image, Text, Button) must be children of a Canvas GameObject.",
        "violation": "UI elements invisible; Unity console error: 'UI elements must be under a Canvas'.",
        "fix": "Parent all UI GameObjects inside a Canvas in the hierarchy.",
        "affects": ["ui"],
        "aliases": ["ui not visible", "image not showing", "canvas parent required"],
    },

    # ── Audio ──────────────────────────────────────────────────────────────────
    {
        "id": "audio_listener_on_camera",
        "label": "AudioListener Typically Belongs on the Camera",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "3D audio spatialization is calculated relative to the AudioListener's position. If misplaced, 3D audio will sound wrong.",
        "violation": "3D audio panning/attenuation calculated from wrong position; immersion broken.",
        "fix": "Keep AudioListener on the Main Camera (or the player's head transform for VR).",
        "affects": ["audio", "camera"],
        "aliases": ["3d audio wrong", "spatial audio broken", "audio listener position"],
    },
    {
        "id": "audio_clip_must_be_assigned",
        "label": "AudioSource Clip Must Be Assigned Before Play()",
        "rule_type": "required",
        "pipeline": "any",
        "description": "Calling AudioSource.Play() with no clip assigned does nothing and logs a warning.",
        "violation": "Silent AudioSource; no sound plays despite calling Play().",
        "fix": "Assign an AudioClip asset to AudioSource.clip before calling Play(), or use PlayOneShot(clip).",
        "affects": ["audio"],
        "aliases": ["audio not playing", "audioclip not assigned", "silent audio"],
    },

    # ── Animation ─────────────────────────────────────────────────────────────
    {
        "id": "animator_requires_controller",
        "label": "Animator Requires an AnimatorController Asset",
        "rule_type": "required",
        "pipeline": "any",
        "description": "An Animator component with no Controller assigned does nothing.",
        "violation": "No animation plays; Animator.SetTrigger/SetBool have no effect.",
        "fix": "Create an AnimatorController asset and assign it to the Animator's Controller field.",
        "affects": ["animation"],
        "aliases": ["animator no controller", "animation not playing", "animator empty"],
    },
    {
        "id": "animator_humanoid_rig_for_retarget",
        "label": "Humanoid Rig Required for Animation Retargeting",
        "rule_type": "required",
        "pipeline": "any",
        "description": "To retarget animations between different character models, both the model and the animation clips must use the Humanoid rig type.",
        "violation": "Animation retargeting fails; character T-poses or deforms incorrectly.",
        "fix": "Import model with Rig > Animation Type = Humanoid. Set the same for animation clips.",
        "affects": ["animation", "character"],
        "aliases": ["humanoid rig", "animation retargeting", "character animation rig"],
    },

    # ── Networking ─────────────────────────────────────────────────────────────
    {
        "id": "netcode_network_object_required",
        "label": "Networked Prefabs Must Have NetworkObject",
        "rule_type": "required",
        "pipeline": "any",
        "description": "Any prefab spawned over the network via Netcode for GameObjects must have a NetworkObject component at its root.",
        "violation": "NetworkManager.Spawn() fails; objects not synced across clients.",
        "fix": "Add NetworkObject to the root of every networked prefab. Register it in NetworkManager's NetworkPrefabs list.",
        "affects": ["network"],
        "aliases": ["network object missing", "netcode spawn fail", "multiplayer prefab"],
    },
    {
        "id": "netcode_network_manager_singleton",
        "label": "Scene Must Have Exactly One NetworkManager",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A Netcode scene requires exactly one NetworkManager in the scene or persistent scenes.",
        "violation": "Multiple NetworkManagers conflict; singleton errors; clients can't connect.",
        "fix": "Place a single NetworkManager in an initial/boot scene that persists with DontDestroyOnLoad.",
        "affects": ["network"],
        "aliases": ["network manager missing", "netcode setup", "multiplayer manager"],
    },

    # ── Lighting ──────────────────────────────────────────────────────────────
    {
        "id": "realtime_gi_expensive",
        "label": "Realtime Global Illumination Is Very Expensive",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Unity's Realtime GI (Enlighten) has high CPU cost. Not suitable for mobile or VR.",
        "violation": "Frame rate drops; device overheating on mobile/VR; editor bake times very long.",
        "fix": "Use Baked GI (Lightmapping) for static objects. Use Light Probes and Reflection Probes for dynamic objects.",
        "affects": ["rendering", "lighting", "performance"],
        "aliases": ["realtime gi", "global illumination performance", "enlighten slow"],
    },
    {
        "id": "baked_light_requires_static_flag",
        "label": "Baked Lighting Only Affects Static Objects",
        "rule_type": "required",
        "pipeline": "any",
        "description": "Lightmapping only bakes lighting onto GameObjects marked as 'Static' (or Contribute GI).",
        "violation": "Dynamic objects appear unlit in baked light scenes; shadows missing on non-static objects.",
        "fix": "Mark all stationary level geometry as Static in the Inspector before baking. Use Light Probes for characters.",
        "affects": ["rendering", "lighting"],
        "aliases": ["baked light not working", "static flag lighting", "lightmap static"],
    },
    {
        "id": "shadow_distance_must_be_set",
        "label": "Shadow Distance Must Be Configured",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Default shadow distance may clip shadows too close or too far for the scene scale.",
        "violation": "Shadows disappear at unexpected distances; shadow quality looks wrong.",
        "fix": "Set Quality Settings > Shadows > Shadow Distance appropriate to your world scale (e.g., 100m for outdoor scenes).",
        "affects": ["rendering", "lighting"],
        "aliases": ["shadow distance", "shadows disappearing", "shadow quality"],
    },

    # ── Performance ────────────────────────────────────────────────────────────
    {
        "id": "draw_calls_batching",
        "label": "Unbatched Materials Increase Draw Calls",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Each unique material instance creates at least one draw call. Hundreds of unique materials tank performance.",
        "violation": "High draw call count; poor performance especially on mobile.",
        "fix": "Use Material instancing (GPU Instancing), Static Batching for static objects, or combine meshes/atlases.",
        "affects": ["rendering", "performance"],
        "aliases": ["draw calls", "batching", "performance materials"],
    },
    {
        "id": "find_calls_expensive",
        "label": "GameObject.Find() Is Expensive in Update()",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "GameObject.Find() and FindWithTag() iterate all scene objects. Calling them in Update() causes significant GC pressure.",
        "violation": "Garbage collection spikes; poor frame rate; profiler shows object.Find() in hot path.",
        "fix": "Cache results in Awake() or Start(). Never call Find() in Update().",
        "affects": ["performance", "scripting"],
        "aliases": ["find expensive", "gameobject.find performance", "cache component"],
    },
    {
        "id": "destroy_creates_garbage",
        "label": "Frequent Instantiate/Destroy Creates GC Garbage",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Frequent Instantiate and Destroy calls cause GC allocations which trigger garbage collection pauses.",
        "violation": "Frame rate spikes; GC pauses visible in profiler; stuttering on bullet/particle spawns.",
        "fix": "Use Object Pooling for frequently spawned/destroyed objects (bullets, particles, enemies).",
        "affects": ["performance", "scripting"],
        "aliases": ["object pooling", "instantiate performance", "gc alloc"],
    },

    # ── Layer / Tag ────────────────────────────────────────────────────────────
    {
        "id": "layer_0_to_31_only",
        "label": "Layer Values Must Be in Range 0–31",
        "rule_type": "required",
        "pipeline": "any",
        "description": "Unity supports exactly 32 layers (0 to 31). Indices outside this range are invalid.",
        "violation": "LayerMask bitwise operations return incorrect results; physics ignores collisions.",
        "fix": "Define all needed layers in Tags and Layers (max 32). Use LayerMask.LayerToName() for safety.",
        "affects": ["physics", "rendering"],
        "aliases": ["layer index out of range", "layermask error"],
    },
    {
        "id": "player_tag_must_be_defined",
        "label": "The 'Player' Tag Must Be Defined Before Use",
        "rule_type": "required",
        "pipeline": "any",
        "description": "FindWithTag('Player') throws an error if the 'Player' tag is not defined in the project.",
        "violation": "UnityException: Tag Player is not defined. Crash or incorrect query result.",
        "fix": "Add 'Player' (and any other needed tags) in Tags and Layers settings.",
        "affects": ["scripting"],
        "aliases": ["player tag", "tag not defined", "tag error"],
    },

    # ── Input ──────────────────────────────────────────────────────────────────
    {
        "id": "new_input_system_legacy_conflict",
        "label": "New and Legacy Input Systems Must Not Both Be Active",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "Having both Input System packages active causes double-processing and unexpected axis values.",
        "violation": "Input.GetAxis returns 0 when using new system; input events fire twice.",
        "fix": "Set Project Settings > Player > Active Input Handling to 'Input System Package (New)' exclusively.",
        "affects": ["input"],
        "aliases": ["input system conflict", "old new input", "input axis zero"],
    },
    {
        "id": "player_input_requires_actions_asset",
        "label": "PlayerInput Requires an Input Actions Asset",
        "rule_type": "required",
        "pipeline": "any",
        "description": "The PlayerInput component does nothing without an Input Actions asset assigned.",
        "violation": "No input events; button presses have no effect.",
        "fix": "Create an Input Actions asset (Create > Input Actions), define your actions, and assign it to PlayerInput.",
        "affects": ["input"],
        "aliases": ["player input not working", "input actions missing"],
    },

    # ── Terrain ────────────────────────────────────────────────────────────────
    {
        "id": "terrain_requires_terrain_layer",
        "label": "Terrain Must Have At Least One Terrain Layer",
        "rule_type": "required",
        "pipeline": "any",
        "description": "A Terrain with no Terrain Layers assigned renders solid magenta/pink.",
        "violation": "Magenta terrain; no texture painted on terrain.",
        "fix": "Create a Terrain Layer asset (Create > Terrain Layer) and add it in the Terrain's Paint Texture tool.",
        "affects": ["terrain", "rendering"],
        "aliases": ["terrain layer missing", "terrain pink", "terrain texture"],
    },

    # ── XR ─────────────────────────────────────────────────────────────────────
    {
        "id": "xr_requires_xr_origin",
        "label": "XR Projects Must Have an XROrigin",
        "rule_type": "required",
        "pipeline": "any",
        "description": "XR tracking (VR/AR) requires an XROrigin component that maps physical tracking to scene space.",
        "violation": "Camera doesn't track headset; no head or controller tracking.",
        "fix": "Add XROrigin to the scene (from the XR Interaction Toolkit prefabs). Remove the regular Camera and use the one inside XROrigin.",
        "affects": ["xr", "camera"],
        "aliases": ["vr not tracking", "xr origin missing", "headset tracking"],
    },
    {
        "id": "xr_audio_listener_on_camera_offset",
        "label": "XR AudioListener Should Be on the Camera Offset, Not the Scene Root",
        "rule_type": "warning",
        "pipeline": "any",
        "description": "In XR, the AudioListener must track head position. Place it on the 'Camera' child of XROrigin.",
        "violation": "3D audio spatialization is wrong; sounds come from wrong direction in VR.",
        "fix": "Move AudioListener from the XROrigin root to its 'Camera Offset > Main Camera' child.",
        "affects": ["xr", "audio"],
        "aliases": ["vr audio wrong", "xr audio listener"],
    },
]
# fmt: on


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = sum(1 for line in OUT.read_text(encoding="utf-8").splitlines() if line.strip())
        print(f"[unity_scene_rules] already present ({count} entries). Use --force to re-scrape.")
        return count

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with OUT.open("w", encoding="utf-8") as f:
        for raw in _RULES:
            try:
                entry = make_scene_rule_entry(raw)
                f.write(entry.to_jsonl() + "\n")
                ok += 1
            except Exception as e:
                print(f"  [skip] {raw.get('id','?')}: {e}", file=sys.stderr)
                err += 1

    print(f"[unity_scene_rules] {ok} written, {err} skipped --> {OUT}")
    return ok


if __name__ == "__main__":
    run(force="--force" in sys.argv)
