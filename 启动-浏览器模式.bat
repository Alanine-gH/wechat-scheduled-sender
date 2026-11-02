@echo off
REM ===================================================================
REM  Browser-mode launcher (for debugging only, no Electron).
REM  ASCII ONLY. Chinese messages are printed by run.py.
REM ===================================================================
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
    echo.
    pause
    exit /b 1
)

python "%~dp0run.py" %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo [ERROR] exited with code %RC%
pause
exit /b %RC%