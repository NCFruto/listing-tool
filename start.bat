@echo off
cd /d "%~dp0"

if not exist "app.py" (
    echo ERROR: Missing file: app.py
    echo The installation looks incomplete. Re-run setup.bat,
    echo or re-download and fully extract the zip, then try again.
    pause
    exit /b 1
)
if not exist "scrape_listing.py" (
    echo ERROR: Missing file: scrape_listing.py
    echo The installation looks incomplete. Re-run setup.bat,
    echo or re-download and fully extract the zip, then try again.
    pause
    exit /b 1
)

echo Starting Listing Tool...
echo.
echo Keep this window open while using the app.
echo Close it when you are done.
echo.
if exist ".\python\python.exe" (
    .\python\python.exe app.py
) else (
    python --version >nul 2>&1
    if %errorlevel% == 0 (
        python app.py
    ) else (
        echo Python not found. Either run setup.bat first, or install Python
        echo from python.org and try again.
    )
)
pause
