@echo off
chcp 65001 >nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ToFetch" /f 2>nul
echo 已关闭 ToFetch 开机自启。
pause
