"""
LLM prompt templates for the Unity Project Creator pipeline.
Same philosophy as GTA5: LLM = storyteller/specifier, pipeline = deterministic compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# ── STEP 2: Project concept ───────────────────────────────────────────────────

UNITY_CONCEPT_SYSTEM = """\
You are a Unity game project architect. Describe game projects in concrete technical terms.
No code. No JSON. No package IDs.
Be specific: name the game type, camera perspective, player mechanics, key systems, \
and what packages are needed. Think like a lead developer writing a project brief."""

UNITY_CONCEPT_USER = """\
{theme_block}
Write a Unity project concept brief based on this idea:

"{user_prompt}"

Cover in 2-3 paragraphs:
- Game type (2D, 2.5D, 3D, VR, top-down, isometric) and target platform
- Core player mechanics (movement style, camera, input)
- Key systems needed (physics, UI, audio, AI, networking, etc.)
- Recommended render pipeline (URP for most games, HDRP for high-end 3D, builtin for simple 2D)
- Unity version recommendation (Unity 6 — 6000.0 LTS is the default; 2022.3 only if the project specifically requires it)
- Specific packages or tools that would be useful (Cinemachine, Input System, NavMesh, Tilemaps, etc.)

Do NOT write any code, JSON, or package IDs."""

# ── STEP 3: Schema fill ───────────────────────────────────────────────────────

UNITY_SCHEMA_FILL_SYSTEM = """\
You are a Unity project schema designer.
Output ONLY valid JSON — no explanation, no markdown fences.
Use plain English for all description fields; the pipeline resolves them to package IDs.
The schema is the project's FULL design, not just its settings: alongside the
scenes/scripts/entities you also author the game's characters, objectives, sound,
surfaces and effects. Every field you fill is compiled into a real asset or
subsystem; every field you leave empty falls back to a generic default."""

UNITY_SCHEMA_FILL_USER = """\
{schema_hints}
Convert this Unity project concept into the schema below.
Use plain English for every "description" field (e.g. "universal render pipeline", "camera tracking").

LIMITS (keep JSON concise):
- packages:          max 10 entries (only what's genuinely needed)
- scripts:           max 24 entries
- scenes:            max 8 scene names (no paths, just names like "Main", "MainMenu", "Level1")
- scene_specs:       max 8 — ONE PER GAMEPLAY SCENE. What makes each scene distinct:
                     {{"name": "<must match a scenes entry>", "description": "one line",
                      "time_of_day": "dawn|day|sunset|night", "fog": true/false,
                      "entities": ["<entity names active in this scene>"]}}
                     Omit "entities" to spawn every group there; use it whenever a
                     scene should host a SUBSET (a boss scene, an empty menu-like hub).
- story_beats:       max 8 entries (omit array if no story-beat progression is needed)
- audio_tracks:      max 8 entries (omit array if no music/audio design is needed)
- procedural_meshes: max 4 entries (omit array if no runtime geometry is needed)
- entities:          max 40 groups — THE SCENE CONTENT. Use this for every group of
                     objects the concept asks for (e.g. "6 rival cars", "a ring of
                     buildings", "a crowd"). Each becomes `count` primitives in the scene.
- features:          only from the allowed list

GAME TYPES — use only one of:
  2D | 2.5D | 3D | VR | top_down | 2d_topdown | isometric
  top_down = real 3D models seen from above (a dungeon-crawler/twin-stick game
  with actual 3D geometry and NavMesh AI). 2d_topdown = flat 2D sprites/physics
  seen from above (a classic Zelda-like — free 8-directional movement, no
  gravity, no jump). Pick 2d_topdown only when the concept is explicitly flat
  2D; default to top_down for anything with real 3D models.

LOCOMOTION — how the player moves. Use only one of:
  walk | drive | fly | swim
  IMPORTANT: racing / driving games MUST set "drive" (the player becomes a real
  physics car with suspension, handbrake drift, headlights). Flying games "fly";
  underwater "swim"; everything else "walk".

ENVIRONMENT — scene mood. If the prompt names a time of day or weather feel, set:
  time_of_day: dawn | day | sunset | night   (drives the sun's angle and color)
  fog: true|false; fog_color: "#RRGGBB"; sun_intensity: 0.0-3.0 (or omit for preset)
  e.g. "a mountain road at sunset with fog" → {{"time_of_day": "sunset", "fog": true}}

PLATFORMS — use only one of:
  pc | mobile | console | webgl | all

RENDER PIPELINES — use only one of:
  URP | HDRP | builtin
  (URP = Universal, good for most; HDRP = High Definition, AAA PC/console; builtin = legacy)

VISUAL STYLES — use only one of:
  cartoon | realistic | low_poly | stylized | pixel_art | retro
  (drives lighting, color palette defaults, and material smoothness)

FEATURE FLAGS — use only these (as many as apply):
  physics | ui_toolkit | input_system | cinemachine | tilemaps | navmesh |
  audio | networking | addressables | localization | timeline | animation_rigging |
  inventory | dialogue | npc_dialog | vfx | scene_transition
  (npc_dialog = spoken/voiced NPCs; vfx = particle/impact effects;
   scene_transition = multi-scene projects that fade/load between scenes)

SCRIPT TYPES — use only one per script:
  player | camera | game_manager | enemy | collectible | ui_manager | audio_manager | health |
  dialogue_manager | mesh_builder | procedural_mesh | level_generator | material_palette | custom

CUSTOM SCRIPTS — how concepts BEYOND the fixed types reach the project.
  The non-custom types render proven baseline templates. For every behavior the prompt
  asks for that no type covers (drift assist, headlight toggle, day/night cycle, popping
  enemies, weather damage…), add a script with "type": "custom" and AUTHOR the behavior
  yourself in these extra fields (pure C#, Unity 6):
    attach_to:          player | camera | game_manager | systems  (which GameObject hosts it)
    extra_usings:       extra namespaces, e.g. "UnityEngine.InputSystem" (no "using", no ";")
    extra_fields:       [SerializeField] declarations: {{"name","type","default","header","tooltip"}}
    awake_extra:        C# statements inside Awake()
    update_extra:       C# statements inside Update()
    fixed_update_extra: C# statements inside FixedUpdate() (omit if not needed)
    extra_methods:      helpers: {{"signature": "private void Foo()", "body": "..."}} (no braces in body)
  Example — "headlights toggle with H":
    {{"name": "HeadlightToggle", "type": "custom", "attach_to": "player",
      "description": "Toggles headlight spotlights with the H key.",
      "extra_usings": ["UnityEngine.InputSystem", "System.Collections.Generic"],
      "extra_fields": [
        {{"name": "lightIntensity", "type": "float", "default": "2.5f", "header": "Headlights"}},
        {{"name": "_lights", "type": "List<Light>", "default": "new List<Light>()"}}
      ],
      "awake_extra": "for (int i = -1; i <= 1; i += 2) {{ var go = new GameObject(\\"Headlight\\"); go.transform.SetParent(transform, false); go.transform.localPosition = new Vector3(0.4f * i, 0.5f, 1.1f); var l = go.AddComponent<Light>(); l.type = LightType.Spot; l.range = 30f; l.intensity = lightIntensity; l.enabled = false; _lights.Add(l); }}",
      "update_extra": "if (Keyboard.current != null && Keyboard.current.hKey.wasPressedThisFrame) foreach (var l in _lights) l.enabled = !l.enabled;",
      "extra_methods": []}}
  CRITICAL: every identifier your fragments reference MUST be declared in extra_fields
  (each becomes a private member) or be a local declared inside the fragment itself.
  Referencing an undeclared variable (e.g. using `wheelSpeed` without declaring it)
  FAILS COMPILATION. extra_usings takes NAMESPACES only (UnityEngine.InputSystem,
  System.Collections.Generic) — never type names like AudioSource or ParticleSystem.
  Reference other scripts via GetComponent / FindFirstObjectByType. Keep fragments under
  ~40 lines each. C# only — never Python.
  extra_fields TYPES MUST BE DATA ONLY — a Unity/C# builtin (float, int, bool, string,
  Vector3, GameObject, Transform, List<T> of those, etc.) or, at most, a bare data name
  with NO methods of its own (an unrecognized type name auto-generates an EMPTY stub
  class purely to avoid a compile error — it has no members). NEVER invent a type and
  then call methods on it (currentState.Update(), enemy.NextState(), item.Use()) or
  assign it an enum-like bare-word default (= Idle, = Attacking) — nothing you didn't
  spell out in extra_fields/extra_methods exists, so both fail compilation. If a prompt
  wants a state machine (idle/walk/attack/etc.), do NOT model it as a custom object
  with behavior — use a plain string or int field ("state") and drive transitions with
  if/else directly inside update_extra.
  INPUT SYSTEM: there is no `InputSystem.current` — the static accessors are per-device:
  `Mouse.current`, `Keyboard.current`, `Gamepad.current`, `Touchscreen.current` (each
  null-checked the same way, e.g. `Mouse.current != null && Mouse.current.leftButton.wasPressedThisFrame`).
  PHYSICS.RAYCAST: `Physics.Raycast(...)` returns a bool — the hit DETAILS live in the
  `out RaycastHit` parameter, not on the bool. `var hit = Physics.Raycast(origin, dir,
  out RaycastHit hitInfo, dist)`, then use `hitInfo.point`, `hitInfo.collider`,
  `hitInfo.normal` — never `hit.point` or `hit.collider` (hit is a bool, it has no members).

ENTITIES — the actual objects in the scene, built from primitives so the concept SHOWS UP.
  Populate one group per distinct set of objects the prompt implies. Fields:
    shape:      cube | sphere | capsule | cylinder | quad | plane
    placement:  single | line | ring | grid | scatter | path — "single" is for count=1
      ONLY (a real model spawned once); a group with count>1 needs a real spread
      (line/ring/grid/scatter/path), or it gets auto-upgraded and a fix is logged.
    color_role: primary | secondary | accent | trim | background  (picks a palette color)
    count:      how many (e.g. 6 rivals → 6); tag: e.g. "Enemy" so AI/health find them
    behavior:   OPTIONAL — the PascalCase name of one of your `scripts` to attach to each
      (leave "" for kind="enemy" and the compiler auto-attaches the composed AI script;
      for any other kind, "" means no behavior script is attached)
    model_hint: 2-5 plain words naming the PHYSICAL OBJECT ("pine tree", "wooden ramp",
      "hanging banner") — matches the group to a real 3D model from the asset library,
      so choose words a model file would be named after. Set it on EVERY entity group.
    spacing/radius: MINIMUMS, not exact values — the engine widens them at spawn time
      to fit each group's actual model size, so instances never overlap. Guess a
      reasonable value; you do not need to know the model's real footprint.
    scale:      a RELATIVE multiplier on the model's own natural size (1 = as authored),
      not an absolute world size — the engine ground-snaps every instance to sit on
      the floor regardless of the model's pivot, so you don't need to hand-tune height.
    path_points: for placement "path" ONLY — up to 64 [x, y, z] world points. y is
      ELEVATION, so author hills by varying y. An optional 4th number per point is
      BANK: degrees of roll about the direction of travel (max ±45; negative leans
      the surface LEFT, positive RIGHT), smoothly interpolated between points — use
      it for banked corners and off-camber sections, e.g. [60, 5, 60, -12]. Instances
      are laid evenly along the polyline, AND a "Waypoints" container is emitted at
      the points — AI racers / patrollers follow it. Use a path for any road, track,
      route, river, or wall line.
  Example: "a race with 6 rivals on a winding road, grandstands, and a crowd" →
    [{{"name":"RoadSegments","kind":"road","count":40,"shape":"cube","placement":"path","color_role":"secondary","scale":4,
       "path_points":[[0,0,0],[40,2,20],[60,5,60],[30,8,90],[-20,6,80],[-40,3,40],[-20,1,10],[0,0,0]]}},
     {{"name":"Rivals","kind":"enemy","count":6,"shape":"capsule","placement":"line","color_role":"accent","tag":"Enemy","model_hint":"rival race car"}},
     {{"name":"Stands","kind":"building","count":8,"shape":"cube","placement":"ring","color_role":"secondary","scale":3,"model_hint":"grandstand structure"}},
     {{"name":"Crowd","kind":"crowd","count":40,"shape":"sphere","placement":"grid","color_role":"trim","scale":0.6,"model_hint":"spectator character"}}]

PROJECT CONCEPT:
{concept}

OUTPUT this exact JSON structure, fully populated:
{{
  "project_name": "PascalCase name, no spaces",
  "company_name": "Your studio name",
  "description": "One sentence description",
  "game_type": "one of: 2D | 2.5D | 3D | VR | top_down | 2d_topdown | isometric",
  "platform": "one of: pc | mobile | console | webgl | all",
  "locomotion": "one of: walk | drive | fly | swim",
  "environment": {{
    "time_of_day": "one of: dawn | day | sunset | night",
    "fog": false,
    "fog_color": null,
    "sun_intensity": null
  }},
  "render_pipeline": "one of: URP | HDRP | builtin",
  "unity_version": "one of: 6000.0 | 6000.1 | 6000.4 | 2022.3 | 2021.3  (default: 6000.0)",
  "visual_style": "one of the visual styles above",
  "color_palette": {{
    "name": "Short palette name",
    "primary": "#RRGGBB",
    "secondary": "#RRGGBB",
    "accent": "#RRGGBB",
    "background": "#RRGGBB",
    "trim": "#RRGGBB"
  }},
  "scenes": ["Main", "MainMenu"],
  "scene_specs": [
    {{
      "name": "Main",
      "description": "one line: what makes this scene distinct",
      "time_of_day": "day",
      "fog": false,
      "entities": ["EntityGroupName1", "EntityGroupName2"]
    }}
  ],
  "features": ["list", "of", "feature", "flags"],
  "packages": [
    {{
      "description": "plain English package description"
    }}
  ],
  "scripts": [
    {{
      "name": "PascalCase class name",
      "type": "one of the script types above",
      "description": "what this script does in plain English",
      "attach_to": "player | camera | game_manager | systems  (custom type only)",
      "extra_usings": [], "extra_fields": [], "awake_extra": "",
      "update_extra": "", "fixed_update_extra": "", "extra_methods": []
    }}
  ],
  "story_beats": [
    {{
      "id": "snake_case_unique_id",
      "label": "Short display label",
      "description": "What happens at this story beat",
      "trigger": "on_start | on_enter_zone | on_enemy_death | on_pickup | on_timer | manual"
    }}
  ],
  "audio_tracks": [
    {{
      "name": "PascalCaseTrackName",
      "description": "plain English description of the track",
      "loop": true,
      "category": "music | sfx | ambient | voice"
    }}
  ],
  "procedural_meshes": [
    {{
      "name": "PascalCaseMeshName",
      "description": "what the geometry looks like",
      "mesh_type": "terrain | room | road | water | custom",
      "resolution": 32
    }}
  ],
  "entities": [
    {{
      "name": "PascalCaseGroupName",
      "kind": "enemy | building | crowd | pickup | prop | ...",
      "count": 6,
      "shape": "cube | sphere | capsule | cylinder | quad | plane",
      "placement": "single | line | ring | grid | scatter | path",
      "color_role": "primary | secondary | accent | trim | background",
      "scale": 1.0,
      "tag": "Untagged",
      "behavior": "",
      "path_points": []
    }}
  ]
}}
{multimodal_block}
Output ONLY the JSON. No other text."""


# ── STEP 3b: multimodal IR block ──────────────────────────────────────────────
# UnitySchema is a full multimodal IR (characters/missions/audio_specs/materials/
# models/animations/vfx/shaders/behavior_trees/microgens/atoms/capabilities), and
# every one of those fields already has a deterministic consumer downstream. They
# stayed empty on every generated project for one reason: the Step-3 prompt never
# asked for them.
#
# Asking for all of them in one call is not an option — local models run at 8k-16k
# context and the schema prompt is already ~3.5k tokens. So the block is split:
#
#   CORE      — always emitted. capabilities/characters/missions/audio_specs apply
#               to every game and drive the composed subsystems directly.
#   OPTIONAL  — gated on CONTENT (the concept + user prompt text), same convention
#               unity_compose.subsystems_for uses to gate optional micros. At most
#               `max_optional` sections ship, ranked by how many distinct signals
#               the brief hit, so the prompt's size stays bounded no matter how
#               much the concept describes.
#   wiring    — compose-only, so it lives in GAMECONTEXT_SCHEMA_GUIDANCE below
#               (its source/target vocabulary is the micro kinds, which only
#               exist in --compose mode).
#
# world_spec is deliberately absent: unity_layout / unity_racetrack author it
# deterministically in Step 5, so asking the LLM for it would only cost tokens.

_MULTIMODAL_HEADER = """
MULTIMODAL DESIGN LAYER — ADDITIONAL top-level keys in the SAME JSON object
(siblings of "entities", not nested inside it). The keys above configure the
PROJECT; these author the GAME, and each one is compiled into a real asset or
subsystem. Fill what the concept actually calls for and leave a key out when the
concept never implies it — padding these with filler is worse than omitting them.
"""

# ── Core sections (always emitted) ────────────────────────────────────────────

_MM_CAPABILITIES = """
  "capabilities": TYPED subsystem parameters. Each sub-object you include is built
    into a real subsystem using THESE numbers instead of generic defaults — set
    them to the concept's feel (a heavy tank and a twitchy hoverbike are the same
    fields with different values). Include only the sub-objects the concept needs:
      "controller": {"movement":true,"jump":true,"sprint":false,"crouch":false,"speed":6.0,"sprint_multiplier":1.7,"jump_force":8.0,"look_sensitivity":2.0}
      "physics":    {"gravity":true,"collision":"dynamic","mass":80.0,"drag":0.0}   collision: none|static|kinematic|dynamic
      "hud":        {"show_health":true,"show_ammo":false,"show_objective":true,"show_minimap":false}
      "input":      {"bindings":[{"action":"Fire","key":"Mouse0"},{"action":"Interact","key":"E"}]}
      "ai":         {"pathfinding":true,"detection_range":15.0,"behavior":"patrol"}  behavior: idle|patrol|guard|chase|flee|attack|custom
      "animation":  {"rig_type":"humanoid","clips":{"Idle":"Idle","Run":"Run"}}      rig_type: generic|humanoid
      "dialog":     {"system":"simple","bark_on_sight":false}                        system: simple|branching|voiced
      "vfx":        {"particles":true,"post_processing":false}
      "model":      {"lod":"auto","animations":{"Idle":"Idle"}}
"""

_MM_CHARACTERS = """
  "characters": max 6 — every named person/creature/pilot in the game, the player
    included. Names and roles, not crowd filler (crowds stay in "entities"):
      {"id":"snake_case_id","name":"Display Name","role":"player|npc|enemy|boss|companion",
       "model_ref":"<an entity group name or models[] id>","animation_set":["Idle","Run"]}
"""

_MM_MISSIONS = """
  "missions": max 3 — what the player is actually asked to DO. Each becomes a real
    on-screen objective tracker, so write objectives the scene can satisfy:
      {"id":"snake_case_id","title":"Short Title","description":"one line",
       "objectives":[{"id":"obj_1","description":"one line","type":"reach|collect|defeat|survive|escort|timer",
                      "target_ref":"<entity group name or character id>","count":1,"optional":false}],
       "rewards":{"score":100},"story_beat_ref":"<a story_beats id, or \\"\\">"}
    max 5 objectives per mission, and a mission with an EMPTY "objectives" list is
    dropped — the objectives are the whole point.
"""

_MM_AUDIO_SPECS = """
  "audio_specs": max 8 — every distinct SOUND the concept implies (engine note,
    footstep, pickup chime, menu theme, wind bed). Each one is really generated:
      {"id":"snake_case_id","type":"music|sfx|ambient|voice","description":"what it sounds like",
       "generation_method":"sfxr|stable_audio|elevenlabs|procedural|recorded","loop":false,
       "parameters":{"gen_prompt":"detailed prompt for the generator","spatial":false}}
    sfxr/procedural = short synthesised blips (hit, jump, pickup); stable_audio =
    music and ambient beds (set "loop":true); elevenlabs = spoken lines (put the
    words in parameters.text). This is a SOUND DESIGN list — "audio_tracks" above
    is only the music playlist.
"""

_CORE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("capabilities", _MM_CAPABILITIES),
    ("characters",   _MM_CHARACTERS),
    ("missions",     _MM_MISSIONS),
    ("audio_specs",  _MM_AUDIO_SPECS),
)

# ── Optional sections (content-gated) ─────────────────────────────────────────

_ATOM_VOCAB_SLOT = "<<ATOM_VOCABULARY>>"

_MM_DIALOG = """
  DIALOG — the concept has people who talk, so extend the characters that speak:
    add "dialog_graph" to that character:
      {"start_node":"n1","nodes":[{"id":"n1","speaker":"Display Name","text":"the spoken line",
        "emotion":"neutral","audio_ref":"<an audio_specs id — set it ONLY for a voiced line>",
        "choices":[{"text":"player reply","next_node":"n2","condition":""}]}]}
    add "npc_brain" to a character with real intent:
      {"traits":["gruff","loyal"],"goals":["guard the gate"],"memory":[]}
    Every "next_node" MUST be another node's id in the same graph. A character
    with any audio_ref set gets a voiced dialog system; leave audio_ref "" for text.
"""

_MM_ANIMATIONS = """
  "animations": max 8 — the named clips the characters actually play:
      {"id":"snake_case_id","name":"Run","clip":"Run","loop":true,"events":["Footstep"]}
    "name" is the animator STATE, "clip" is the clip asset name (usually equal).
    "events" are Unity Animation Event names fired from the clip. Every name you
    list here should also appear in the matching character's "animation_set".
"""

_MM_BEHAVIOR_TREES = """
  "behavior_trees": max 3 — one per distinct enemy/NPC mind the concept describes:
      {"id":"snake_case_id","name":"GuardBrain","tree":{"root":"n1","nodes":[
        {"id":"n1","type":"selector","children":["n2","n4"]},
        {"id":"n2","type":"sequence","children":["n3"]},
        {"id":"n3","type":"condition","action":"PlayerInRange","params":{"range":12}},
        {"id":"n4","type":"action","action":"Patrol","params":{"speed":2.0}}]}}
    type: sequence|selector|action|condition|decorator. "root" and every id in
    "children" MUST be another node's id in the same tree; only sequence/selector/
    decorator nodes take children. The same tree may also be inlined on a
    character as npc_brain.behavior_tree.
"""

_MM_VFX = """
  "vfx": max 4 — the particle/impact effects the concept names:
      {"id":"snake_case_id","name":"PascalCaseName","type":"particle|volumetric|shader_fx",
       "parameters":{"loop":false,"duration":2.0,"play_on_start":true,"auto_destroy":true,"scale":1.0}}
    One entry = one effect; a looping ambient effect sets "loop":true and
    "auto_destroy":false.
"""

_MM_SURFACES = """
  "materials": max 6 — the named surfaces the concept describes. Each becomes a
    real .mat asset tinted from parameters.color, so give every one a color:
      {"id":"snake_case_id","name":"PascalCaseName","shader_model":"Standard",
       "generation_method":"procedural|substance|sdxl|imported",
       "parameters":{"color":"#RRGGBB","smoothness":0.3}}
  "shaders": max 3 — ONLY for a look no plain material gives (toon ramp, water,
    hologram, scanline):
      {"id":"snake_case_id","name":"PascalCaseName","language":"shadergraph|hlsl|glsl",
       "parameters":{"note":"what the shader does"}}
  "models": max 6 — the 3D assets the concept names, and the materials dressing them:
      {"id":"snake_case_id","name":"PascalCaseName","category":"vehicle|character|prop|environment",
       "generation_method":"blender_microgen|parametric_base|imported",
       "parameters":{},"materials":["<a materials id>"]}
"""

_MM_SEQUENCE = """
  "atoms": max 12 — a scripted beat sequence (intro/cutscene) played once at start,
    in order. Use ONLY these atom names — anything else compiles to a TODO comment
    (args in parentheses; omit an arg to take its default):
""" + _ATOM_VOCAB_SLOT + """
      [{"atom":"ScreenFade","args":{"alpha":0.0,"duration":1.5},"label":"fade in"},
       {"atom":"CameraShake","args":{"intensity":0.4,"duration":1.0},"label":"impact"}]
  "microgens": max 8 — a named bundle of those same atoms for one subsystem:
      {"kind":"intro_sequence","input":{},"output_files":[],
       "atoms":[{"atom":"Wait","args":{"seconds":1.0},"label":"beat"}]}
    The compiler prepends every microgens[].atoms entry to "atoms".
    When "kind" names a real subsystem — controller, camera, health, hud,
    inventory, ai, spawner, physics, trigger, vfx, objectives, menu, saveload,
    dialogue, npc_dialog, interactable, statemachine, animator, lighting,
    shader, transition, behavior_tree, audio — "input" may ALSO carry the C#
    that makes that subsystem specific to THIS concept. The compiler already
    generates the baseline for the chosen variant; you author only the
    difference, as typed fragments it injects at fixed points:
      {"kind":"health","input":{
         "extra_fields":[{"name":"_radiation","type":"float","default":"0f"}],
         "update_extra":"_radiation += Time.deltaTime * 0.5f;",
         "extra_methods":[{"signature":"private bool IsIrradiated()",
                           "body":"return _radiation > 50f;"}]}}
    Keys: extra_usings (namespaces, no "using"/";"), extra_fields
    ({name,type,default,header,tooltip}), awake_extra / update_extra /
    fixed_update_extra (C# statements, no braces), extra_methods
    ({signature,body} — no braces, the template adds them). Pure C#, never
    Python. Anything outside these keys is ignored.
"""

# Section id → (keywords, body). Declaration order breaks score ties, so the
# earlier a section sits the more it is favoured at equal signal strength.
_OPTIONAL_SECTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("dialog", (
        "dialog", "dialogue", "conversation", "talk", "talks", "speak", "speaks",
        "spoken", "voiced", "voice", "npc", "npcs", "quest", "quest giver",
        "banter", "narrator", "story beat", "branching",
    ), _MM_DIALOG),
    ("behavior_trees", (
        "enemy", "enemies", "ai", "patrol", "patrols", "guard", "guards", "chase",
        "chases", "hunt", "hunts", "boss", "aggro", "pursue", "flee", "hostile",
        "wave", "waves", "spawner", "swarm", "stealth", "detection",
    ), _MM_BEHAVIOR_TREES),
    ("animations", (
        "animation", "animations", "animated", "animator", "rig", "rigged",
        "humanoid", "walk cycle", "idle", "mocap", "motion capture", "gesture",
        "dance", "climb", "vault", "ragdoll",
    ), _MM_ANIMATIONS),
    ("vfx", (
        "vfx", "particle", "particles", "explosion", "explosions", "explode",
        "smoke", "spark", "sparks", "trail", "trails", "muzzle", "dust", "flame",
        "flames", "magic", "glow", "impact", "debris", "splash", "sparkle",
    ), _MM_VFX),
    ("surfaces", (
        "material", "materials", "shader", "shaders", "texture", "textures",
        "pbr", "metallic", "roughness", "glossy", "toon", "cel shaded", "outline",
        "neon", "hologram", "reflective", "mesh", "meshes", "geometry", "fbx",
        "blender", "procedural",
    ), _MM_SURFACES),
    ("sequence", (
        "cutscene", "cutscenes", "cinematic", "cinematics", "intro", "opening",
        "scripted", "sequence", "timeline", "camera pan", "fade in", "fade out",
        "set piece", "choreograph", "montage", "prologue",
    ), _MM_SEQUENCE),
)

_SEQUENCE_ATOM_CATEGORIES = (
    "movement", "camera", "animation", "audio", "fx", "logic", "entity",
)


@lru_cache(maxsize=None)
def atom_vocabulary(categories: tuple[str, ...] = _SEQUENCE_ATOM_CATEGORIES,
                    per_line: int = 3) -> str:
    """Compact `Name(arg, arg)` listing of the real atom registry.

    Generated from src.atoms so the prompt can never drift from the atoms that
    actually have a Unity backend snippet — an atom name the registry doesn't
    know compiles to a `// TODO` comment, so the vocabulary has to be exact.
    Subsystem-level categories (controller/hud/physics/input/model/wiring) are
    left out: those are driven by "capabilities", not by a beat sequence.
    """
    from .atoms import list_atoms

    entries = [
        f"{a.name}({', '.join(p.name for p in a.params)})"
        for cat in categories for a in list_atoms(cat)
    ]
    return "\n".join(
        "      " + "  ".join(entries[i:i + per_line])
        for i in range(0, len(entries), per_line)
    )


@dataclass(frozen=True)
class MultimodalBlock:
    """Rendered Step-3 multimodal block + why each optional section shipped."""
    text:     str
    sections: tuple[str, ...]          # core ids first, then the chosen optional ids
    scores:   dict[str, int]           # optional id → distinct keywords matched


def _signal_score(text: str, keywords: tuple[str, ...]) -> int:
    """Count DISTINCT keywords present in `text` (already lowercased).

    Word-boundary matched, so "ai" doesn't fire on "chair" and "rig" doesn't fire
    on "rigid" — the same class of false positive that silently mis-triggered the
    audio keyword table before it was boundary-matched.
    """
    return sum(1 for kw in keywords
               if re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text))


def render_multimodal_block(concept: str,
                            user_prompt: str = "",
                            *,
                            max_optional: int = 3) -> MultimodalBlock:
    """Render the Step-3 multimodal ask for one project.

    Core sections always ship. Optional sections are scored against the concept +
    user prompt and the top `max_optional` are appended, which bounds the prompt
    growth at roughly +900 tokens no matter how much the brief describes.
    Deterministic: same text in → same block out, no LLM, no RNG.
    """
    haystack = f"{concept}\n{user_prompt}".lower()

    scores = {sid: _signal_score(haystack, kws)
              for sid, kws, _body in _OPTIONAL_SECTIONS}
    order  = {sid: i for i, (sid, _kws, _body) in enumerate(_OPTIONAL_SECTIONS)}
    ranked = sorted((sid for sid, score in scores.items() if score > 0),
                    key=lambda sid: (-scores[sid], order[sid]))[:max(0, max_optional)]
    # Selection is by score; rendering + reporting stay in declaration order so
    # the block reads the same way every time.
    chosen = tuple(sid for sid, _kws, _body in _OPTIONAL_SECTIONS if sid in ranked)

    bodies = [_MULTIMODAL_HEADER]
    bodies += [body for _sid, body in _CORE_SECTIONS]
    bodies += [body for sid, _kws, body in _OPTIONAL_SECTIONS if sid in chosen]

    text = "".join(bodies).replace(_ATOM_VOCAB_SLOT, atom_vocabulary())
    return MultimodalBlock(
        text     = text,
        sections = tuple(sid for sid, _b in _CORE_SECTIONS) + chosen,
        scores   = scores,
    )


# Micro kinds unity_compose.subsystems_for can emit — the ONLY valid
# wiring.dependencies source/target values (a dep naming anything else is
# silently dropped by MicroComposer). Kept in sync by test_unity_prompts.py,
# which diffs this against the kinds subsystems_for actually produces.
WIRING_MICRO_KINDS: tuple[str, ...] = (
    "controller", "camera", "health", "hud", "menu", "saveload", "physics",
    "input", "interactable", "animator", "statemachine", "trigger", "audio",
    "ai", "spawner", "inventory", "dialogue", "npc_dialog", "vfx",
    "transition", "objectives", "behavior_tree", "lighting",
)

# Pairs unity_compose already wires with a named seam — declaring them again
# just produces a duplicate comment, so the prompt tells the LLM to skip them.
_WIRED_PAIRS = (
    "health↔hud, health↔statemachine, health↔audio, statemachine↔animator, "
    "input↔controller, input↔statemachine, trigger↔saveload"
)

WIRING_SCHEMA_GUIDANCE = """

  "wiring": a top-level key declaring seams BETWEEN composed subsystems:
      {"dependencies":[{"source":"trigger","target":"objectives","type":"triggers"}]}
    type: uses | triggers | references | contains. "source"/"target" must both be
    micro kinds from this list — any other name is dropped:
      """ + ", ".join(WIRING_MICRO_KINDS) + """
    These pairs are already wired for you — do NOT declare them:
      """ + _WIRED_PAIRS + """
    Declare only seams the concept genuinely implies (max 6)."""


# Appended to the schema-fill prompt ONLY in --compose mode, where the micro
# subsystems and the GameContext locator exist. Steers custom-script fragments to
# reach other subsystems through GameContext instead of inventing singletons
# (which the deterministic fixer would otherwise strip).
GAMECONTEXT_SCHEMA_GUIDANCE = """

  REACHING OTHER SUBSYSTEMS — this project is composed, so the player/camera/HUD/
  audio/save systems already exist as separate components. From any custom-script
  fragment, reach another subsystem through the shared locator (never invent a
  singleton like FooManager.Instance — only these two access points exist):
      GameContext.Instance.Get<T>()   // cached; may return null — ALWAYS null-check
      GameManager.Instance            // game state / score / lives
  Examples:
      var hp = GameContext.Instance.Get<PlayerHealth>(); if (hp != null) hp.TakeDamage(10f);
      GameContext.Instance.Get<GameManager>()?.AddScore(100);
  Composed subsystem types you may Get<T>(): PlayerController, PlayerCamera,
  PlayerHealth, PlayerAnimator, PlayerFSM, GameHud, PauseMenu, SaveSystem,
  PhysicsBody, WorldInteractable, SceneTrigger, LightingDirector (plus GameManager).
  A PLAYER STATE MACHINE ALREADY EXISTS (PlayerFSM) — never author your own custom
  FSM/state-object script for player state. Drive its state via
  GameContext.Instance.Get<PlayerFSM>() instead of inventing a ClimbState/EnemyState-
  style type with its own Update()/NextState() methods (those methods don't exist
  anywhere and fail compilation — see extra_fields rule above).""" + WIRING_SCHEMA_GUIDANCE

# ── STEP 5: Schema validation ─────────────────────────────────────────────────

UNITY_VALIDATE_SYSTEM = """\
You are a Unity project schema validator.
You receive a schema with resolved package IDs and must verify and correct it.
Output ONLY valid JSON — no explanation, no markdown."""

UNITY_VALIDATE_USER = """\
Validate and correct this Unity project schema.

SCHEMA:
{schema}

VALIDATION RULES:
1. package_id must be a valid Unity Package Manager ID:
   VALID:   com.unity.render-pipelines.universal | com.unity.inputsystem | com.unity.cinemachine
   INVALID: "URP" | "universal render pipeline" | "Input System"
2. version must match pattern: digits.digits.digits (e.g. "14.0.11") or null
3. game_type: 2D | 2.5D | 3D | VR | top_down | 2d_topdown | isometric
4. platform: pc | mobile | console | webgl | all
5. render_pipeline: URP | HDRP | builtin
6. unity_version: 6000.0 | 6000.1 | 6000.4 | 2022.3 | 2021.3  (default: 6000.0)
7. script type: player | camera | game_manager | enemy | collectible | ui_manager | audio_manager | health |
   dialogue_manager | mesh_builder | procedural_mesh | level_generator | material_palette | custom
   (custom scripts keep their extra_* fragment fields and attach_to — do not strip them)
8a. visual_style: cartoon | realistic | low_poly | stylized | pixel_art | retro
8b. color_palette: each color must be a valid #RRGGBB hex string
8. project_name: PascalCase, no spaces, no special characters
9. Remove any extra fields not in the original schema

Output the corrected schema as JSON only."""
