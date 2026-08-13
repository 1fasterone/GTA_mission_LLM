# Grounded Spine, Quarantined Invention

How to take variety and semantic richness from the Pipeline LLM — **including its
hallucinations** — without letting its inventions degrade the build.

- **Control arm:** `python main.py unity-create "<brief>" --no-llm` (`src/unity_briefparse.py`)
- **Reference implementations:** `src/micro/ai.py` (states), `src/micro/hud.py` (widgets)
- **Related:** `docs/WIRING_SEAMS.md`, `CLAUDE.md` § Eval Discipline

---

## 1. The problem, stated precisely

The Pipeline LLM is not bad at its job. It is *pointed at the wrong job*, and the
compiler is built so that its one genuine strength — invention — can only ever
arrive as damage.

### 1.1 Step 3 cannot see the brief

```python
# src/unity_prompts.py
UNITY_SCHEMA_FILL_USER = """...
Convert this Unity project concept into the schema below.
...
{concept}
"""
```

Schema-fill receives **the concept**, never `user_prompt`. The brief is a
specification; the concept is a few hundred words of prose the model wrote about
it. Everything the concept omits is unrecoverable at Step 3 **by construction** —
no amount of model capability fixes a value that is not in the input.

This is a pipeline-topology decision, not a model weakness. It is the structural
cause of "the LLM dropped my entities".

### 1.2 The rescue layers are the evidence

`src/unity_concept_entities.inject_missing_concept_entities` and
`src/unity_concept_scripts.inject_missing_concept_scripts` re-parse
`meta['user_prompt']` and put back what Step 3 dropped. They exist *only* because
Step 3 systematically drops brief content. A pipeline that needs a deterministic
reader of the brief to repair its LLM reader of the brief already contains the
argument for reading the brief deterministically first.

### 1.3 One wide, shallow call

Step 3 fills 13+ modalities in a single completion. Breadth is rewarded — the
JSON validates — and depth is not. Nothing checks that `EntitySpec.behavior`
holds behaviour prose rather than a script name, so it routinely holds
`"EnemyAI"`, and every downstream consumer of that field gets nothing.

### 1.4 Invention has nowhere safe to land — the actual degradation mechanism

Every slot a model invention currently reaches is **load-bearing**:

| The model invents | Where it lands | Failure |
|---|---|---|
| a state name | an enum member the switch dispatches on | **CS0117** (the audit's `ai` defect) |
| a transition condition | raw C# in `CheckTransitions` | **CS0103** |
| a script name | a class that can shadow a micro | **CS0101 / CS0111** |
| a cue id | an audio asset that must exist | silent dead reference |

The compiler's only correct response to an unverifiable invention is to reject
it. So the pipeline **systematically filters variety out** and keeps the generic
residue — the model's ideas are precisely the part that gets dropped.

That is why adding a model can make output blander: not because it invents too
little, but because there is no slot in which its invention is allowed to be
wrong.

### 1.5 The asymmetry the design ignores

| | deterministic parse | LLM fill |
|---|---|---|
| recall of brief **names** | **total** — it reads the brief | lossy — it reads a summary |
| recall of brief **meaning** | **zero** — it copies strings it cannot interpret | **high** |
| variety / semantic invention | **zero** — can only restate | **high** |
| safety of its output | **total** | unbounded |

The pipeline currently uses the high-variety, low-name-recall, unsafe source as
the **primary**, and the total-name-recall, safe source as a **rescue**. That is
inverted for identity — and, as §1.6 shows, the rescue cannot simply replace the
primary, because it carries no meaning for downstream generators to work from.

---

## 1.6 What the A/B run actually showed

Same brief (`prompts/coldwater_circuit.txt`), same compiler, both arms `--compose`.

**Where the brief is EXPLICIT, the deterministic arm keeps the NAMES:**

| brief section | `--no-llm` | LLM | has a rescue layer? |
|---|---|---|---|
| entities | 9/9 exact | 9/9 exact **+1 invented** | yes — `unity_concept_entities` |
| scripts | 12/12 parsed | 12/12 parsed **+1** | yes — `unity_concept_scripts` |
| scene names | **5/5 exact** | **1/5** — invented `RaceStart`, `MidLap`, `FinishLine`, `WarmingHut`, `CrewChiefHub`, `LevelSelect` | **no** |
| audio cue ids | **10/10 exact** | 3 invented | **no** |
| narrative beats | **6** | **3** | **no** |
| project title | `ColdwaterCircuit` | `FrozenLakeRally` | **no** |

The pattern is exact: **every section with a rescue layer survived at 100%; every
section without one was overwritten by invention.** This is not a per-section
quirk of the model — it is the rescue layers doing their job, and the absence of
them doing nothing.

### The audio result inverts this, and it is the most important row in the study

Read the table above and you would conclude the deterministic arm won on audio,
10/10 against 3. Then listen to the two builds:

| | `--no-llm` | LLM |
|---|---|---|
| `audio_tracks` in schema | all 10 brief ids, **verbatim** | 3 invented names |
| **cues actually synthesized** | 7 | 13 |
| **what they are** | `coin`, `footstep`, `hit`, `jump`, `music_loop`, `powerup`, `ui_click` | `studs_wear`, `snow_spray`, `ice_shards`, `checkpoint_hut`, `radio_chirp`, `rival_car`, `engine_idle`, `rubber_tires`, `wind_layer`, `snow_surface`, `hood_metal`, `engine_block`, `camera_rig` |

The arm that preserved **every** audio cue id in the brief shipped a game that
sounds like a generic platformer. The arm that **forgot all ten** shipped a game
that sounds unmistakably like studded tyres on black ice.

Why: diegetic cue extraction is concept-driven. With `--no-llm` there is no
concept, so there is nothing to extract from and the resolver falls back to its
generic bank. The brief's cue ids sat in `schema.audio_tracks` as inert strings —
carried perfectly, understood not at all.

> **Recall of names is not recall of meaning.** This is the single most
> load-bearing correction in this document, and it is invisible to any metric
> that counts schema fields.

**Where the brief is SILENT, only the LLM arm produces anything:**

| IR field | `--no-llm` | LLM |
|---|---|---|
| missions / objectives | **0 / 0** | 1 / 1 |
| characters | 0 | 2 |
| models | 0 | 6 |
| audio_specs | 0 | 3 |
| procedural_meshes | 0 | 2 |

The consequence is decisive. The brief has a NARRATIVE section but no MISSIONS
section, so the deterministic parser emits no objectives, no `objectives` micro
is composed, and **SEAM 8 and SEAM 9 cannot fire at all** — that arm's game
cannot be won. The LLM invented a mission, and the loop closed:

```csharp
// ── SEAM 8: trigger → objectives (reach: obj_finish_line) ─────
_onReachObjective = () => _firstlapvictoryobjectives.ReportProgress("obj_finish_line");

// ── SEAM 9: objectives → HUD (current objective + tally) ──────
_firstlapvictoryobjectives.OnObjectiveProgress += _onObjectiveHudProgress;
```

The same arm also chose the correct spatial primitive (`ArenaPlane`, from
`goal_topology=reach_point`) where the deterministic arm — having no objectives
to derive topology from — fell into a `BoundedVolume` 200 m box with the player
at y=100 and a water plane overhead. **For an ice racer.**

Emitted micro C#: 2,263 lines (`--no-llm`) vs 2,730 (LLM), +467 / +21%, of
which the entire 112-line `objectives` tracker exists in one arm only.

### 1.7 The sharpened rule

The two arms do not differ in quality. **They differ in which half of the job
they can do at all.**

| | deterministic parse | LLM |
|---|---|---|
| owns | **identity** — names, counts, ids, wiring targets | **meaning** — what a thing is, sounds like, is for |
| failure mode | correct names, **generic content** | wrong names, **specific content** |
| the ice racer it ships | says "ice racing", sounds like a platformer, cannot be won | forgets half the spec, sounds right, closes the mission loop |

Identity is what the compiler must bind exactly. Meaning is what every
downstream generator — audio, layout, objectives, models — needs in order to
emit something specific rather than a template. Neither arm can substitute for
the other, and **the pipeline needs both halves simultaneously**, which is what
neither arm currently does.

Two corollaries:

**(a) Invention is valuable where the brief is silent, destructive where it is
specific.** Both are the same act — the model writing a name not in the brief.
Only the presence of ground truth in that slot differs. A rescue layer is exactly
what converts a destructive invention into an additive one: with one, the extra
entity is a bonus on top of 9/9 recall; without one, the extra scene *replaces*
a named scene.

**(b) Never gate a meaning-driven generator on a deterministic-only run.**
The audio result is the warning: `--no-llm` did not produce worse audio, it
produced *unrelated* audio, and no schema-field count would have caught it.

**Highest-value work, therefore:** extend the rescue-layer pattern to SCENES,
AUDIO, NARRATIVE and TITLE so identity is never lost — and keep the LLM in the
loop for meaning, because removing it does not make the output safer, it makes
it generic.

### 1.8 The third failure: invention in executable slots

The LLM arm's per-subsystem code enrichment authored C# referencing
`StuddedTyre`, `currentStudWear`, `PlayIceSkitter()`, `PlaySnowCrunch()`,
`SetDialogueText()`, `WarmingHut`, `RivalRacerAI`. Every one is *this game's own
vocabulary*. Every one was deleted:

```
FIX  neutralized 2 unbindable reference(s) in authored C#: PlayIceSkitter(), PlaySnowCrunch()
FIX  dropped 1 authored method(s) left with no live statement: SetSurfaceAudio
FIX  neutralized 3 unbindable reference(s) in authored C#: SetDialogueText(), StuddedTyre, currentStudWear
FIX  dropped 1 authored method(s) left with no live statement: ApplySpecularBoost
```

~13 identifiers neutralized, and two whole methods dropped once neutralisation
emptied them. The fixer is right — an unbindable name cannot ship. But note what
this costs: the model's best domain thinking arrived in the one slot where the
compiler's only correct response is deletion.

---

## 2. The principle

> **An invention may never land where being wrong breaks the build. It must
> always land where being wrong is merely flavour.**

Route every LLM-produced value into one of three tiers.

### Tier G — Grounded
Resolves against something real: an entity named in the brief, an asset in the
catalog, an atom in the registry, a member of a closed enum.
→ **Accept as content.**

### Tier V — Free vocabulary *(this is where the hallucinations go)*
Resolves against nothing, but the slot is **inert**: a display label, a
description, a state's *name*, a cue *descriptor*, a palette name, an objective's
prose. Being wrong costs flavour and nothing else.
→ **Accept verbatim.** This is the entire source of variety and semantic richness.

### Tier L — Load-bearing
Resolves against nothing and the slot **dispatches**: an enum member a switch
keys on, a class name, a method name, a file path, a C# expression.
→ **Reject, name it in the log, substitute a deterministic equivalent.**

---

## 3. The mechanism: split every invented concept into (name, role)

- **`name`** — the model's, verbatim. Tier V. Reaches the emitted artifact.
- **`role`** — inferred deterministically from the name against a **closed set**.
  Tier G. Is what code is generated from.

The model can invent `Prowl`, `Freeze`, `Bail`. It can never invent a *behaviour*.
Its vocabulary reaches the game; its imagination cannot reach the compiler.

### Worked example — `ai` states (`src/micro/ai.py`)

```python
AIRole = Literal["idle", "patrol", "alert", "chase", "attack", "flee", "dead"]

def infer_role(name: str, description: str = "") -> str:
    """Deterministic and TOTAL — every name resolves. An unrecognised name
    becomes "idle" rather than being dropped: the state still appears in the
    enum and the switch, so the model's vocabulary survives even when its
    meaning does not."""
```

The brief says *"prowls the gantries, freezes when it hears you, then charges"*.
The emitted enum is:

```csharp
public enum SentryDroneState
{
    Prowl,    // [patrol] prowls the gantries
    Freeze,   // [alert]  freezes when it hears you
    Charge,   // [chase]  charges at full thrust
}
```

The names are the brief's. The `switch` bodies and the transition graph are the
compiler's. **Neither can break the other.**

`infer_role` being *total* is the load-bearing detail: a name it does not
recognise is not an error and is not discarded — it degrades to `idle` and still
renders. Rejection would put us back to filtering variety out.

### Transitions: a closed vocabulary instead of authored code

```python
TransitionWhen = Literal["target_detected", "target_lost", "in_attack_range",
                         "out_of_attack_range", "alert_elapsed", "always"]
```

An authored `condition` string is exactly where a hallucinated identifier becomes
CS0103. The model gets a safe surface to express intent on instead — and a
hallucinated `when` clamps to `always` rather than emitting code that cannot
compile.

---

## 4. Already adopted

| Site | Tier V (the model's) | Tier G (deterministic) |
|---|---|---|
| `ai` states | `AIState.name` → sanitised enum member | `AIState.role` → the emitted `case` body |
| `ai` transitions | — | `when` clamped to 6 sensor predicates; no authored C# reaches `CheckTransitions` |
| `hud` widgets | `label`, `source` → tooltip + on-screen text | `kind` clamped to 5; `id` → sanitised setter, collision-linted against the variant's real surface |

In all three, a hallucination costs a *word*, never a build.

---

## 5. Next adopters, ranked

**0. Rescue layers for SCENES, AUDIO, NARRATIVE, TITLE — the highest-value item.**
§1.6 measured it exactly: every brief section with a rescue survived at 100%,
every section without one was overwritten. Four sections currently have no
rescue, and the run lost 4/5 scene names, 7/10 audio cues, 3/6 narrative beats
and the project title to that gap. Build them on the same parse the
corresponding fixer uses, exactly as `unity_concept_entities` and
`unity_concept_scripts` already do. This makes LLM invention *purely additive*,
after which more invention is strictly better.

**1. `statemachine` — has the `ai` defect, unfixed.**
`_DEFAULT_TRANSITIONS` conditions are raw C#, LLM-authored `condition` strings
are unguarded (CS0103), and SEAM 4's `OnFSMStateChanged` hardcodes
`State.Idle/Moving/Jumping/Dead`. It is safe today *only* because the composer
never feeds it states. The moment it does, it breaks exactly as `ai` did. Port
the `when` vocabulary and the name/role split.

**2. Feed Step 3 the brief, not only the concept.**
The single highest-recall change available, and it needs no new schema: pass
`user_prompt` alongside `concept` into `UNITY_SCHEMA_FILL_USER`. The rescue
injectors become a safety net instead of the mechanism.

**3. Invert primacy — parse first, enrich second.**
For a structured brief, run `unity_briefparse` to build the spine (identity),
then ask the LLM to *enrich* that schema (behaviour prose, cue descriptors,
widget labels, state names, missions) rather than author it. `--no-llm` already
proves the spine builds alone.

Critically: **enrichment is not optional polish.** §1.6 shows the spine alone
ships generic audio and an unwinnable mission, because the meaning-driven
generators have nothing to read. "Parse first" fixes name loss; it does not
remove the need for the model.

**4. `trigger` / `interactable` / `spawner`.**
`actions[]` already accepts brief phrases. Give them the same split so an
invented action name becomes a real `SendMessage` target with a known handler
shape.

---

## 6. How to measure it

`--no-llm` is the control arm. Score every enrichment change as
`llm_arm − nollm_arm` on:

- brief-entity recall, brief-script recall
- mission objectives bound by SEAM 8
- seams fired
- emitted micro LOC
- Step 7 playability

**A change that raises variety while lowering recall is a regression.** The point
of the tiering is that those two stop trading against each other.

> Eval note: a `--no-llm` run is **not** a test of the Pipeline LLM and must never
> be reported as one. Step 3 does not run. It is the control. See `CLAUDE.md`
> § Eval Discipline and the module docstring in `src/unity_briefparse.py`.
