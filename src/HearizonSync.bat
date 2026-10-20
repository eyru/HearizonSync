@echo off
setlocal
title Hearizon Sync
cd /d "%~dp0"

rem ============================================================
rem  Pure ASCII on purpose: avoids every Windows codepage issue.
rem  Files are matched with wildcards, never typed literally.
rem ============================================================

set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONIOENCODING=utf-8"

rem ---- locate python in the bundled venv ----
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if defined PYEXE goto havepy

echo [ERROR] .venv not found. Run setup first.
echo   Expected: %~dp0.venv\Scripts\python.exe
pause
exit /b 1

:havepy
rem ---- locate the main script ----
set "SCRIPT="
for %%F in ("%~dp0*.py") do if not defined SCRIPT set "SCRIPT=%%~fF"
if not defined SCRIPT goto noscript
if /i "%%~nxF"=="" goto noscript

echo Starting Hearizon Sync...
echo   Python: %PYEXE%
echo   Script: %SCRIPT%
echo.
"%PYEXE%" "%SCRIPT%"
if errorlevel 1 goto failed
exit /b 0

:noscript
echo [ERROR] No .py script found next to this file.
pause
exit /b 1

:failed
echo.
echo [ERROR] Program exited with errors. See messages above.
pause
exit /b 1
