@echo off
cd /d "%~dp0"
set "PY=%~dp0python\pythonw.exe"
set "OPENER=%~dp0tofetch-open.py"
set "FAIL=0"

rem --- ToFetch ProgIds. The open command is deleted first: some
rem --- download managers lock their old keys, and a fresh create
rem --- succeeds more often than an overwrite.
reg add "HKCU\Software\Classes\ToFetch.magnet" /ve /d "URL:Magnet Link" /f >nul || set "FAIL=1"
reg add "HKCU\Software\Classes\ToFetch.magnet" /v "URL Protocol" /d "" /f >nul || set "FAIL=1"
reg delete "HKCU\Software\Classes\ToFetch.magnet\shell\open\command" /f >nul 2>&1
reg add "HKCU\Software\Classes\ToFetch.magnet\shell\open\command" /ve /d "\"%PY%\" \"%OPENER%\" \"%%1\"" /f >nul || set "FAIL=1"
reg add "HKCU\Software\Classes\.torrent" /ve /d "ToFetch.torrent" /f >nul || set "FAIL=1"
reg add "HKCU\Software\Classes\ToFetch.torrent" /ve /d "ToFetch torrent file" /f >nul || set "FAIL=1"
reg delete "HKCU\Software\Classes\ToFetch.torrent\shell\open\command" /f >nul 2>&1
reg add "HKCU\Software\Classes\ToFetch.torrent\shell\open\command" /ve /d "\"%PY%\" \"%OPENER%\" \"%%1\"" /f >nul || set "FAIL=1"

rem --- Direct protocol key (fast path when nothing locked magnet).
reg add "HKCU\Software\Classes\magnet" /ve /d "URL:Magnet Link" /f >nul || set "FAIL=1"
reg add "HKCU\Software\Classes\magnet" /v "URL Protocol" /d "" /f >nul || set "FAIL=1"
reg delete "HKCU\Software\Classes\magnet\shell\open\command" /f >nul 2>&1
reg add "HKCU\Software\Classes\magnet\shell\open\command" /ve /d "\"%PY%\" \"%OPENER%\" \"%%1\"" /f >nul || set "FAIL=1"

rem --- Capabilities, so ToFetch also appears in Settings > Default apps.
reg add "HKCU\Software\ToFetch\Capabilities" /v "ApplicationName" /d "ToFetch" /f >nul
reg add "HKCU\Software\ToFetch\Capabilities" /v "ApplicationDescription" /d "ToFetch download manager" /f >nul
reg add "HKCU\Software\ToFetch\Capabilities\UrlAssociations" /v "magnet" /d "ToFetch.magnet" /f >nul
reg add "HKCU\Software\ToFetch\Capabilities\FileAssociations" /v ".torrent" /d "ToFetch.torrent" /f >nul
reg add "HKCU\Software\RegisteredApplications" /v "ToFetch" /d "Software\ToFetch\Capabilities" /f >nul

echo.
if "%FAIL%"=="1" (
  echo PARTLY FAILED: another app ^(e.g. Quark^) has locked the magnet / .torrent
  echo association keys, so they could not be overwritten.
  echo Workaround: Settings ^> Apps ^> Default apps ^> choose by protocol,
  echo find MAGNET and pick ToFetch there - it is registered, so it
  echo will show up in that list. Same for .torrent under "by file type".
) else (
  echo Done: magnet links and .torrent files now open with ToFetch
  echo ^(current user only, no admin needed^).
  echo To undo, run the unregister .bat file in this folder.
)
pause
