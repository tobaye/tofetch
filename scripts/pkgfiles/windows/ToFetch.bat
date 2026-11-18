@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ToFetch
set "PYTHONUTF8=1"
set "ARIA2_BIN=%~dp0aria2c.exe"
set "TOFETCH_PORT=8077"
set "TOFETCH_HOST=127.0.0.1"
set "TOFETCH_DOWNLOAD_DIR=%USERPROFILE%\Downloads\ToFetch"
if not exist "%TOFETCH_DOWNLOAD_DIR%" mkdir "%TOFETCH_DOWNLOAD_DIR%"
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8077"
echo ToFetch 正在运行，浏览器会自动打开 http://localhost:8077
echo 下载的文件保存在：%TOFETCH_DOWNLOAD_DIR%
echo 关闭这个窗口即可停止 ToFetch。
"%~dp0python\python.exe" "%~dp0server.py"
pause
