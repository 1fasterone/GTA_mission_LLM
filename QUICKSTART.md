# Quick start

This guide takes a fresh clone to a generated Unity project using a local LLM.

## 1. Install the prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai/) or [Ollama](https://ollama.com/)
- Unity 6000.x if you want to open or compile-check the generated project

## 2. Create the environment

Windows PowerShell:

```powershell
git clone https://github.com/1fasterone/GTA_mission_LLM.git
cd GTA_mission_LLM
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
Copy-Item config.example.yaml config.yaml
```

macOS or Linux:

```bash
git clone https://github.com/1fasterone/GTA_mission_LLM.git
cd GTA_mission_LLM
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
cp config.example.yaml config.yaml
```

## 3. Start a model server

For LM Studio, load an instruction model, start the local server, and set
`lm_studio.model` in `config.yaml` to the exact identifier LM Studio exposes.
The default endpoint is `http://localhost:1234/v1`.

For Ollama, pull a model and change `lm_studio.backend` to `ollama`:

```bash
ollama pull qwen2.5:7b
```

Then set `ollama.model` to the pulled tag.

## 4. Generate a game

```bash
python main.py unity-create --compose "a top-down survival game with waves of enemies"
```

The generated project and its canonical schema are written under
`output/unity/<ProjectName>/`.

To use a reference image:

```bash
python main.py unity-create --compose --viz reference.png "a bright sci-fi arena shooter"
```

## 5. Open or verify the project

- In Unity Hub, choose **Add project from disk** and select the generated
  project directory.
- For a headless compile check, run:

```bash
python unity_cli.py verify --source output/unity/<ProjectName>
```

## 6. Replay without the model

```bash
python main.py unity-validate output/unity/<ProjectName>/<ProjectName>.schema.json --compose
```

The replay uses the saved schema and frozen enrichment data. Steps 4–7 remain
LLM-free.

## Optional paths

```bash
python main.py serve
python main.py unity-tools list
python main.py generate "infiltrate the docks at night"
python main.py rag-status
```

RAG is optional. Set `basic_mode: true` in `config.yaml` to use the bundled
local lookup catalogs only.

## Troubleshooting

- **Connection refused:** load a model and start the configured local server.
- **Model not found:** copy the model identifier exactly as the server exposes it.
- **Unity not found:** configure the Unity path in `config.yaml` or open the
  generated project manually in Unity Hub.
- **The compiler abstains:** follow the named remedy in the error; do not hide
  the missing or ambiguous input with a silent fallback.
- **Need all options:** run `python main.py unity-create --help`.
