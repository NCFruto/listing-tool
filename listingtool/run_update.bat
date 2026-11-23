@echo off
cd /d "%~dp0"
if exist "..\python\python.exe" (
    ..\python\python.exe update_pipeline.py > update_log.txt 2>&1
) else (
    python update_pipeline.py > update_log.txt 2>&1
)
pause
