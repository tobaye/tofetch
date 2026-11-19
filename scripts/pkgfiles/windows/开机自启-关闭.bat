@echo off
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ToFetch" /f >nul 2>&1
echo Done: ToFetch autostart is off.
pause
