@echo off
setlocal
title Fix Icon Cache - Hearizon Sync
cd /d "%~dp0"

rem ============================================================
rem  Windows icon cache is very sticky: it keeps showing the OLD
rem  icon even after you install a new version.
rem  This script rebuilds the icon cache so the new icon appears.
rem  (Pure ASCII on purpose - no codepage problems.)
rem ============================================================

echo.
echo ============================================
echo   Rebuild Windows Icon Cache
echo ============================================
echo.
echo This will:
echo   1. Close Windows Explorer
echo   2. Delete the icon cache database
echo   3. Restart Windows Explorer
echo.
echo Your desktop will blink for a moment. That is normal.
echo.

choice /C YN /M "Continue"
if errorlevel 2 goto cancel

echo.
echo [1/4] Stopping Explorer...
taskkill /f /im explorer.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/4] Deleting icon cache...
del /f /s /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /f /s /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*.db" >nul 2>&1
del /f /s /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache*.db" >nul 2>&1

echo [3/4] Restarting Explorer...
start explorer.exe
timeout /t 3 /nobreak >nul

echo [4/4] Refreshing shell...
ie4uinit.exe -show >nul 2>&1

echo.
echo ============================================
echo   Done. The new icon should show now.
echo ============================================
echo.
echo If the OLD icon still shows in the taskbar only,
echo unpin it and pin it again.
echo.
pause
exit /b 0

:cancel
echo Cancelled.
pause
exit /b 1
