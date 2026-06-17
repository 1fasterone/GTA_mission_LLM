# Marketing Plan

> Internal playbook. Not for distribution. © 2026 — All rights reserved.

This document is the positioning, tester-acquisition, and launch script for
the project. Keep it in sync as decisions change.

---

## 1. Category & positioning

**Do not call this an "AI assistant."** That framing reduces the tool to
ChatGPT-with-extras and hides what's actually new.

### The category

> **Prototype Forge** — a *local, schema-first scaffolder with an optional
> AI brief step*.

### Why those words

| Word              | Job it does                                                              |
|-------------------|--------------------------------------------------------------------------|
| **Forge**         | Real artifacts come out: a `.3.cs`, a Unity project folder. Not chat.    |
| **Scaffolder**    | Ends the boilerplate, like `create-react-app` did for React.             |
| **Schema-first**  | The LLM writes a brief; a deterministic compiler builds from the schema. |
| **Local**         | No cloud, no API bill, no data exit. Distinct from Cursor / Devin.       |
| **Optional AI**   | Basic Mode runs purely from local lookups. AI is a feature, not the product. |

### The one-liner

> *You bring the idea. It writes the brief. The compiler builds the project.*

### Words to use

scaffolder · forge · compiler · brief · spec · schema-first · deterministic
· local · boilerplate killer · first-hour generator · playable in 10 minutes
· drop-in mod · ready-to-open project

### Words to avoid

AI assistant · AI copilot · AI agent · ChatGPT for game devs · LLM-powered ·
AI-generated game · generative AI · prompt-to-game · "just describe what you
want and AI makes it"

### The defensive sentence (comment-pin everywhere)

> *This isn't an AI assistant. It's a scaffolder with a brief writer. The
> AI writes the spec. The compiler builds the project.*

---

## 2. Tester acquisition (private beta — 5–20 people)

A private beta is a **DM-first operation**, not a posting operation.
Public posts = public beta. Stay in DMs until the smoke gate is fully clean
and 3-5 testers have round-tripped end-to-end.

### Tier 1 — your rolodex (day one)

- Phone contacts, Discord friends, X DMs, jam teammates. Find anyone who has
  ever shipped a GTA mod, a Unity prototype, or a game-jam game.
- Send each one a **two-line message + 10-second screen-recording GIF** (not
  a link, not the zip):

> *"Hey — I built a scaffolder that turns a sentence into a playable GTA
> mission or a Unity project in under 10 min. Closed beta of ~10 people,
> looking for honest first-impression feedback. Want a build?"*

- Expected conversion: 30–50%. Target 5 invites from this tier alone.

### Tier 2 — small niche Discords

The people who would actually drop a `.3.cs` into `scripts/` and tell you
whether it loaded.

- ScriptHookV / ScriptHookVDotNet community Discords.
- Cinemachine / Unity render-pipeline Discords.
- Indie gamedev Discords you're already in (GameDev.tv, IndieDev, etc.).

Drop the same two-line pitch in show-and-tell. **Don't link.** Say "DM me if
interested." Filters out tourists.

### Tier 3 — targeted Reddit DMs (not posts)

- Search r/GTAModding, r/Unity3D, r/proceduralgeneration for posts from the
  last 30 days where someone said *"I'm trying to make a mission script
  that…"* or *"starting a new Unity project…"*.
- **DM that person.** They are pre-qualified. They have an active need.
- Public posts: still off the table until smoke is green.

### Tier 4 — one high-leverage tweet (use sparingly)

- 15-second loop GIF: sentence typed → live progress → mission running.
- Caption: *"Closed beta of a local prototype forge. Sentence → playable
  mission in 10 min. Reply if you mod or prototype, DMs open."*
- No link in the post. Reply to comments with a DM.

### Anti-patterns

- Don't post the zip URL publicly.
- Don't launch on Product Hunt / Show HN / r/SideProject yet — those are
  launch venues, not beta venues.
- Don't run a Google Form. DMs are higher-signal at this scale.

### Wave structure

3 testers → fold issues → 7 more → fold → final 10. **Don't ship to 20 at
once.** Every wave should incorporate lessons from the previous.

---

## 3. YouTube launch script (90 seconds)

For **after** the beta signal is positive. The framing never says "AI
assistant."

### Title

**"I built a tool that turns one sentence into a playable GTA mission in 10
minutes"**

### Thumbnail

Split-screen. Left: a tiny chat-bubble that says *"Heist the docks at night."*
Right: GTA gameplay screenshot mid-mission with a HUD blip and the timestamp
*"0:09:42."* Bold word across the middle: **FORGED**.

### Script

**[0:00 – 0:07] COLD OPEN — no talking**
- Cut: a sentence typed into a text field: *"Infiltrate the docks at night
  and steal a drug shipment."*
- Cut: progress lines streaming.
- Cut: a `.3.cs` file dragged into `Grand Theft Auto V/scripts/`.
- Cut: in-game, player walks up to a yellow blip, mission starts, HUD pops.

**[0:07 – 0:14] HOOK**
> "That took about nine minutes. Not nine days. **And it's not an AI
> assistant.**"

**[0:14 – 0:28] PROBLEM**
- B-roll: empty Unity project, `Packages/manifest.json` open in a code
  editor, camera scrolling slowly down a long list of dependencies.

> "Every Unity project starts the same way. Pick a render pipeline. Add
> Cinemachine. Add Input System. Set up the manifest. Configure scenes.
> Write a boilerplate controller. A day before you write a line of actual
> gameplay."

- Cut to a SHVDN3 mission template open in VS Code with `// TODO`s.

> "GTA mods are worse. Spawn syntax, ped models, blip handling — a
> thousand-line skeleton before your mission does anything."

**[0:28 – 1:00] DEMO — live, no cuts**
- Screen capture: the web UI's Prompt page in the dark theme.

> "So I built a scaffolder. You type a sentence. A local language model
> writes the brief — the spec — for what the mission should do. Then a
> deterministic Python compiler reads the spec and builds the actual files."

- Screen: typing *"Heist at the airport hangar, vintage muscle car, three
  guards, escape through the desert."* Click Generate.
- Screen: live progress streaming — STEP 2, STEP 3, STEP 4 with RAG hits,
  STEP 5 fixes, STEP 6 file write.
- Screen: output panel shows `C:\…\HangarHeist.3.cs` with Copy and Open
  buttons.
- Screen: drag into `scripts/`, alt-tab to GTAV, mission runs.

**[1:00 – 1:15] PAYOFF**
> "Five seconds to type. Three minutes to generate. The compiler does the
> rest. **The AI writes one step out of six.** The other five — schema
> validation, asset resolution, consistency fixes, code generation — are
> pure Python. Deterministic. Reproducible. The same schema regenerates the
> same script."

**[1:15 – 1:25] CATEGORY**
> "This is a **local prototype forge**. Schema-first scaffolder, with an
> optional AI brief step. Same idea for Unity — sentence in, ready-to-open
> project folder out, manifest correct, scenes wired, packages resolved."

**[1:25 – 1:30] CTA**
> "Closed beta right now. DM me. Link in the description when it opens up."

### Longer devlog (3–5 min) additions

Keep the 90-second skeleton; insert between 0:28 and 1:00:

- **B-1 (40s) — Why deterministic:** show running the same prompt twice with
  the same seed, getting the same output. *"The AI is the brief writer, but
  the build is reproducible."*
- **B-2 (40s) — Why local:** show the Backends page with LM Studio running
  on localhost. *"No API bill, no data leaves my machine, works on the
  plane."*

### Production notes

- **Record using a fresh prompt you've never tried.** Don't cherry-pick. The
  honesty of "watch me actually run this" is the differentiator from every
  AI-tool launch video that's secretly rehearsed.
- **First 7 seconds carry the video.** Open with cold output, not with you
  on camera.
- **Don't show your face for the launch.** Product, not creator channel.
- **Pinned comment**:
  > *"This isn't an AI assistant. It's a scaffolder with a brief writer.
  > The compiler builds the project."*

---

## 4. DM templates

### Tier 1 (someone you already know)

> *"Hey [name] — built a thing you'd probably have opinions on. Sentence in,
> playable GTA mission or Unity project out in under 10 min. Closed beta of
> ~10 people. Want a build to break?"*

### Tier 2 (small Discord show-and-tell)

> *"Been working on a local prototype forge — turns a one-line idea into a
> playable GTA mod (.3.cs, drop into scripts/) or a ready-to-open Unity
> project in under 10 minutes. Looking for a handful of testers before I open
> it up. DM if you want a build."*

### Tier 3 (Reddit DM to someone who recently posted relevant)

> *"Saw your post about [their specific topic]. Built a scaffolder that
> would have shortcut a chunk of that — local app, one-sentence prompt,
> outputs a working [.3.cs / Unity project] in under 10 min. Closed beta if
> you want a build."*

---

## 5. The post-launch sequence (Iteration 5+)

When the beta signal is positive and you flip to public:

1. **YouTube video first** — using the script above.
2. **Reddit post on r/GTAModding + r/Unity3D** the same day, both linking to
   the video, never to a download.
3. **Unity Forum thread** the day after — links to the video, technical tone.
4. **Show HN** when GitHub goes public.
5. Re-evaluate license at that point — open-source is a one-way door.

---

## 6. Status

- Category language: **locked.**
- DM templates: **draft, ready to send.**
- YouTube script: **draft, needs recording.**
- Free-asset support (so testers can drop in their own models): see
  `docs/FREE_ASSETS.md` — testers will need this to make videos.
- GTA custom-model injection: see `docs/GTA_CUSTOM_MODELS.md` for how
  AddOn models flow through the schema → script path.
