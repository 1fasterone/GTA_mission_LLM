# Quickstart — describe a game, get a wired Unity project

Five minutes from zero to a Unity project assembled from the micro library.

## 1. Prerequisites

- **Python 3.11+**
- **[LM Studio](https://lmstudio.ai)** (free) — runs a local LLM. Load any instruct model
  (Llama 3.1 8B, Qwen 2.5/3.5, etc.) and start its server (**Developer → Start Server**, port 1234).
- **Unity 6000.x** (via Unity Hub) — only needed to open/verify the generated project.

## 2. Install

```powershell
./scripts/install.ps1        # makes a .venv, installs pinned deps (requirements.lock)
```
Or manually:
```bash
python -m venv .venv && . .venv/Scripts/activate    # (.venv/bin/activate on macOS/Linux)
pip install -r requirements.lock
cp config.example.yaml config.yaml                  # then set lm_studio.model (see step 3)
```
RAG (local ChromaDB index) is **optional** — skip `rag-build` to run in local-lookup mode.

## 3. Pick your model

Set `lm_studio.model` in `config.yaml` to the id shown in LM Studio (e.g.
`meta-llama-3.1-8b-instruct`), **or** override per-run with `--model`:

```bash
python main.py unity-tools list          # see all 15 micro-generators
```

## 4. Generate a composed game

```bash
python main.py unity-create --compose "a top-down survival game with waves of enemies" \
  --model meta-llama-3.1-8b-instruct
```
`--compose` assembles micros into one **wired** project: a controller on the Player, a camera rig
on the Main Camera, a HUD + pause menu on the Canvas, a health system wired to the HUD bar, plus
save/audio/AI on a `_Systems` object — all connected by a generated `GameWiring.cs`. Output lands in
`output/unity/<ProjectName>/`.

Want a single subsystem instead of a whole game? Any micro runs standalone:
```bash
python main.py unity-health "player with regenerating shields" --variant shielded
python main.py unity-hud "minimal health + ammo HUD" --variant full_hud_bundle
```

## 5. Open it / verify it

- **Open:** Unity Hub → *Add project from disk* → pick `output/unity/<ProjectName>/`.
- **Headless compile-check** (needs Unity installed):
  ```bash
  python unity_cli.py verify --source output/unity/<ProjectName>
  ```
  Prints `PASS — compiles clean` or every C# error mapped back to its template.

## Troubleshooting

- **"LM Studio did not respond / empty response"** — load a model and start the server on `:1234`.
  Reasoning models (e.g. Phi-4-reasoning) are slow; raise `lm_studio.timeout` in `config.yaml`.
- **Web UI** instead of CLI: `python main.py serve` → http://127.0.0.1:8000
- **Different model?** Just `--model <id>` — the pipeline is hardened across Qwen / Llama / deepseek.
