@echo off
setlocal EnableExtensions

cd /d "%~dp0"

call :refresh_path

call :ensure_python
if errorlevel 1 goto :failed

call :ensure_command adb Google.PlatformTools "Android SDK Platform-Tools / adb"
if errorlevel 1 goto :failed

call :ensure_command scrcpy Genymobile.scrcpy "scrcpy"
if errorlevel 1 goto :failed

echo.
echo Starting Scrcpy Device Manager...
%PYTHON_CMD% "%~dp0main.py"
goto :done

:ensure_python
where.exe py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    echo [OK] Python found via py launcher.
    exit /b 0
)

where.exe python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    echo [OK] Python found.
    exit /b 0
)

echo [ERROR] Python 3.12+ was not found.
echo Install Python, then run this launcher again.
exit /b 1

:ensure_command
set "COMMAND_NAME=%~1"
set "WINGET_ID=%~2"
set "DISPLAY_NAME=%~3"

where.exe "%COMMAND_NAME%" >nul 2>nul
if not errorlevel 1 (
    echo [OK] %DISPLAY_NAME% found.
    exit /b 0
)

if "%WINGET_ID%"=="" (
    echo [ERROR] %DISPLAY_NAME% was not found on PATH.
    echo Install it, then run this launcher again.
    exit /b 1
)

call :ensure_winget
if errorlevel 1 (
    echo [ERROR] %DISPLAY_NAME% was not found, and winget is not available.
    echo Install %DISPLAY_NAME%, then run this launcher again.
    exit /b 1
)

echo [SETUP] %DISPLAY_NAME% was not found on PATH.
echo Installing %DISPLAY_NAME% with winget package %WINGET_ID%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "winget install --exact --id '%WINGET_ID%' --accept-package-agreements --accept-source-agreements"
if errorlevel 1 (
    echo [ERROR] winget could not install %DISPLAY_NAME%.
    exit /b 1
)

call :refresh_path
where.exe "%COMMAND_NAME%" >nul 2>nul
if not errorlevel 1 (
    echo [OK] %DISPLAY_NAME% installed and found.
    exit /b 0
)

echo [ERROR] %DISPLAY_NAME% installed, but it is still not visible on PATH.
echo Close this window and run the launcher again. If it still fails, restart Windows.
exit /b 1

:ensure_winget
powershell -NoProfile -Command "if (Get-Command winget -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

:refresh_path
for /f "tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MACHINE_PATH=%%B"
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
set "PATH=%PATH%;%MACHINE_PATH%;%USER_PATH%;%LOCALAPPDATA%\Microsoft\WindowsApps"
exit /b 0

:failed
echo.
echo Launch failed. See the message above.
goto :done

:done
echo.
pause
