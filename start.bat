@echo off
rem Double-click to start ytarchive and open its interface.
rem It checks for a yt-dlp update on the way in, if one is due.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py ytarchive.py ui
) else (
  python ytarchive.py ui
)
if errorlevel 1 (
  echo.
  echo ytarchive stopped with an error. Press any key to close.
  pause >nul
)
