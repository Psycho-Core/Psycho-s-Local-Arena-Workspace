@echo off
setlocal
cd /d "%~dp0"

rem --- Arena Workspace Bridge launcher for Windows ---
rem Prefers the Windows Python launcher (py), falls back to python.

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 arena_bridge.py %*
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python arena_bridge.py %*
  ) else (
    echo Python was not found on this PC.
    echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
    echo or run:  winget install Python.Python.3.12
    pause
    exit /b 1
  )
)

if errorlevel 1 pause
