@echo off
rem ============================================================
rem  ImageDB launcher (Windows)
rem  First run: creates .venv and installs dependencies.
rem  Subsequent runs: starts directly.
rem ============================================================
title ImageDB
cd /d "%~dp0"

rem ---------- Check virtual environment ----------
if exist ".venv\Scripts\python.exe" goto :start

echo [ImageDB] First run detected. Setting up environment...
where python >nul 2>nul
if errorlevel 1 (
    echo [ImageDB] ERROR: Python not found.
    echo Please install Python 3.10+ and make sure it is in PATH.
    pause
    exit /b 1
)

echo [ImageDB] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo [ImageDB] ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo [ImageDB] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [ImageDB] Installing dependencies (first run only)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ImageDB] ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

:start
echo [ImageDB] Starting ImageDB...
".venv\Scripts\python.exe" main.py %*
pause
