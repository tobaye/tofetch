@echo off
chcp 65001 >nul
reg delete "HKCU\Software\Classes\magnet" /f 2>nul
reg delete "HKCU\Software\Classes\.torrent" /f 2>nul
reg delete "HKCU\Software\Classes\ToFetch.torrent" /f 2>nul
echo 已取消磁力链接和 .torrent 文件与 ToFetch 的关联。
pause
