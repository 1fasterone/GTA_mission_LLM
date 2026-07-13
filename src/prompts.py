"""
LLM prompt templates.
LLM = storyteller.  RAG = database.  Pipeline = compiler.
"""

# ── STEP 2: Story concept ────────────────────────────────────────────────────

CONCEPT_SYSTEM = """\
You are a GTA5 mission writer. Write compelling mission stories — narrative only.
No code. No JSON. No technical IDs.
Be specific: name character types, locations, vehicles, and props."""

CONCEPT_USER = """\
{theme_block}
{archetype_block}
Write a GTA5 mission concept based on this idea:

"{user_prompt}"

Cover in 2-3 paragraphs:
- Mission setup and why the player is there
- Key characters and their roles (guard, dealer, corrupt cop, cultist, etc.)
- Step-by-step objectives in order
- Atmosphere, time of day, weather

Use ONLY these location names (exact spelling):
docks | airport | downtown | vinewood | sandy_shores | paleto_bay | alamo_sea | military_base | prison | port | highway

Do NOT write any code, JSON, or model IDs."""

# ── STEP 3: Schema fill ──────────────────────────────────────────────────────

SCHEMA_FILL_SYSTEM = """\
You are a GTA5 mission schema designer.
Output ONLY valid JSON — no explanation, no markdown fences.
Use plain English descriptions for all model fields.
The pipeline resolves English → model IDs automatically."""

SCHEMA_FILL_USER = """\
{schema_hints}
{structure_block}
Convert this mission concept into the schema below.
Use plain English for every "description" field (e.g. "corrupt cop", "cargo van", "drug package").

The mission needs a clear objective SPINE — do not emit a flat list of go_to/wait
steps. Use the step types that express the actual goal (race_lap for a race,
pickup for a grab, kill_all for a fight, escape for a getaway, timer for pressure).

LIMITS (keep the JSON short — the model has a token budget):
- steps:    max 6
- peds:     max 5 entries (use count field for multiples)
- vehicles: max 4 entries
- objects:  max 4 entries
- audio:    max 4 entries
- labels:   under 8 words each

STEP TYPES — use only these:
  go_to          — player drives/walks to a map waypoint
  kill_all       — player eliminates all spawned hostile peds
  pickup         — player walks over a spawned prop to collect it
  deliver        — player reaches the blip (use after pickup)
  get_in_vehicle — player boards one of the spawned mission vehicles
  escape         — player loses all wanted stars
  wait           — 3-second pause with a message (use sparingly)

  --- RACING / TIMED (use for any race, circuit, or time-based objective) ---
  checkpoint     — player passes through all points on a named track (single pass)
                   params: {{"track": "airport.figure_8", "require_vehicle": true}}
                   available tracks: airport.figure_8 | airport.circuit | vinewood.circuit | highway.circuit | docks.circuit | sandy_shores.circuit
  race_lap       — player completes N laps of a checkpoint circuit
                   ALWAYS use this for racing / figure-8 / lap missions
                   params: {{"lap_count": 5, "track": "airport.figure_8", "require_vehicle": true}}
  timer          — player must survive or hold position for N seconds
                   params: {{"seconds": 60}}
  scavenger      — player visits N scattered points in ANY order (true scavenger
                   hunt, not linear); blips turn green as each is collected
                   params: {{"target_count": 5}}   (3-10 points)

MISSION CONCEPT:
{concept}

OUTPUT this exact JSON structure, fully populated:
{{
  "mission_name": "string",
  "location_name": "one of: docks | airport | downtown | vinewood | sandy_shores | paleto_bay | alamo_sea | military_base | prison | port | highway",
  "time_of_day": 0,
  "weather": "one of: CLEAR | OVERCAST | RAIN | THUNDER | FOGGY | SMOG",
  "steps": [
    {{
      "type": "one of: go_to | kill_all | pickup | deliver | get_in_vehicle | escape | wait | checkpoint | race_lap | timer | scavenger",
      "label": "Brief on-screen instruction shown to the player (under 8 words)",
      "params": {{}}
    }}
  ],
  "peds": [
    {{
      "description": "plain English character type",
      "role": "one of: guard | patrol | target | civilian | ally",
      "count": 1,
      "weapon": "one of: none | pistol | smg | rifle | shotgun | sniper"
    }}
  ],
  "vehicles": [
    {{
      "description": "plain English vehicle type",
      "role": "one of: patrol | escape | delivery | parked | chase | race_opponent",
      "count": 1
    }}
  ],
  "objects": [
    {{
      "description": "plain English prop or pickup item",
      "role": "one of: pickup | obstacle | decoration"
    }}
  ],
  "audio": [
    {{
      "type": "one of: ambient | mission_passed | mission_failed | sting",
      "context": "when this audio plays"
    }}
  ],
  "ai_difficulty": "one of: easy | medium | hard — ONLY meaningful if you added a race_opponent vehicle; scale it to how tough the concept says the rivals are (default medium)",
  "config_menu": "true only if the concept explicitly asks for a pre-race settings/options menu the player can open (e.g. an F10 menu to adjust weather or rival difficulty); otherwise false",
  "events": "OPTIONAL list (max 3) of one-shot mid-mission twists, each {{\\"event_type\\": \\"thunderstorm | police_pursuit\\", \\"at_progress\\": 0.5, \\"params\\": {{}}}} — at_progress is the mission fraction (0.1-0.9) when it fires; police_pursuit params may set {{\\"wanted_level\\": 3, \\"spawn_heli\\": true}} for an attack helicopter. Use [] if the concept has no mid-mission twist",
  "hud": "true only if the concept wants an on-screen timer / progress bar display during the mission; otherwise false",
  "reward_cash": "integer cash paid on mission completion (max 1000000); 0 if the concept mentions no payment",
  "extra_usings": [],
  "extra_fields": [],
  "extra_init": "",
  "extra_tick": "",
  "extra_methods": []
}}

────────── MISSION-SPECIFIC C# (this is how the prompt's UNIQUE ideas reach the script) ──────────
The pipeline already emits the full mission skeleton: a SHVDN3 `Script` subclass
with spawning, a step state-machine, blips, and cleanup. You do NOT rewrite that.
The standard steps cover only generic objectives — EVERYTHING ELSE the concept
asks for (a custom fail condition, ambient events, escalating pressure, scripted
moments, hazards, special vehicle behavior) MUST be authored by you as SHVDN3 C#
in these fields. A concept with distinctive ideas and empty extra_* fields is an
INCOMPLETE answer:

  extra_usings:  extra namespaces (rare; "GTA.UI" is already reachable as a path)
  extra_fields:  extra private members the logic needs
                 each: {{"name": "_myTimer", "type": "int", "default": "0", "tooltip": "..."}}
  extra_init:    C# statements run ONCE when the mission starts (end of Init())
  extra_tick:    C# statements run EVERY frame (end of OnTick())
  extra_methods: helper methods, each {{"signature": "private void Foo()", "body": "..."}}

────────── FEW-SHOT EXAMPLE ──────────
Concept detail: "every 30 seconds the cartel sends another chase car after you"
→ you author:
  "extra_fields": [
    {{"name": "_nextWaveAt", "type": "DateTime", "default": "DateTime.MinValue", "tooltip": "next reinforcement time"}}
  ],
  "extra_init": "_nextWaveAt = DateTime.UtcNow.AddSeconds(30);",
  "extra_tick": "if (_state == State.Active && DateTime.UtcNow >= _nextWaveAt) {{ SpawnChaseWave(); _nextWaveAt = DateTime.UtcNow.AddSeconds(30); }}",
  "extra_methods": [
    {{"signature": "private void SpawnChaseWave()",
      "body": "var pos = Game.Player.Character.Position + new Vector3(40f, 40f, 0f);\\nvar car = World.CreateVehicle(new Model(\\"baller\\"), pos);\\nvar drv = World.CreatePed(new Model(\\"g_m_y_mexgoon_01\\"), pos + new Vector3(2f, 0f, 0f));\\nif (car == null || drv == null) return;\\n_vehicles.Add(car); _peds.Add(drv);\\ndrv.SetIntoVehicle(car, VehicleSeat.Driver);\\ndrv.Task.VehicleChase(Game.Player.Character);\\nGTA.UI.Notification.PostTicker(\\"~r~The cartel sent another car!\\", false);"}}
  ]

SHVDN3 RULES (this is GTA5, NOT Unity):
- Use SHVDN3 API: Game.Player.Character, World.CreatePed, Function.Call(Hash.XXX, ...),
  GTA.UI.Notification.PostTicker("~y~text", false), GTA.Math.Vector3.
- NEVER use UnityEngine — there is no Unity here.
- Reference skeleton members you can rely on: _state, _step, _peds, _vehicles,
  _props, SpawnOrigin. Don't redeclare them (they're reserved).
- CRITICAL: every identifier your fragments reference MUST be declared in
  extra_fields (or be a local declared inside the fragment) — referencing an
  undeclared variable FAILS COMPILATION. Add spawned entities to _peds/_vehicles/
  _props so cleanup handles them.
- Pure C#, no Python. No "using" lines inside bodies — those go in extra_usings.
- No braces around extra_methods bodies — the template adds them.
- Only a truly generic concept with no distinctive ideas leaves every extra_* empty.
- CRITICAL: extra_tick runs on EVERY frame regardless of mission state — the
  switch(_state) above it only dispatches CheckStart()/RunStep(), it does NOT
  gate your extra_tick code. Any side effect that can hurt/kill the player or
  fail the mission (Health = 0, forcing State.Failed, etc.) MUST be wrapped in
  `if (_state == State.Active) {{ ... }}` yourself, or it fires before the
  mission even starts (State.Inactive) and after it's already over
  (State.Complete/State.Failed). Live regression: an oxygen meter that
  decremented and force-killed the player unconditionally, instantly killing
  them on script load before they ever reached the mission start blip.
- CRITICAL: OnTick fires many times per SECOND, not once per second. A plain
  `_meter -= 2;` in extra_tick depletes almost instantly (e.g. 100 in well
  under a second at 60fps) — that is virtually never what "N per second"
  language in the concept means. For any time-based meter/countdown/decay,
  accumulate `Game.LastFrameTime` (seconds since last frame) in a float field
  and only apply the change once the accumulator crosses 1f (resetting it by
  subtracting 1f, not zeroing, so no time is lost): e.g. add
  `_meterTimer += Game.LastFrameTime; if (_meterTimer >= 1f) {{ _meterTimer -= 1f; _meter -= 2; }}`.

Output ONLY the JSON. No other text."""

# ── STEP 5: Schema validation ────────────────────────────────────────────────

VALIDATE_SYSTEM = """\
You are a GTA5 mission schema validator.
You receive a schema with resolved model IDs and must verify and correct it.
Output ONLY valid JSON — no explanation, no markdown."""

VALIDATE_USER = """\
Validate and correct this GTA5 mission schema.

SCHEMA:
{schema}

VALIDATION RULES:
1. "model" fields must be lowercase GTA5 IDs: letters, digits, underscores only
   VALID:   s_m_y_cop_01  |  police4  |  prop_paper_bag_01  |  a_m_o_acult_01
   INVALID: "cop"  |  "Police Car"  |  "Cop_01"
2. If a model field still looks like plain English, set it to "UNRESOLVED"
3. weather must be uppercase: CLEAR | OVERCAST | RAIN | THUNDER | FOGGY | SMOG
4. step types: go_to | kill_all | pickup | deliver | get_in_vehicle | escape | wait | checkpoint | race_lap | timer | scavenger
4b. params field on steps is optional — preserve it if present, default to {} if absent; do NOT strip it
4c. events (optional list): event_type must be thunderstorm | police_pursuit; at_progress a number
    between 0 and 1; preserve the list as given, do NOT strip it
5. ped roles: guard | patrol | target | civilian | ally
6. vehicle roles: patrol | escape | delivery | parked | chase | race_opponent
7. object roles: pickup | obstacle | decoration
8. count must be an integer between 1 and 8
9. Remove any extra fields not in the schema — EXCEPTIONS: keep "params" on steps, keep
   "events"/"hud"/"reward_cash" as given, and keep
   "extra_usings"/"extra_fields"/"extra_init"/"extra_tick"/"extra_methods" EXACTLY as given
   (they are the mission-specific C# channel; never strip, rewrite, or empty them)

Output the corrected schema as JSON only."""
