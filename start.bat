set "NULLDEV=NUL"
@echo off
REM Launches the school notice workbench (school-radar).
REM Keep this file ASCII-only on purpose: cmd.exe mis-parses batch files whose
REM comment lines contain multi-byte characters, and it then tries to *execute*
REM the tail of the comment. Chinese instructions live in README.md.
setlocal
cd /d "%~dp0"

REM Find an interpreter that actually runs. "python" on a fresh Windows box is
REM often the Microsoft Store stub, which opens the Store instead of running.
set "PY="
py -3 -c "import sys" 1>%NULLDEV% 2>&1 && set "PY=py -3"
if not defined PY (py -c "import sys" 1>%NULLDEV% 2>&1 && set "PY=py")
if not defined PY (python -c "import sys" 1>%NULLDEV% 2>&1 && set "PY=python")
if not defined PY (python3 -c "import sys" 1>%NULLDEV% 2>&1 && set "PY=python3")

if not defined PY (
  echo [school-radar] No usable Python found.
  echo Install Python 3.9 or newer and tick "Add python.exe to PATH", then
  echo double-click this file again.  Download:
  echo   https://www.python.org/downloads/windows/
  echo Chinese step-by-step guide: README.md
  echo.
  pause
  exit /b 1
)

%PY% -c "import requests,bs4,lxml" 1>%NULLDEV% 2>&1
if errorlevel 1 (
  echo [school-radar] Installing dependencies: requests beautifulsoup4 lxml
  %PY% -m pip install requests beautifulsoup4 lxml
  if errorlevel 1 (
    echo [school-radar] pip install failed. Run it by hand:
    echo   %PY% -m pip install requests beautifulsoup4 lxml
    echo.
    pause
    exit /b 1
  )
)

if /i "%~1"=="doctor" (
  %PY% app.py --doctor
  echo.
  pause
  exit /b 0
)

%PY% app.py
if errorlevel 1 pause
