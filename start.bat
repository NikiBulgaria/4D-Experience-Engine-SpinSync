@echo off
setlocal
title SpinSync - Show Controller

echo.
echo   ============================================
echo     SpinSync  -  Hardware Synced Show Player
echo   ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo   [X] Python was not found on this PC.
    echo.
    echo   Install Python 3.10 or newer from https://python.org
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   [1/3] Python %PYVER% found.

echo   [2/3] Checking dependencies...
python -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>nul
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo   [X] Dependencies failed to install.
    echo       Try running this command yourself to see why:
    echo         python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo   [2/3] Dependencies ready.

echo   [3/3] Starting SpinSync...
echo.
python main.py %*

if errorlevel 1 (
    echo.
    echo   The application exited with an error. The message above explains why.
    echo.
    pause
)

endlocal
