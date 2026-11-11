@echo off
REM ===================================================================
REM  Upgrade helper for v2.0.2 and earlier ONLY.
REM
REM  Problem this solves:
REM    Old versions stored user files (data/ logs/ shots/ cache/ tmp/
REM    runtime/) INSIDE the install directory. The NSIS installer runs
REM    `RMDir /r $INSTDIR` on upgrade, which deletes them. New versions
REM    keep them in a sibling "WeChatSender-Data" folder instead.
REM
REM  New versions migrate automatically on first launch -- BUT the old
REM  data must survive the upgrade first. Run THIS SCRIPT before running
REM  the new installer if you are still on v2.0.2 or earlier.
REM
REM  Actions: MOVE the six user folders out of the install directory
REM  into the sibling data root. Nothing is deleted. Re-runnable.
REM  ASCII ONLY: cmd.exe reads .bat using the system ANSI codepage;
REM  UTF-8 Chinese here would desync the parser.
REM
REM  Usage:  migrate-user-data.bat "<install dir>"
REM    e.g.  migrate-user-data.bat "D:\soft\WeChatSender"
REM ===================================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "INST=%~1"
if "%INST%"=="" (
  echo.
  echo Usage: %~nx0 "install directory"
  echo   e.g. %~nx0 "D:\soft\WeChatSender"
  echo.
  echo The install directory is the folder containing WeChatSender.exe.
  echo.
  pause
  exit /b 1
)

REM Strip trailing backslash so path concatenation stays correct.
if "%INST:~-1%"=="\" set "INST=%INST:~0,-1%"

if not exist "%INST%\WeChatSender.exe" (
  echo.
  echo [ERROR] WeChatSender.exe not found in:
  echo   %INST%
  echo.
  echo Pass the folder that contains WeChatSender.exe.
  echo.
  pause
  exit /b 1
)

for %%I in ("%INST%") do set "PARENT=%%~dpI"
if "%PARENT:~-1%"=="\" set "PARENT=%PARENT:~0,-1%"
set "DEST=%PARENT%\WeChatSender-Data"

echo.
echo   Install dir : %INST%
echo   Data root   : %DEST%
echo.
echo   Please CLOSE WeChatSender before continuing, otherwise files
echo   may be locked and the move will fail.
echo.
pause

if not exist "%DEST%" mkdir "%DEST%" 2>nul
if not exist "%DEST%" (
  echo [ERROR] Cannot create data root: %DEST%
  pause
  exit /b 1
)

set /a MOVED=0
set /a SKIPPED=0
set /a FAILED=0

for %%D in (data logs shots cache tmp runtime) do (
  call :MOVE_ONE "%%D"
)

echo.
echo ---------------------------------------------------------------
echo   moved   : %MOVED%
echo   skipped : %SKIPPED%  (source missing or target already has data)
echo   failed  : %FAILED%
echo ---------------------------------------------------------------
echo.
if %FAILED% GTR 0 (
  echo [WARN] Some folders could not be moved. Close WeChatSender and
  echo        re-run this script. Nothing was deleted.
) else (
  echo   Done. You can now run the new installer.
  echo   Old folders that were moved no longer exist in the install dir;
  echo   that is expected and safe.
)
echo.
pause
exit /b 0

:MOVE_ONE
set "NAME=%~1"
set "SRC=%INST%\%NAME%"
set "DST=%DEST%\%NAME%"

if not exist "%SRC%" (
  echo   [skip] %NAME% : not present in install dir
  set /a SKIPPED+=1
  exit /b 0
)

if exist "%DST%" (
  dir /b "%DST%" >nul 2>&1
  if not errorlevel 1 (
    echo   [skip] %NAME% : target already has data, left untouched
    set /a SKIPPED+=1
    exit /b 0
  )
  rmdir "%DST%" 2>nul
)

move /Y "%SRC%" "%DST%" >nul 2>&1
if exist "%DST%" (
  echo   [ ok ] %NAME% -^> %DST%
  set /a MOVED+=1
  exit /b 0
)

echo   [FAIL] %NAME% : could not move ^(file locked?^)
set /a FAILED+=1
exit /b 0
