@echo off
rem ToFetch background start (no console window, no browser) - used by
rem the autostart entry; downloads land in Downloads\ToFetch as usual.
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "ARIA2_BIN=%~dp0aria2c.exe"
set "TOFETCH_PORT=8077"
set "TOFETCH_HOST=127.0.0.1"
set "TOFETCH_DOWNLOAD_DIR=%USERPROFILE%\Downloads\ToFetch"
if not exist "%TOFETCH_DOWNLOAD_DIR%" mkdir "%TOFETCH_DOWNLOAD_DIR%"
start "" /min "%~dp0python\pythonw.exe" "%~dp0server.py"
