@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Chemistry Lab Local Companion Agent
cd /d "%~dp0"

echo ===============================================================
echo  Chemistry Lab Local Agent Installer and Launcher
echo ===============================================================
echo.

set "AGENT_DIR=%LOCALAPPDATA%\ChemistryLabAgent"
set "VENV_DIR=%AGENT_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "SUPPORTED_PY="

:: Setup PYTHONPATH to locate local_agent package
if exist "%~dp0local_agent" (
    set "PYTHONPATH=%~dp0;!PYTHONPATH!"
) else if exist "%~dp0..\..\local_agent" (
    set "PYTHONPATH=%~dp0..\..;!PYTHONPATH!"
)

:: Locate requirements-local-agent.txt (handles both extracted package and repo checkout)
set "REQS_FILE=%~dp0requirements-local-agent.txt"
if not exist "!REQS_FILE!" (
    if exist "%~dp0..\..\requirements-local-agent.txt" (
        set "REQS_FILE=%~dp0..\..\requirements-local-agent.txt"
    )
)

:: 1. Check if existing venv has supported Python (3.11 to 3.13)
if exist "!VENV_PY!" (
    "!VENV_PY!" -c "import sys; v=sys.version_info; sys.exit(0 if 3<=v[0] and 11<=v[1] and v[1]<14 else 1)" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        goto :RUN_VENV
    ) else (
        echo [*] Note: Existing virtual environment Python does not satisfy requirements (3.11 to 3.13)
        echo [*] Recreating virtual environment with a supported Python version...
        rmdir /s /q "!VENV_DIR!" 2>nul
    )
)

:: 2. Discover supported Python interpreter using py launcher
py -3.12 -c "import sys; sys.exit(0)" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    set "SUPPORTED_PY=py -3.12"
    goto :FOUND_PY
)

py -3.13 -c "import sys; sys.exit(0)" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    set "SUPPORTED_PY=py -3.13"
    goto :FOUND_PY
)

py -3.11 -c "import sys; sys.exit(0)" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    set "SUPPORTED_PY=py -3.11"
    goto :FOUND_PY
)

:: 3. Check python on PATH
where python >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    python -c "import sys; v=sys.version_info; sys.exit(0 if 3<=v[0] and 11<=v[1] and v[1]<14 else 1)" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        set "SUPPORTED_PY=python"
        goto :FOUND_PY
    )
)

:: 4. Check python3 on PATH
where python3 >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    python3 -c "import sys; v=sys.version_info; sys.exit(0 if 3<=v[0] and 11<=v[1] and v[1]<14 else 1)" >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        set "SUPPORTED_PY=python3"
        goto :FOUND_PY
    )
)

:: 5. Check well-known Python installation directories
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python313\python.exe"
) do (
    if exist "%%~p" (
        "%%~p" -c "import sys; v=sys.version_info; sys.exit(0 if 3<=v[0] and 11<=v[1] and v[1]<14 else 1)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "SUPPORTED_PY=%%~p"
            goto :FOUND_PY
        )
    )
)

:: 6. If no supported Python found, prompt user
echo ===============================================================
echo  [ERROR] Supported Python version [3.11 to 3.13] not found.
echo  Chemistry Lab requires Python 3.11, 3.12, or 3.13.
echo  Python 3.14 is currently unsupported by scientific packages.
echo ===============================================================
echo.
set /p INSTALL_PY="Would you like to install Python 3.12 using winget? [Y/N]: "
if /i "!INSTALL_PY!"=="Y" (
    echo [*] Installing official Python 3.12 package via winget...
    winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    echo.
    echo Installation complete. Please re-run install_and_run.bat.
    pause
    exit /b 0
) else (
    echo Python 3.11 to 3.13 is required. Please install it from https://www.python.org and re-run.
    pause
    exit /b 1
)

:FOUND_PY
echo [*] Selected Python interpreter: !SUPPORTED_PY!
if not exist "!AGENT_DIR!" mkdir "!AGENT_DIR!"

echo [*] Creating dedicated virtual environment in !VENV_DIR!...
!SUPPORTED_PY! -m venv "!VENV_DIR!"
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to create virtual environment with !SUPPORTED_PY!.
    pause
    exit /b 1
)

:RUN_VENV
echo [*] Checking and installing Local Agent requirements...
"!VENV_PY!" -m pip install --quiet --upgrade pip
if exist "!REQS_FILE!" (
    "!VENV_PY!" -m pip install --quiet -r "!REQS_FILE!"
)
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
)

echo.
echo [*] Starting Chemistry Lab Local Agent...
echo.
set PYTHONUNBUFFERED=1
"!VENV_PY!" -m local_agent.agent start
set AGENT_EXIT_CODE=!ERRORLEVEL!

if !AGENT_EXIT_CODE! NEQ 0 (
    echo.
    echo [ERROR] Agent stopped with error code !AGENT_EXIT_CODE!.
    pause
    exit /b !AGENT_EXIT_CODE!
)

echo.
echo ===============================================================
echo  Chemistry Lab Local Agent has stopped.
echo ===============================================================
pause
exit /b 0

