@echo off
cd /d "%~dp0"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ToFetch" /d "\"%~dp0ToFetch-Background.bat\"" /f
echo Done: ToFetch will start in the background when you sign in to Windows.
echo Open http://localhost:8077 in your browser to use it.
pause
