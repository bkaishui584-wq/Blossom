@echo off
rem Thin ASCII wrapper around uninstall.ps1. Pass --purge to also delete user data.
setlocal
set "EXTRA="
if /I "%~1"=="--purge" set "EXTRA=-Purge"
if /I "%~1"=="/purge" set "EXTRA=-Purge"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %EXTRA%
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Uninstall did not finish cleanly ^(exit %CODE%^). See the message above.
)
if "%~1"=="" pause
endlocal & exit /b %CODE%
