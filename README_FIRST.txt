Bacon & Eggs — a combinatorial game-design tool (private beta bundle)
=====================================================================

(c) 2026 — All rights reserved. Do not redistribute.

Describe a game, get a wired Unity project composed from 15 drop-in subsystems.
(GTA5 mission generation is a secondary showcase.)

Install Python 3.11 first. From PowerShell in this folder, run:

    py -3.11 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements.lock
    if (!(Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml }
    .\.venv\Scripts\python.exe main.py serve

No PowerShell activation script is needed. On later visits, run only the last
command from this folder. Existing config.yaml settings are kept.

The terminal will print a URL like http://127.0.0.1:8000 — open it in your
browser. Pick a model, choose "Compose Game", and describe your game.

Fast happy-path guide:    QUICKSTART.md
Full tester guide:        docs\TESTERS.md
Per-output checklist:     docs\TESTING.md

Reporting bugs: see docs\TESTERS.md for the issue template and where to send
them. Please include the captured concept from the Library row when you file
an issue — it's the single most useful piece of context.

Thanks for testing.
