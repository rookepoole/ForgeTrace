@echo off
setlocal
cd /d "%~dp0"
title ForgeTrace Local Repository
echo.
echo Starting ForgeTrace at http://127.0.0.1:8765
echo Repository files are stored in: %CD%\workspace
echo Press Ctrl+C to stop.
echo.
py server.py --port 8765 2>nul || python server.py --port 8765
