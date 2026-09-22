@echo off
REM LAUNCHER.bat - Double-click this file to start Aria on Windows.
REM It opens http://localhost:8091.
REM
REM On a brand new PC this installs everything Aria needs by itself:
REM the package manager, Python, the libraries, and the sign-in browser.
REM Nothing is installed system-wide. Everything lives inside this folder,
REM so deleting the folder removes it all.
REM
REM HKUST AI Literacy Course - Path A: Agentic AI Exemplar

setlocal enabledelayedexpansion
title Aria - Library Booking Assistant

set "ROOT=%~dp0"
set "UV_DIR=%ROOT%.uv"
set "UV_BIN=%UV_DIR%\uv.exe"
set "STAMP=%UV_DIR%\.chromium-installed"

REM Everything Aria downloads lives inside this folder. Python, the voice
REM model, the sign-in browser, uv itself -- deleting the folder removes it all.
set "UV_PYTHON_INSTALL_DIR=%ROOT%.uv\python"
REM uv also keeps a package cache and a tool dir. Left alone those go to
REM %LOCALAPPDATA%, survive deleting this folder, and reach hundreds of
REM megabytes, which contradicts what the README promises.
set "UV_CACHE_DIR=%ROOT%.uv\cache"
set "UV_TOOL_DIR=%ROOT%.uv\tools"
set "UV_TOOL_BIN_DIR=%ROOT%.uv\tools\bin"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%.uv\playwright-browsers"

REM This script re-invokes itself with an "openWhenReady" argument to poll
REM the server in a second window before opening the browser (see below).
if "%~1"=="openWhenReady" goto :openWhenReady

cd /d "%ROOT%"

REM -- 1. Find uv, or install it into this folder ------------------------
REM uv is a single .exe that needs no Python of its own. It can download
REM Python, so it is the only thing that has to exist before anything else.
set "UV="
if exist "%UV_BIN%" (
    set "UV=%UV_BIN%"
) else (
    where uv >nul 2>nul
    if not errorlevel 1 set "UV=uv"
)

if not defined UV (
    echo.
    echo   First run: setting up Aria. This takes a few minutes and needs
    echo   an internet connection. You only have to do this once.
    echo.
    echo   Step 1 of 3: downloading the setup tool...
    powershell -ExecutionPolicy ByPass -c "$env:UV_UNMANAGED_INSTALL='%UV_DIR%'; irm https://astral.sh/uv/install.ps1 | iex" >nul 2>nul
    if not exist "%UV_BIN%" goto :no_uv
    set "UV=%UV_BIN%"
)

cd /d "%ROOT%app"

REM -- 2. Python and the libraries ---------------------------------------
REM uv reads pyproject.toml, downloads Python 3.11+ if this PC has none,
REM creates .venv, and installs the pinned versions from uv.lock.
echo   Step 2 of 3: installing Python and the libraries...
"!UV!" sync
if errorlevel 1 goto :sync_failed

REM -- 3. The sign-in browser --------------------------------------------
REM Only Chromium. booking_agent.py always launches Chromium for the HKUST
REM sign-in, so Firefox and WebKit would be a wasted download.
if not exist "%STAMP%" (
    echo   Step 3 of 3: installing the sign-in browser ^(about 150 MB^)...
    "!UV!" run playwright install chromium
    if not errorlevel 1 (
        echo installed> "%STAMP%"
    ) else (
        echo   The sign-in browser did not install. Checking room availability
        echo   will still work. Booking needs sign-in, so it will not.
    )
)

echo   Starting Aria...
REM Open the browser once the server actually answers, not on a guess. A
REM fixed delay races server startup and can hit a dead port.
REM The doubled quotes are required, not a typo. `cmd /c "path" arg` strips
REM the outer quotes when the path contains spaces, which breaks for a folder
REM like "aria-project 2". Wrapping the whole command in one more pair of
REM quotes is the form that survives it.
start "" /min cmd /c ""%~f0" openWhenReady"
"!UV!" run python server.py
pause
exit /b 0

:openWhenReady
REM Wait for the server, then give the Kokoro voice model a grace period to
REM finish downloading (337 MB on first run). If it is not ready in time we
REM open anyway: the page falls back to the browser's built-in voice and
REM switches to Kokoro on its own once it lands.
where curl >nul 2>nul
if errorlevel 1 (
    timeout /t 8 /nobreak >nul
    start http://localhost:8091
    exit /b 0
)
set /a GRACE=0
for /l %%i in (1,1,600) do (
    curl -sf -m 2 -o nul http://localhost:8091/api/session 2>nul
    if not errorlevel 1 (
        curl -sf -m 2 http://localhost:8091/api/tts/status 2>nul | findstr /c:"\"ready\": true" >nul
        if not errorlevel 1 goto :openNow
        set /a GRACE+=1
        if !GRACE! geq 60 goto :openNow
    )
    timeout /t 1 /nobreak >nul
)
:openNow
timeout /t 2 /nobreak >nul
start http://localhost:8091
exit /b 0

:no_uv
echo.
echo   Could not download the setup tool.
echo.
echo   Check your internet connection and try again. If you are on a
echo   university or company network that blocks downloads, try a
echo   different network.
echo.
pause
exit /b 1

:sync_failed
echo.
echo   Could not install Python or the libraries.
echo.
echo   Check your internet connection and try again.
echo.
pause
exit /b 1
