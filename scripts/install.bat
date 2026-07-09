@echo off
REM ===========================================================================
REM  Bacon & Eggs / GTA-Unity compiler - full dependency installer (Windows)
REM
REM  Creates a local .venv and installs EVERYTHING the asset pipeline needs:
REM    - base Python deps (requirements.txt)
REM    - Playwright + a real headless Chromium  (Sketchfab live model source)
REM    - Kokoro-82M + soundfile                 (NPC dialog TTS)
REM    - PyTorch                                (Kokoro / Stable Audio inference)
REM
REM  Usage (double-click, or from a terminal at the repo root):
REM      scripts\install.bat
REM
REM  Optional heavy backends are commented out near the bottom (Stable Audio
REM  Open, Bark). The default `audio.backend: stable3` uses a separate local
REM  model install, not a pip package, so it is not installed here.
REM ===========================================================================

setlocal enableextensions
REM --- Move to repo root (this script lives in scripts\) ---------------------
cd /d "%~dp0.."
echo ==^> Repo root: %CD%

REM --- 1. Python present? ----------------------------------------------------
echo.
echo ==^> Checking for Python
where python >nul 2>nul
if errorlevel 1 (
    echo     ERROR: Python is not on PATH.
    echo     Install Python 3.11+ from https://python.org and re-run this script.
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo     %%v

REM --- 2. Virtual environment ------------------------------------------------
echo.
echo ==^> Creating virtual environment (.venv)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 ( echo     ERROR: venv creation failed. & exit /b 1 )
) else (
    echo     .venv already exists - reusing
)
set "PY=.venv\Scripts\python.exe"

REM --- 3. Upgrade pip + core build tools -------------------------------------
echo.
echo ==^> Upgrading pip / setuptools / wheel
"%PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 ( echo     ERROR: pip upgrade failed. & exit /b 1 )

REM --- 4. Base project dependencies ------------------------------------------
echo.
echo ==^> Installing base dependencies
if exist "requirements.lock" (
    echo     using requirements.lock (pinned)
    "%PY%" -m pip install -r requirements.lock
) else (
    echo     using requirements.txt
    "%PY%" -m pip install -r requirements.txt
)
if errorlevel 1 ( echo     ERROR: base dependency install failed. & exit /b 1 )

REM --- 5. Playwright + Chromium ----------------------------------------------
REM  Sketchfab's API sits behind AWS WAF Bot Control that fingerprints plain
REM  HTTP clients; only a real Chromium TLS handshake clears it. `playwright`
REM  is already in requirements.txt, but the browser binary must be fetched.
echo.
echo ==^> Installing Playwright + headless Chromium
"%PY%" -m pip install "playwright>=1.40"
if errorlevel 1 ( echo     ERROR: playwright pip install failed. & exit /b 1 )
"%PY%" -m playwright install chromium
if errorlevel 1 ( echo     ERROR: chromium download failed. & exit /b 1 )

REM --- 6. Kokoro-82M dialog TTS ----------------------------------------------
echo.
echo ==^> Installing Kokoro-82M dialog TTS (kokoro + soundfile)
"%PY%" -m pip install kokoro soundfile
if errorlevel 1 ( echo     WARNING: kokoro install failed - dialog will be text-only. )

REM --- 7. PyTorch (Kokoro / Stable Audio inference) --------------------------
REM  Default pip torch is CPU-only on Windows. For CUDA (recommended for the
REM  Stable Audio backend), instead run, matching your CUDA version, e.g.:
REM    "%PY%" -m pip install torch --index-url https://download.pytorch.org/whl/cu124
echo.
echo ==^> Installing PyTorch (CPU build; see comment for CUDA)
"%PY%" -m pip install torch
if errorlevel 1 ( echo     WARNING: torch install failed - Stable Audio / GPU TTS unavailable. )

REM --- 8. Optional heavy generative-audio backends (commented) ---------------
REM  Only needed for audio.backend: local (Stable Audio Open + Bark). The
REM  default audio.backend: stable3 uses a separate local model install.
REM  "%PY%" -m pip install stable-audio-tools
REM  "%PY%" -m pip install git+https://github.com/suno-ai/bark.git

REM --- 9. Bootstrap config.yaml ----------------------------------------------
echo.
echo ==^> Bootstrapping config.yaml
if not exist "config.yaml" (
    if exist "config.example.yaml" (
        copy /y "config.example.yaml" "config.yaml" >nul
        echo     copied config.example.yaml -^> config.yaml (edit it next)
    ) else (
        echo     config.example.yaml not found - author config.yaml by hand.
    )
) else (
    echo     config.yaml already present - leaving it alone
)

REM --- 10. Done -------------------------------------------------------------
echo.
echo ===========================================================================
echo  Install complete.
echo.
echo  Next steps:
echo    1. Edit config.yaml (audio backend, Blender path, etc.)
echo    2. For Sketchfab downloads, set your API token in this shell:
echo         set SKETCHFAB_API_TOKEN=your_token_here
echo    3. Activate the venv:   .venv\Scripts\activate.bat
echo    4. Try it:              python main.py serve
echo ===========================================================================
endlocal
