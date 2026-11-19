@echo off
reg delete "HKCU\Software\Classes\magnet" /f >nul 2>&1
reg delete "HKCU\Software\Classes\ToFetch.magnet" /f >nul 2>&1
reg delete "HKCU\Software\Classes\.torrent" /f >nul 2>&1
reg delete "HKCU\Software\Classes\ToFetch.torrent" /f >nul 2>&1
reg delete "HKCU\Software\ToFetch" /f >nul 2>&1
reg delete "HKCU\Software\RegisteredApplications" /v "ToFetch" /f >nul 2>&1
echo Done: magnet links and .torrent files are no longer associated with ToFetch.
pause
