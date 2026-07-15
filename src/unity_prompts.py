"""
LLM prompt templates for the Unity Project Creator pipeline.
Same philosophy as GTA5: LLM = storyteller/specifier, pipeline = deterministic compiler.
"""

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
Use plain English for all description fields; the pipeline resolves them to package IDs."""

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

ENTITIES — the actual objects in the scene, built from primitives so the concept SHOWS UP.
  Populate one group per distinct set of objects the prompt implies. Fields:
    shape:      cube | sphere | capsule | cylinder | quad | plane
    placement:  single | line | ring | grid | scatter | path — "single" is for count=1
      ONLY (a real model spawned once); a group with count>1 needs a real spread
      (line/ring/grid/scatter/path), or it gets auto-upgraded and a fix is logged.
    color_role: primary | secondary | accent | trim | background  (picks a palette color)
    count:      how many (e.g. 6 rivals → 6); tag: e.g. "Enemy" so AI/health find them
    behavior:   OPTIONAL — the PascalCase name of one of your `scripts` to attach to each
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

Output ONLY the JSON. No other text."""


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
  PhysicsBody, WorldInteractable, SceneTrigger (plus GameManager).
  A PLAYER STATE MACHINE ALREADY EXISTS (PlayerFSM) — never author your own custom
  FSM/state-object script for player state. Drive its state via
  GameContext.Instance.Get<PlayerFSM>() instead of inventing a ClimbState/EnemyState-
  style type with its own Update()/NextState() methods (those methods don't exist
  anywhere and fail compilation — see extra_fields rule above)."""

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
