@echo off
setlocal
chcp 65001 >nul
title Install Character Studio Beta
cd /d "%~dp0"

set "PACKAGE_MODE_ARG="
if exist ".git" set "PACKAGE_MODE_ARG=--repository-worktree"

echo.
echo Character Studio Beta Installer
echo ================================
echo Checking for Python 3.10 or newer...

set "FOUND_PYTHON="
where py.exe >nul 2>&1 && set "FOUND_PYTHON=1"
where py.cmd >nul 2>&1 && set "FOUND_PYTHON=1"
where py.bat >nul 2>&1 && set "FOUND_PYTHON=1"
call py -3 -c "import sys; raise SystemExit(0 if sys.version_info[0] == 3 and sys.version_info[1] in range(10, 100) else 1)" >nul 2>&1
if not errorlevel 1 goto install_with_py

where python.exe >nul 2>&1 && set "FOUND_PYTHON=1"
where python.cmd >nul 2>&1 && set "FOUND_PYTHON=1"
where python.bat >nul 2>&1 && set "FOUND_PYTHON=1"
call python -c "import sys; raise SystemExit(0 if sys.version_info[0] == 3 and sys.version_info[1] in range(10, 100) else 1)" >nul 2>&1
if not errorlevel 1 goto install_with_python

echo.
if defined FOUND_PYTHON (
  echo Installation failed: the detected Python version is older than 3.10.
) else (
  echo Installation failed: Python was not found.
)
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo Select "Add Python to PATH" during setup, then double-click this file again.
goto failed

:install_with_py
echo Python is ready. Installing...
call py -3 scripts\install_studio.py %PACKAGE_MODE_ARG%
if errorlevel 1 goto installer_failed
goto success

:install_with_python
echo Python is ready. Installing...
call python scripts\install_studio.py %PACKAGE_MODE_ARG%
if errorlevel 1 goto installer_failed
goto success

:installer_failed
echo.
echo Installation did not complete. Review the message above.
echo Details: CharacterConsistencyStudio\logs\studio-install.log
goto failed

:success
echo.
echo Installation complete.
echo Double-click the "Character Studio Beta" desktop shortcut.
echo If shortcut creation failed, the installer printed a fallback launcher path above.
if not defined CHARACTER_STUDIO_INSTALL_NO_PAUSE pause
exit /b 0

:failed
echo.
echo This window will stay open so you can review the error.
if not defined CHARACTER_STUDIO_INSTALL_NO_PAUSE pause
exit /b 2
