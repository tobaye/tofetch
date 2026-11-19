@echo off
cd /d "%~dp0"
title ToFetch
set "PYTHONUTF8=1"
set "ARIA2_BIN=%~dp0aria2c.exe"
set "TOFETCH_PORT=8077"
set "TOFETCH_HOST=127.0.0.1"
set "TOFETCH_DOWNLOAD_DIR=%USERPROFILE%\Downloads\ToFetch"
if not exist "%TOFETCH_DOWNLOAD_DIR%" mkdir "%TOFETCH_DOWNLOAD_DIR%"
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8077"
echo ToFetch is running. Your browser will open http://localhost:8077
echo Downloaded files go to: %TOFETCH_DOWNLOAD_DIR%
echo Close this window to stop ToFetch.
"%~dp0python\python.exe" "%~dp0server.py"
pause
