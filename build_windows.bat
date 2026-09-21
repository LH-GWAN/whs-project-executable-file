@echo off
REM ============================================================
REM  IDAS - Windows exe build script
REM
REM  NOTE: This file is intentionally ASCII-only. Korean text in a
REM  .bat breaks under CP949 consoles and mangled bytes can be parsed
REM  as commands. See BUILD.md for the Korean guide.
REM
REM  Requires: Python 3.11 or 3.12 (64-bit), "Add python.exe to PATH"
REM            https://www.python.org/downloads/windows/
REM ============================================================

setlocal
cd /d "%~dp0"

REM Building while the app is running fails: PyInstaller cannot replace
REM the files under dist\ that the running exe has open.
tasklist /FI "IMAGENAME eq IDAS.exe" 2>nul | find /I "IDAS.exe" >nul
if not errorlevel 1 (
    echo.
    echo [ERROR] IDAS.exe is running. Close the app first, then run
    echo         this script again.
    echo.
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python not found on PATH.
    echo         Install Python 3.12 64-bit and check
    echo         "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo.
echo [1/5] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :error
)
call .venv\Scripts\activate.bat

echo.
echo [2/5] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

dir /b "assets\*.pmtiles" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARN] No .pmtiles basemap found in assets\.
    echo        The app still works, but the map will show the GPS track
    echo        on a blank background instead of real roads.
    echo        See assets\README.md to create one.
    echo.
    choice /c YN /t 10 /d Y /m "Continue without the basemap (auto-yes in 10s)"
    if errorlevel 2 exit /b 1
)

if exist "assets\online_keys.json" (
    echo [INFO] Online map key file found: assets\online_keys.json
    echo        It will be bundled into dist\IDAS\_internal\assets\.
) else (
    echo [INFO] No assets\online_keys.json - online map stays off until a key
    echo        file is put in dist\IDAS\_internal\assets\ after the build.
)

echo.
echo [3/5] Cleaning previous build...
if exist "build\" rmdir /s /q build
if exist "dist\"  rmdir /s /q dist
if exist "IDAS.lnk" del /q "IDAS.lnk"

echo.
echo [4/5] Building exe (this takes a few minutes)...
pyinstaller idas.spec --noconfirm
if errorlevel 1 goto :error

if not exist "dist\IDAS\IDAS.exe" (
    echo.
    echo [ERROR] Build finished but IDAS.exe was not found.
    echo         Check the PyInstaller output above.
    pause
    exit /b 1
)

echo.
echo [5/5] Creating shortcut in this folder...
REM The exe cannot be moved out of dist\IDAS on its own: a one-dir
REM build needs the _internal folder sitting right next to it. So we put
REM a shortcut here instead - double-click it and the app starts.
set "APPDIR=%CD%\dist\IDAS"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%CD%\IDAS.lnk');$s.TargetPath='%APPDIR%\IDAS.exe';$s.WorkingDirectory='%APPDIR%';$s.Description='IDAS';$s.Save()"
if not exist "IDAS.lnk" (
    echo [WARN] Could not create the shortcut.
    echo        Run the app directly: dist\IDAS\IDAS.exe
)

echo.
echo ============================================================
echo  BUILD OK
echo.
echo  To run : double-click IDAS.lnk in this folder.
echo.
echo  Real exe : dist\IDAS\IDAS.exe
echo.
echo  To ship: copy the WHOLE dist\IDAS folder, not just the
echo           exe. The _internal folder next to the exe holds Qt,
echo           the analysis engine and the offline basemap.
echo           The shortcut only works on this machine.
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo *** BUILD FAILED - see the error message above ***
pause
exit /b 1
