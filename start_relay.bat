@echo off
cd /d "%~dp0"

if not exist "relay.py" (
    echo ERROR: Missing file: relay.py
    echo The installation looks incomplete. Re-download and fully extract the zip, then try again.
    pause
    exit /b 1
)
if not exist "scrape_listing.py" (
    echo ERROR: Missing file: scrape_listing.py
    echo The installation looks incomplete. Re-download and fully extract the zip, then try again.
    pause
    exit /b 1
)
if not exist "relay_config.json" (
    echo ERROR: Missing file: relay_config.json
    echo Ask NC for the real relay_config.json — the one in the zip is a placeholder.
    pause
    exit /b 1
)

echo Starting Listing Tool Relay...
echo.
echo Keep this window open while using the shared Listing Tool webapp.
echo Close it when you are done.
echo.
if exist ".\python\python.exe" (
    .\python\python.exe relay.py
) else (
    python --version >nul 2>&1
    if %errorlevel% == 0 (
        python relay.py
    ) else (
        echo Python not found. Either run setup.bat first, or install Python
        echo from python.org and try again.
    )
)
pause
