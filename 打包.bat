@echo off
REM ===================================================================
REM  Build installers (electron-builder).
REM  ASCII ONLY - see README section 6.4 (cmd.exe reads .bat with the
REM  system ANSI codepage; UTF-8 Chinese here desyncs the parser).
REM ===================================================================
cd /d "%~dp0"

where node >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Node.js not found. Please install Node.js 18+.
    echo.
    pause
    exit /b 1
)

if not exist "node_modules\electron-builder\cli.js" (
    echo.
    echo [!] Build tools are not installed. Running "npm install" ...
    echo.
    call npm install
    if errorlevel 1 (
        echo.
        echo [ERROR] npm install failed.
        echo.
        pause
        exit /b 1
    )
)

if not exist "node_modules\electron\dist\electron.exe" (
    echo.
    echo [!] Electron binary is missing. Starting the app once will repair it,
    echo     or run:  npm install-scripts approve electron ^&^& npm install
    echo.
)

echo.
echo [i] Building NSIS installer ...
echo     Cache stays inside: %~dp0cache\electron-builder
echo     Output goes to:     %~dp0dist
echo.
node build.js --win --x64
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo [OK] Done. See the dist folder for the installer.
) else (
    echo [ERROR] Build failed with code %RC%
    echo         If it hangs while downloading nsis/winCodeSign, check your network.
)
echo.
pause
exit /b %RC%