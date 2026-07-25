@echo off
setlocal
cd /d "%~dp0"
title ForgeTrace Local Repository Workspace

set "FORGETRACE_PYTHON="
where py >nul 2>nul
if %errorlevel%==0 set "FORGETRACE_PYTHON=py -3"
if not defined FORGETRACE_PYTHON (
  where python >nul 2>nul
  if %errorlevel%==0 set "FORGETRACE_PYTHON=python"
)

if not defined FORGETRACE_PYTHON (
  echo.
  echo ForgeTrace could not find Python.
  echo Install Python 3.10 or newer from python.org, then run this file again.
  pause
  exit /b 1
)

echo.
echo ================================================
echo   ForgeTrace - Local Repository Workspace
echo ================================================
echo.
echo This package will open only after its own server binds successfully.
echo If port 8765 is already used by an older ForgeTrace package, close it first.
echo Owner workspace: http://127.0.0.1:8765
echo Press Ctrl+C in this window to stop ForgeTrace and all sharing.
echo.

%FORGETRACE_PYTHON% server.py --port 8765 --open-browser
set "FORGETRACE_EXIT=%errorlevel%"

if not "%FORGETRACE_EXIT%"=="0" (
  echo.
  echo ForgeTrace stopped with exit code %FORGETRACE_EXIT%.
  pause
)
exit /b %FORGETRACE_EXIT%
