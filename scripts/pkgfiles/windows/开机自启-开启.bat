@echo off
chcp 65001 >nul
cd /d "%~dp0"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ToFetch" /d "\"%~dp0ToFetch-后台.bat\"" /f
echo 已设置开机自启：登录 Windows 后 ToFetch 会在后台运行，
echo 浏览器打开 http://localhost:8077 即可使用。
pause
