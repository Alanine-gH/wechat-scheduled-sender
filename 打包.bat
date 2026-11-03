@echo off
REM ===================================================================
REM  Build installers (electron-builder).
REM  ASCII ONLY - see README section 6.4 (cmd.exe reads .bat with the
REM  system ANSI codepage; UTF-8 Chinese here desyncs the parser).
REM
REM  Usage:
REM    this script          -> asks for a version (Enter = keep package.json)
REM    this script 1.2.0    -> build as version 1.2.0
REM  The version is applied to package.json only for the duration of the
REM  build and is restored afterwards (see build.js).
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

set "VER=%~1"
if "%VER%"=="" set /p "VER=Version to build (Enter = keep current): "

echo.
if "%VER%"=="" (
    echo [i] Building NSIS installer with the current version ...
) else (
    echo [i] Building NSIS installer as version %VER% ...
)
REM Path must be QUOTED here: %~dp0 can contain "&" (this project lives
REM under a directory whose name contains "&"), and an unquoted & makes
REM cmd treat the rest of the line as a separate command ->
REM "The system cannot find the path specified" (while still building fine).
echo     Cache stays inside: "%~dp0cache\electron-builder"
echo     Output goes to:     "%~dp0dist"
echo.

if "%VER%"=="" goto :build_plain
node build.js --version %VER% --win --x64
goto :after_build

:build_plain
node build.js --win --x64

:after_build
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