# Contributing

Thanks for helping improve GTA Mission LLM.

## Ground rules

- Treat the repository as a compiler, not a chat chain.
- The Pipeline LLM may run only in Steps 2, 3, and 3.5.
- Steps 4–7 must stay deterministic and LLM-free.
- Never hand-author a schema and present it as pipeline evaluation output.
- Do not invent atom types or schema fields outside the documented
  `extensions` namespace.
- Change compiler sources or templates, regenerate outputs, and validate the
  result. Generated Unity files are not the source of truth.
- Abstain clearly when ground truth is missing; do not add silent fallbacks.

Read [the freeze-boundary contract](docs/LLM_FREEZE_BOUNDARY.md) and
[schema-extension contract](docs/SCHEMA_EXTENSIONS.md) before changing either.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt pytest mypy ruff
cp config.example.yaml config.yaml
```

Never commit API keys, local model credentials, generated caches, or private
asset credentials.

## Before opening a pull request

Run the checks that apply to your change:

```bash
ruff check src main.py cli rag
mypy src main.py cli rag
pytest -q tests/test_frozen_step35_acceptance.py
pytest
```

Unity runtime changes also need generated-project compilation and advancing
Play Mode evidence. Describe the exact project, Unity version, scene, observed
behavior, and any rejection stage in the pull request.

## Pull requests

- Keep each pull request focused on one capability or repair.
- Explain the expected behavior, actual behavior, and deterministic remedy.
- Identify any transformed evaluation input explicitly.
- Include tests and update the relevant contract documentation.
- Resolve review conversations and keep the branch current with `main`.
- Do not weaken tests, freeze-boundary enforcement, or provenance checks merely
  to make CI pass.

By contributing, you agree that your contribution is licensed under the
repository's GNU AGPL v3-or-later license.
