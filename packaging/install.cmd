@echo off
rem Thin ASCII wrapper: all the real work lives in install.ps1 next to this file.
rem Keeping this file pure ASCII avoids the encoding traps that batch files hit.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Install did not finish cleanly ^(exit %CODE%^). See the message above.
)
rem Double-clicked (no arguments): stay open so the message can be read.
rem Called with arguments (scripts): return right away.
if "%~1"=="" pause
endlocal & exit /b %CODE%
