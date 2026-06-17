# Beta Tester Onboarding

Thanks for testing. This page gets you running in about 5 minutes. There is no
telemetry — everything runs locally on your machine.

---

## What you need

| Required                                                     | Why                                                  |
|---|---|
| Windows 10 / 11                                              | Current build is Windows-tested                      |
| Python 3.11 or newer ([python.org](https://python.org))     | Runtime                                              |
| [LM Studio](https://lmstudio.ai) **or** [Ollama](https://ollama.com) | Local LLM. Either one works.                        |
| ~6 GB free disk                                              | Python deps + a small LLM model                       |

**Optional:**
- [AnythingLLM](https://anythingllm.com) for sharper asset resolution. The
  app works without it — turn on **Basic Mode** in the UI and you'll use the
  bundled local lookup tables instead.

For GTA testing, additionally:
- [ScriptHookV](https://www.dev-c.com/gtav/scripthookv/)
- ScriptHookVDotNet (drops `ScriptHookVDotNet3.dll`)

For Unity testing, additionally:
- Unity Hub + the editor version `6000.0.66f2` (or whatever the project's
  `ProjectVersion.txt` declares).

---

## Install (two commands)

1. Unzip the build to a folder of your choice.
2. From PowerShell in that folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```
3. Then:
   ```powershell
   python main.py serve
   ```
4. The terminal prints a localhost URL — open it in your browser. The
   sidebar's **Tutorial** tab walks you through the first run.

---

## First-run walkthrough

The Tutorial tab will guide you, but in short:

1. **Backends** page → pick LM Studio or Ollama, point at the right URL,
   click **Test Connection**. The LLM light up top turns green.
2. **(Optional)** Hook up AnythingLLM the same way, then click **Create**
   next to each workspace row to bootstrap the GTA + Unity RAG workspaces.
   If you skip this, turn **Basic Mode** on instead.
3. **Prompt** page → pick GTA or Unity, type one sentence, hit **Generate**.
4. Watch the live progress. When it's done, the panel shows the absolute
   output path, a Copy-path button, and an Open-file-location button.
5. The **Library** page lists everything you've generated, with star
   ratings, the original prompt, and a Show-more preview of the LLM concept.

---

## How to report a bug

For each issue, please include:
- **Project / mission name** (visible in the Library row).
- **Date / time** of the run.
- **Backend used** (LM Studio / Ollama; model name).
- **Theme & seed** if you used them.
- **What you expected vs. what happened.**
- The **schema concept** (Library row → "Show more" → copy the text). This is
  the single most useful piece of context.
- For GTA: paste the relevant excerpt from
  `Grand Theft Auto V/ScriptHookVDotNet.log`.
- For Unity: copy the Console line(s) (right-click → Copy).

The full **per-output checklist** is in `docs/TESTING.md` — testers don't have
to run every box, but if a generated mission/project doesn't behave, that's
the structured place to check what failed.

---

## Privacy

- No telemetry. The app never phones home.
- The web UI is bound to `127.0.0.1` only — nothing exposes a port to your
  network unless you change the `--host` flag yourself.
- The only outbound traffic is to your locally running LM Studio / Ollama /
  AnythingLLM servers.
- Your AnythingLLM API key lives in `config.yaml` and is masked in the
  Backends page.

---

## License & redistribution

This is a private invite-only beta. © 2026 — All rights reserved. Please do
not redistribute the build, post screenshots publicly, or commit changes to a
public repo without permission.
