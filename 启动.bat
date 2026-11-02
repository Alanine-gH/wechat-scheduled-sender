@echo off
REM ===================================================================
REM  WeChat Scheduled Sender - Electron launcher
REM  ASCII ONLY. See README: cmd.exe reads .bat with the system ANSI
REM  codepage, so UTF-8 Chinese here would desync the parser.
REM ===================================================================
cd /d "%~dp0"

where node >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Node.js not found.
    echo         Please install Node.js 18+ from https://nodejs.org/
    echo.
    pause
    exit /b 1
)

if not exist "node_modules\electron\package.json" (
    echo.
    echo [!] Electron is not installed yet. Running "npm install" ...
    echo     This downloads ~100 MB on first run, please wait.
    echo.
    call npm install
    if errorlevel 1 (
        echo.
        echo [ERROR] npm install failed. Check your network / proxy settings.
        echo.
        pause
        exit /b 1
    )
)

echo.
echo [i] Starting WeChat Scheduled Sender ...
echo     All user files are kept inside: %~dp0
echo.
call npm start
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo [ERROR] exited with code %RC%
pause
exit /b %RC%