@echo off
setlocal EnableDelayedExpansion
title Listing Tool Relay - First-time Setup

set "TOOL_DIR=%~dp0"
set "LOG=%TOOL_DIR%setup_relay.log"
set "WEBAPP_URL=https://209-38-94-2.sslip.io"

echo [%date% %time%] Relay setup started > "%LOG%"
echo. >> "%LOG%"

cls
echo.
echo ============================================================
echo   Listing Tool Relay - First-time Setup
echo   This will take 2-3 minutes. Please don't close this window.
echo ============================================================
echo.
echo  This installs a small background helper, NOT the full app.
echo  The Listing Tool itself lives at: %WEBAPP_URL%
echo  This helper just lets that shared site fetch listings using
echo  YOUR internet connection, since realestate.com.au blocks
echo  requests coming from the shared server directly.
echo.

:: ============================================================
::  Pre-flight  Verify all required files are present
:: ============================================================
echo  Checking required files...
echo [%date% %time%] Pre-flight: verifying required files >> "%LOG%"

set "MISSING="
for %%F in (relay.py scrape_listing.py start_relay.bat requirements.txt relay_config.json) do (
    if not exist "%TOOL_DIR%%%F" set "MISSING=%%F"
)

if defined MISSING (
    echo.
    echo  ERROR: Missing file: !MISSING!
    echo  The download may be incomplete or you may have extracted only part
    echo  of the zip. Re-download the zip, extract ALL of it, then try again.
    echo.
    echo [%date% %time%] ERROR: Missing required file: !MISSING! >> "%LOG%"
    pause
    exit /b 1
)
echo  All required files present. OK.
echo [%date% %time%] Pre-flight OK >> "%LOG%"

:: ============================================================
::  1/6  Check for Google Chrome
:: ============================================================
call :progress 1 "Checking for Google Chrome"

set "CHROME_EXE="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe"          set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"          set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not defined CHROME_EXE (
    echo.
    echo  ERROR: Google Chrome was not found on this computer.
    echo  Chrome is required for the relay to fetch listings.
    echo.
    echo  Please install Chrome from https://google.com/chrome
    echo  then run setup_relay.bat again.
    echo.
    echo [%date% %time%] ERROR: Chrome not found >> "%LOG%"
    start https://google.com/chrome
    pause
    exit /b 1
)
echo  Chrome found. OK.
echo [%date% %time%] Chrome: %CHROME_EXE% >> "%LOG%"

:: ============================================================
::  2/6  Download Python 3.11.9 embeddable  (skipped on re-run)
:: ============================================================
if exist "%TOOL_DIR%python\python.exe" (
    call :progress 2 "Python already installed - skipping download"
    echo [%date% %time%] Python already present, skipping download >> "%LOG%"
    goto :configure_python
)

call :progress 2 "Downloading Python 3.11.9 (~30 MB)"
echo  Downloading... please wait (this may take 1-2 minutes).
echo [%date% %time%] Downloading Python 3.11.9 embeddable >> "%LOG%"

powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile '%TOOL_DIR%python_embed.zip' -UseBasicParsing } catch { Write-Error $_; exit 1 }" >> "%LOG%" 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Python download failed.
    echo  Your network may be blocking the download - contact NC for help.
    echo  See setup_relay.log for details.
    echo [%date% %time%] ERROR: Python download failed >> "%LOG%"
    pause
    exit /b 1
)

echo  Download complete. Extracting...
echo [%date% %time%] Extracting Python embeddable >> "%LOG%"
powershell -NoProfile -Command "try { Expand-Archive -Path '%TOOL_DIR%python_embed.zip' -DestinationPath '%TOOL_DIR%python' -Force } catch { Write-Error $_; exit 1 }" >> "%LOG%" 2>&1
del "%TOOL_DIR%python_embed.zip" 2>nul

if not exist "%TOOL_DIR%python\python.exe" (
    echo.
    echo  ERROR: Python extraction failed.
    echo  A file may have been blocked by antivirus software.
    echo  Try disabling antivirus temporarily, or contact NC for help.
    echo [%date% %time%] ERROR: python.exe missing after extraction >> "%LOG%"
    pause
    exit /b 1
)
echo  Python ready. OK.
echo [%date% %time%] Python extracted OK >> "%LOG%"

:configure_python
:: ============================================================
::  3/6  Configure embeddable Python
:: ============================================================
call :progress 3 "Configuring Python"
echo [%date% %time%] Configuring embeddable Python >> "%LOG%"

powershell -NoProfile -Command "(Get-Content '%TOOL_DIR%python\python311._pth') -replace '^#import site','import site' | Set-Content '%TOOL_DIR%python\python311._pth'" >> "%LOG%" 2>&1

if exist "%TOOL_DIR%python\Scripts\pip.exe" (
    echo  pip already installed. Skipping.
    echo [%date% %time%] pip already present, skipping >> "%LOG%"
    goto :install_deps
)

echo  Downloading pip installer...
echo [%date% %time%] Downloading get-pip.py >> "%LOG%"
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TOOL_DIR%get-pip.py' -UseBasicParsing } catch { Write-Error $_; exit 1 }" >> "%LOG%" 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: pip download failed - contact NC for help. See setup_relay.log.
    echo [%date% %time%] ERROR: get-pip.py download failed >> "%LOG%"
    pause
    exit /b 1
)

echo  Installing pip...
echo [%date% %time%] Running get-pip.py >> "%LOG%"
"%TOOL_DIR%python\python.exe" "%TOOL_DIR%get-pip.py" >> "%LOG%" 2>&1
del "%TOOL_DIR%get-pip.py" 2>nul

if not exist "%TOOL_DIR%python\Scripts\pip.exe" (
    echo.
    echo  ERROR: pip installation failed - contact NC for help. See setup_relay.log.
    echo [%date% %time%] ERROR: pip.exe not found after install >> "%LOG%"
    pause
    exit /b 1
)
echo  pip installed. OK.
echo [%date% %time%] pip installed OK >> "%LOG%"

:install_deps
:: ============================================================
::  4/6  Install app dependencies  (live output shown on screen)
:: ============================================================
call :progress 4 "Installing packages"
echo  Each package will appear below as it downloads:
echo  (First run takes 1-2 minutes - please wait)
echo.
echo [%date% %time%] Running pip install -r requirements.txt >> "%LOG%"
"%TOOL_DIR%python\python.exe" -m pip install -r "%TOOL_DIR%requirements.txt"
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Package installation failed. Contact NC for help.
    echo  (Take a screenshot of this window if you need to report the error.)
    echo [%date% %time%] ERROR: pip install failed >> "%LOG%"
    pause
    exit /b 1
)
echo.
echo  All packages installed. OK.
echo [%date% %time%] pip install completed OK >> "%LOG%"

:: ============================================================
::  5/6  Smoke test
:: ============================================================
call :progress 5 "Verifying installation"
echo [%date% %time%] Running smoke test >> "%LOG%"
"%TOOL_DIR%python\python.exe" -c "import flask, requests, bs4, PIL, nodriver" >> "%LOG%" 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Verification failed - a package did not install correctly.
    echo  See setup_relay.log for the full error. Contact NC for help.
    echo [%date% %time%] ERROR: Smoke test failed >> "%LOG%"
    pause
    exit /b 1
)
echo  All packages verified. OK.
echo [%date% %time%] Smoke test passed >> "%LOG%"

:: ============================================================
::  6/6  Desktop shortcut
:: ============================================================
call :progress 6 "Creating desktop shortcut"
echo [%date% %time%] Creating desktop shortcut >> "%LOG%"

powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Listing Tool Relay.lnk'); $s.TargetPath = '%TOOL_DIR%start_relay.bat'; $s.WorkingDirectory = '%TOOL_DIR%'; $s.Description = 'Start the Listing Tool Relay helper'; $s.IconLocation = '%CHROME_EXE%,0'; $s.Save()" >> "%LOG%" 2>&1

echo  Shortcut created on your Desktop. OK.
echo [%date% %time%] Shortcut created OK >> "%LOG%"

:: ============================================================
::  Done
:: ============================================================
echo.
echo ============================================================
echo  [####################]  6/6  Setup complete!
echo.
echo  Press any key to start the relay helper.
echo ============================================================
echo.
echo [%date% %time%] Setup complete >> "%LOG%"
pause > nul

echo [%date% %time%] Launching relay >> "%LOG%"
start "Listing Tool Relay" "%TOOL_DIR%start_relay.bat"

echo.
echo  The relay helper is starting in a new window - leave that
echo  window open whenever you're using the Listing Tool.
echo.
echo  Now open the shared Listing Tool in your browser:
echo    %WEBAPP_URL%
echo.
echo  Use the "Listing Tool Relay" shortcut on your Desktop to
echo  start the helper again next time.
echo.
pause
exit /b 0

:: ============================================================
::  Subroutine: :progress STEP "Label"
::  Prints:  [####----------------] Step N/6  Label
:: ============================================================
:progress
setlocal EnableDelayedExpansion
set /a "filled=%~1 * 20 / 6"
set /a "empty=20 - filled"
set "bar="
if %filled% gtr 0 for /l %%i in (1,1,%filled%) do set "bar=!bar!#"
if %empty%  gtr 0 for /l %%i in (1,1,%empty%)  do set "bar=!bar!-"
echo.
echo  [!bar!] Step %~1/6  %~2
echo.
endlocal
goto :eof
