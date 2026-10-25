@echo off
rem Runs after the single-file installer unpacks itself.
rem Unpacks app.zip next to this script, then hands over to install.ps1.
setlocal

set "WORK=%TEMP%\BlossomSetup"
if exist "%WORK%" rmdir /S /Q "%WORK%"
mkdir "%WORK%"

echo Unpacking...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%~dp0app.zip' -DestinationPath '%WORK%' -Force"
if errorlevel 1 (
    echo Unpack failed. Check free disk space and try again.
    pause
    exit /b 1
)

set "APP="
for /D %%D in ("%WORK%\*") do set "APP=%%D"
if not defined APP (
    echo Unexpected archive layout: no application folder inside.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\install.ps1" -Source "%APP%"
set "CODE=%ERRORLEVEL%"
rmdir /S /Q "%WORK%" >nul 2>&1

if "%CODE%"=="0" (
    echo.
    echo Blossom is installed. The desktop icon is ready.
) else (
    echo.
    echo Install finished with code %CODE%. See the message above.
)
pause
endlocal & exit /b %CODE%
