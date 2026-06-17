# Campaign — Operational Pack

> Internal playbook. Not for distribution. © 2026 — All rights reserved.

This is the tactical, day-by-day companion to the strategic plan in
`docs/MARKETING.md`. Use it like a checklist: don't memorize, *check off*.

Two trackers live next to this doc:
- `docs/campaign/targets.csv` — every person you DM, with status.
- `docs/campaign/post_log.md` — every public post, with what landed.

Update both daily during the campaign. They're the difference between
"running a campaign" and "drifting on social media."

---

## Pre-flight — once, before any of this

- [ ] One handle locked across **X, Bluesky, Reddit, Discord, YouTube**. Same string, same avatar, same bio everywhere.
- [ ] Bio (verbatim everywhere it fits):
  > *Building Prototype Forge — a local scaffolder that turns one sentence into a playable Unity project or GTA mod. Back to share it.*
- [ ] 15-second demo GIF recorded *once* (specs in §6 below). Save as `prototype_forge_demo.gif` somewhere you can find it cold.
- [ ] Beta zip ready: `dist/GTA_mission_LLM_*.zip` (build it fresh from `scripts/package.ps1` before each invite wave).
- [ ] One-line invite blurb saved in your clipboard manager.

---

## §1 — Subreddit reference card

Per-subreddit rules digest, refreshed against actual sidebars before posting.

### r/Unity3D — ~1M members — **strict**
- **Posting window**: self-promo allowed weekends only. Weekdays = removed.
- **Account age**: ~7 days minimum, comment karma required (their AutoModerator filters cold accounts).
- **Content**: must include media (GIF/video). Link-only posts get nuked.
- **Title**: descriptive, not click-bait. *"I built a local scaffolder that turns one sentence into a Unity project — closed beta"* > *"Check this out!"*
- **Tag**: flair as `Question` or `Show-Off` (use what the sub has).
- **Cadence**: max 1 self-promo per 30 days.
- **Posting checklist**:
  - [ ] Saturday or Sunday between 10am–2pm EST (their peak)
  - [ ] GIF embedded (not linked)
  - [ ] First comment from you: the category sentence + DM-open
  - [ ] Camp the thread 2 hours
  - [ ] Do not reply to negative comments — block + move on

### r/GTAModding — ~80k members — **friendly**
- **Posting window**: anytime.
- **Account age**: no hard filter; some karma helps.
- **Content**: tag posts. Use `Showcase` flair for tool launches.
- **Format**: Streamable or Imgur preferred for video; direct Reddit video has lower reach.
- **Cadence**: max 1 launch post; devlog posts (2nd, 3rd, etc.) are fine.
- **Posting checklist**:
  - [ ] Title: *"I built a local scaffolder that turns one sentence into a playable .3.cs mission script — closed beta"*
  - [ ] Embed GIF + link to a Streamable copy for higher-res viewing
  - [ ] Mention "drop into `Grand Theft Auto V/scripts/`" so readers know it's SHVDN3
  - [ ] Camp the thread 2 hours; reply to every legitimate question

### r/IndieDev — ~700k members — **moderate**
- **Posting window**: Feedback Friday or weekends.
- **Account age**: ~3 days minimum.
- **Content**: must have a "what's the story" component. Pure tool posts feel out of place.
- **Best angle**: post as a **devlog**, not as a launch. Reframe as *"Devlog #1: I built this in 8 weeks, here's where I'm at."*
- **Cadence**: 1 post per week max; mods notice clusters.

### r/proceduralgeneration — ~140k members — **technical**
- **Posting window**: anytime; Source Saturday is the weekly highlight thread.
- **Audience**: wants the algorithm, not the marketing. Show how the schema → code compilation works, not just the output.
- **Best angle**: *"How I'm using LLMs as a spec writer for a deterministic code compiler (no procedural content via the LLM itself)"* — that thesis is *interesting* to this sub. Pure "look at the GTA mission" is not.
- **Cadence**: 1 post; this is a specialty audience, save for Phase 4 devlog.

### r/gamedev — 1.5M members — **stricter than r/Unity3D**
- **Self-promo**: only allowed in the weekly Self-Promotion Saturday thread.
- **Posts at any other time**: auto-removed.
- **Best use of this sub**: it's a *reading* sub for you, not a posting sub. Look for "I'm struggling with X" posts where X is what your tool solves. Comment helpfully; if appropriate, soft-pitch in DM.

### r/Unity2D — ~80k members — **friendly**
- **Best angle**: only after the 2D-controller micro generator has been verified by a tester. Frame the post around the *2D Platformer* variant: *"Built a scaffolder for 2D Unity prototypes — generates the controller, camera, and HUD wiring in one go."*

---

## §2 — Discord reference card

Per-server rules digest. Update channel names by checking each server when you join (they rename).

### Official Unity Discord — `discord.gg/unity` — ~150k members
- **Channels**:
  - `#self-promo` (or similar) — where launches go. Read its pinned rules.
  - `#scripting`, `#help`, `#urp`, `#hdrp` — Phase 1 helpful-reply territory.
  - **Do not use** `#asset-store-assets` (gated; off-topic for this tool anyway — see `docs/GTA_CUSTOM_MODELS.md`-style context: this is a scaffolder, not a package).
- **Posting rules**:
  - 1 post per 7 days in `#self-promo`.
  - Must include media.
  - Reply-engage for 60+ min after posting.
- **Pre-warm**: lurk 3 days, answer 5+ questions in `#scripting`/`#help` first.

### ScriptHookV / SHVDN community Discord
- **Find via**: pinned message in r/GTAModding sidebar or via the ScriptHookV download page.
- **Channels**:
  - `#releases` or `#showcase` — where mod tool launches go.
  - `#scripting-help` — Phase 1 territory.
- **Posting rules**: typically permissive but no spam; one post is fine, multiple raise eyebrows.
- **Best angle**: *"Generates a ready-to-drop `.3.cs` for SHVDN3; closed beta if you mod"*.

### Indie game-dev Discords (pick 2)
- **Game Dev League** (`discord.gg/gamedev`) — large, well-moderated, friendly.
- **Indie Game Developers** — search the IndieDev subreddit sidebar.
- **Channels**: `#show-and-tell`, `#feedback`, `#tools`.
- **Posting rules**: tenure-based, similar to Unity Discord. Lurk first.

### Discord etiquette baseline (all servers)
- **Never** post the same content in multiple channels of the same server. That's the #1 ban trigger.
- **Never** DM mods to ask permission. Read the rules; follow them; let the post stand.
- **Never** link the zip directly. Always "DM if interested" → manual handoff.
- **Always** reply to questions in the thread, not via DM. Visibility = social proof.

---

## §3 — DM templates by tier and platform

### Tier 1: Rolodex (people you know)

> *"Hey [name] — built a thing you'd probably have opinions on. Sentence in, playable GTA mission or Unity project out in under 10 min. Closed beta of ~10 people. Want a build to break?"*

15-second GIF attached. Wait. If they say yes, DM the zip + this:

> *"Cool — here's the build: [zip URL]. README_FIRST.txt walks you through it. There's a checklist in docs/TESTING.md if you want to do this seriously, but honest first-impression feedback in one paragraph is fine too. No rush — break it on your schedule."*

### Tier 2: Warm Discord contact (someone you helpfully replied to)

> *"Hey — saw your question about [thing] last week. I've actually been building a tool that bumps into that exact problem: it scaffolds a [Unity / GTA] project from a one-line prompt in about 5 minutes. Closed beta if you want a build to try."*

### Tier 3: Cold but pre-qualified (Reddit post they made)

> *"Saw your post about [their specific topic]. The scaffolder I built would have shortcut a chunk of that — local app, one-sentence prompt, outputs a working [.3.cs / Unity project] in under 10 min. Closed beta if you want a build."*

### Reddit DM specifics
- Reddit DMs have no formatting in the inbox preview. Lead with the value, not the greeting.
- Subject line: *"Quick question — saw your [topic] post"* (questions get opened more than statements).
- Keep under 300 characters total. Long DMs read as spam.

### Discord DM specifics
- One message, not three. Don't split "hey" / "btw I built" / "here's the link" across messages.
- No emoji in the opener. Looks bot-shaped.

### X / Twitter DM specifics
- Quote-DM their tweet, don't open with a generic pitch. *"Re: [tweet]: this is something I built for exactly that. Closed beta if you want a build."*
- DMs from non-mutuals go to filtered requests on X; expect lower reply rate.

### When they say no (or ghost)
- Mark "no" or "ghost" in `targets.csv` and **never DM them again** about this tool. Re-engaging dead leads is what kills your credibility on Reddit specifically.

### When they say yes but never report back
- One follow-up after 7 days: *"Hey — anything to report? Even 'haven't tried it yet, no time' is useful feedback."*
- One follow-up after 14 days: *"All good if this isn't your week. Mute the DM and I'll resurface if there's a major update."*
- After that: silent. They got the build; if it matters to them they'll come back.

---

## §4 — Post checklists by platform

Copy these into your post-prep notes the morning of.

### Unity Discord #self-promo

- [ ] 2+ weeks of helpful presence in `#scripting`/`#help` first
- [ ] GIF attached (not linked)
- [ ] First message body (verbatim):
  > *"Built a local scaffolder that turns one sentence into a Unity project or a playable GTA mission. This isn't an AI assistant — it's a schema-first scaffolder with a brief writer. The compiler builds the project. Closed beta, DMs open."*
- [ ] Stay in the channel for 60+ minutes after posting
- [ ] Update `post_log.md` with timestamp + initial reactions

### r/GTAModding launch post

- [ ] Title: *"I built a local scaffolder that turns one sentence into a playable .3.cs mission script — closed beta"*
- [ ] Streamable.com link to the demo GIF as primary content
- [ ] First (top) comment from you: *"Drops into `Grand Theft Auto V/scripts/` — requires ScriptHookV + ScriptHookVDotNet3.dll. Not an AI assistant; it's a scaffolder with an LLM-written brief step. The compiler is pure Python. Closed beta — DM if you mod. Happy to answer any technical questions."*
- [ ] Camp the thread for 2 hours
- [ ] Don't engage flame; block + move on
- [ ] Update `post_log.md`

### r/Unity3D launch post

- [ ] Saturday or Sunday 10am–2pm EST only
- [ ] Account age ≥ 14 days, comment karma ≥ 50
- [ ] Self-Promo flair (or whatever the sub has currently)
- [ ] Title: *"I built a local scaffolder for Unity projects — sentence in, ready-to-open project folder out in under 10 minutes. Closed beta."*
- [ ] Video upload to Reddit native + a Streamable mirror in first comment
- [ ] First comment from you: same category sentence as Discord, plus *"manifest resolves automatically, scenes wired, render pipeline configured. DM if you want a build."*
- [ ] Camp 2 hours
- [ ] Update `post_log.md`

### X / Twitter launch post

- [ ] Video format (not GIF — X demotes GIF reach)
- [ ] Hook in the first line: *"Sentence in. Playable Unity project out. 10 minutes."*
- [ ] Body: the category sentence
- [ ] No link in the body. Pinned reply with: *"Closed beta — reply or DM if you mod or prototype."*
- [ ] Tag nobody. Tag-spam is the worst-performing pattern.
- [ ] Reply to every comment for 4 hours after posting
- [ ] Update `post_log.md`

### Bluesky mirror

- [ ] Same video as X
- [ ] Same caption (Bluesky has no character limit issue at this length)
- [ ] Add `#gamedev #unity3d #indiedev` (Bluesky hashtags actually work, unlike X)
- [ ] Update `post_log.md`

---

## §5 — Day-by-day schedule (Phase 0 → Phase 3, days 1–30)

Copy/paste day-by-day. Cross off as you go.

**Day 1** (Setup, 2h once)
- [ ] Handle locked everywhere
- [ ] Avatars + bios up
- [ ] Discord servers joined (Unity, ScriptHookV, 2 indie). No messages yet.
- [ ] Reddit accounts ready

**Day 2** (Hygiene polish)
- [ ] X profile finished, pinned post: *"Closed beta running now. DM if you mod or prototype."*
- [ ] Bluesky profile finished
- [ ] YouTube channel created (no upload)
- [ ] Add `prototype_forge_demo.gif` to a folder where you can grab it fast

**Day 3** (Lurk only)
- [ ] 30 min scrolling each platform; bookmark 10 posts that show what your tribe is talking about

**Day 4–10** (Phase 1: Be useful)
Each day:
- [ ] X: reply once with a real helpful answer
- [ ] Reddit: comment once in r/Unity3D or r/GTAModding
- [ ] Discord: answer one question in `#scripting`/`#help` (any server)
- [ ] Bookmark people with relevant problems → `targets.csv` (Tier 3 column)

By end of day 10:
- [ ] 20+ helpful replies
- [ ] 10–15 people on the Tier-3 list

**Day 11–12** (Tier 1 DMs)
- [ ] Tier-1 list compiled (rolodex)
- [ ] Day 11: DM 6 people
- [ ] Day 12: DM 6 more
- [ ] Update `targets.csv` after each DM

**Day 13–14** (Tier 2 DMs)
- [ ] Day 13: 4 warm Discord DMs
- [ ] Day 14: 4 more

**Day 15–17** (Tier 3 DMs)
- [ ] 3 personalized DMs per day to your Tier-3 list

End of Phase 2 target: **3 tester invitations sent and accepted.**

**Day 18** (First public post)
- [ ] Unity Discord `#self-promo` post (checklist §4)
- [ ] Camp the thread 60 min
- [ ] Update `post_log.md`

**Day 19–20** (Recover + reply)
- [ ] No new posts
- [ ] Answer any leftover Discord thread replies

**Day 21** (Reddit launch — friendly sub first)
- [ ] r/GTAModding launch post (checklist §4)
- [ ] Camp the thread 2 hours
- [ ] Update `post_log.md`

**Day 22–23** (Recover)
- [ ] Daily reply rounds continue, no new posts

**Day 24** (X launch)
- [ ] X video post (checklist §4)
- [ ] Reply for 4 hours
- [ ] Update `post_log.md`

**Day 25–26** (Recover)

**Day 27** (Reddit launch — bigger sub)
- [ ] r/Unity3D launch post (Saturday/Sunday 10am–2pm EST only)
- [ ] Camp 2 hours
- [ ] Update `post_log.md`

**Day 28–29** (Recover)

**Day 30** (Bluesky mirror)
- [ ] Bluesky video post
- [ ] Update `post_log.md`

End of Phase 3 target: **8 testers total.**

---

## §6 — Demo GIF / video spec

Record **once**. Re-use everywhere.

### Settings
- **Resolution**: 1280×720, 30fps. Bigger isn't better — Reddit and X recompress aggressively.
- **Length**: **15 seconds** (the loop) for GIFs; **45–60 seconds** for X video and Streamable mirror.
- **Format**:
  - GIF for Discord and embeds: `.gif` under 8MB.
  - Video for X / Reddit / Streamable: `.mp4`, H.264, AAC audio, under 50MB.

### Scenes (15-second loop)

| Time | What's on screen | Notes |
|------|------------------|-------|
| 0.0–2.0s | Empty prompt textarea, cursor blinking | No music, no voiceover |
| 2.0–4.0s | Typing *"Heist the docks at night."* | Realistic typing speed |
| 4.0–5.0s | Click Generate | Highlight the click |
| 5.0–10.0s | Live progress: STEP 2, STEP 3, STEP 4 (RAG hits), STEP 5, STEP 6 | Fast cut, ~5 lines visible |
| 10.0–12.0s | Output panel appears with absolute path + Copy/Open buttons | Pause briefly |
| 12.0–15.0s | Cut to GTA in-game — player at the start blip, mission starts | Action shot, motion |

Loop back to 0.0s. The whole thing reads as: *"Type → Generate → Play"* in one breath.

### What to **not** include in the demo
- No face cam.
- No voiceover.
- No "AI" or "ChatGPT" overlay.
- No download URLs.
- No watermarks beyond your handle in the bottom-right corner (optional, small).

### Tools to record with
- **ShareX** (Windows, free) — GIF + video, the indie-dev default.
- **OBS** for the 60-second video version.
- **Gifski** or **ezgif.com** for size optimization if the raw GIF is too big.

---

## §7 — Mental-load triage

When the campaign gets noisy (it will), use this triage:

| Signal                                          | Action                                       |
|-------------------------------------------------|----------------------------------------------|
| Helpful question in thread                      | Answer publicly                              |
| Tester DM with feedback                         | Answer within 24h, log in `targets.csv`     |
| Curious DM (not your target audience)           | Polite *"Beta is closed right now, follow X for updates"* — one reply, then mute |
| Snarky reply                                    | Don't engage. Block. Move on.                |
| Insightful critique                             | Save to `post_log.md`. Reply once with a thank-you. |
| "Is this open source?"                          | *"Closed for the beta. Re-evaluating after."* |
| "What about [competitor X]?"                    | Don't compare. *"Different tradeoff — I'm focused on local + deterministic."* Move on. |
| Off-topic philosophical AI debate               | Don't engage. Block if persistent.           |

---

## §8 — End-of-week ritual (every Sunday, 30 min)

- [ ] Read `post_log.md` from this week. What landed? What didn't?
- [ ] Read `targets.csv`. Any DMs to follow up on?
- [ ] One paragraph in `post_log.md` under "Week N retrospective": what to do more of, what to do less of.
- [ ] Plan next week's posts (mark days in §5).
- [ ] Close laptop. Saturday tomorrow. You're done.

---

## Reference

- Strategic frame: `docs/MARKETING.md`
- Tester onboarding: `docs/TESTERS.md`
- Per-output checklist: `docs/TESTING.md`
- Free asset sourcing: `docs/FREE_ASSETS.md`
- GTA custom-model context: `docs/GTA_CUSTOM_MODELS.md`
