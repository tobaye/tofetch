@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0python\pythonw.exe"
set "OPENER=%~dp0tofetch-open.py"
reg add "HKCU\Software\Classes\magnet" /ve /d "URL:Magnet Link" /f
reg add "HKCU\Software\Classes\magnet" /v "URL Protocol" /d "" /f
reg add "HKCU\Software\Classes\magnet\shell\open\command" /ve /d "\"%PY%\" \"%OPENER%\" \"%%1\"" /f
reg add "HKCU\Software\Classes\.torrent" /ve /d "ToFetch.torrent" /f
reg add "HKCU\Software\Classes\ToFetch.torrent" /ve /d "ToFetch torrent file" /f
reg add "HKCU\Software\Classes\ToFetch.torrent\shell\open\command" /ve /d "\"%PY%\" \"%OPENER%\" \"%%1\"" /f
echo 已关联：磁力链接和 .torrent 文件将用 ToFetch 打开（仅当前用户，不需要管理员）。
echo 不想用了就运行「关联-取消.bat」。
pause
