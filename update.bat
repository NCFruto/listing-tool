@echo off
title Listing Tool - Update to v1.3

set "TOOL_DIR=%~dp0"

cls
echo.
echo ============================================================
echo   Listing Tool - Update to v1.3
echo   This fixes a bug that caused listing details to go blank.
echo   Takes about 30 seconds. Please don't close this window.
echo ============================================================
echo.

:: ── Check that setup has already been run ────────────────────────────────────
if not exist "%TOOL_DIR%python\python.exe" (
    echo  ERROR: Python runtime not found.
    echo  It looks like you haven't run setup.bat yet on this computer.
    echo  Please run setup.bat first, then try this update again.
    echo.
    pause
    exit /b 1
)

:: ── Remove old package ────────────────────────────────────────────────────────
echo  [1/3]  Removing old browser driver...
"%TOOL_DIR%python\python.exe" -m pip uninstall undetected-chromedriver -y >nul 2>&1
echo  Done.

:: ── Install new package ───────────────────────────────────────────────────────
echo  [2/3]  Installing new browser driver (nodriver)...
echo         This may take 15-30 seconds...
"%TOOL_DIR%python\python.exe" -m pip install "nodriver>=0.50" --quiet
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Installation failed.
    echo  Check your internet connection and try again.
    echo  If it keeps failing, contact NC for help.
    echo.
    pause
    exit /b 1
)
echo  Done.

:: ── Smoke test ────────────────────────────────────────────────────────────────
echo  [3/3]  Verifying update...
"%TOOL_DIR%python\python.exe" -c "import nodriver, flask, requests, bs4, PIL" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Verification failed.
    echo  Contact NC for help.
    echo.
    pause
    exit /b 1
)
echo  Done.

:: ── Success ──────────────────────────────────────────────────────────────────
echo.
echo ============================================================
echo  Update complete!
echo.
echo  You can now close this window and use the Listing Tool
echo  as normal. Listing details (address, agents, description)
echo  will be extracted correctly again.
echo ============================================================
echo.
pause
