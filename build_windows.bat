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
REM A UNC path (\\server\share) is refused outright. "cd /d" silently stays in C:\Windows
REM there (review #128), and pushd would quietly map a temporary drive letter instead, so
REM the venv and the IDAS.lnk shortcut would point at a drive that is gone after a reboot
REM (review #98). Note for editing: inside a ( ... ) block an unquoted ")" ends the block,
REM so paths are quoted and messages carry no parentheses there (review #25).
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~0,2%"=="\\" (
    echo [ERROR] The project is on a network path: "%SCRIPT_DIR%"
    echo         Copy the project to a local drive such as C:\ or D:\ and run it there.
    pause
    exit /b 1
)
pushd "%SCRIPT_DIR%" || (
    echo [ERROR] Cannot change to the script folder: "%SCRIPT_DIR%"
    echo         Copy the project to a local drive and run the script there.
    pause
    exit /b 1
)
if not exist "idas.spec" (
    echo [ERROR] idas.spec not found in "%CD%". Run this script from the project folder.
    pause
    exit /b 1
)

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
REM Call the venv interpreter directly. activate.bat keeps the absolute path the venv
REM was created with, so a moved folder silently fell back to the system Python (review #126).
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv\Scripts\python.exe is missing. Delete .venv and run again.
    goto :error
)

echo.
echo [2/5] Installing dependencies...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

REM A basemap downloaded as .zip is unpacked here so the spec can bundle the
REM .pmtiles inside (review #59). The app does the same at start-up.
dir /b "assets\*.zip" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Unpacking zipped basemap in assets\...
    "%PY%" -c "import sys; sys.path.insert(0, '.'); from core.basemap import extract_zipped_basemap; print(extract_zipped_basemap() or 'no .pmtiles inside the zip')"
)
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
REM Key files and basemaps that were put into dist\IDAS\_internal\assets\ after the last
REM build (as assets\README.md tells the user to do) must survive the rebuild (review #58).
REM Only files that are NOT in assets\ are kept: a file that is in assets\ is bundled fresh
REM by idas.spec, and restoring an old copy over it shipped a replaced key as the old key.
REM A build_keep_assets\ left behind by a failed build is not deleted here - it is restored
REM after this build like the rest (review #59). The folder is in .gitignore.
set "KEEP=%CD%\build_keep_assets"
if exist "%KEEP%\" echo [INFO] build_keep_assets\ from an earlier run is kept and restored after this build.
if exist "dist\IDAS\_internal\assets\" (
    for %%f in ("dist\IDAS\_internal\assets\*") do (
        if not exist "assets\%%~nxf" (
            if not exist "%KEEP%\" mkdir "%KEEP%"
            echo [INFO] Keeping dist\IDAS\_internal\assets\%%~nxf - restored after the build
            copy /Y "%%f" "%KEEP%\" >nul
        )
    )
)
if exist "build\" rmdir /s /q build
if exist "dist\"  rmdir /s /q dist
if exist "IDAS.lnk" del /q "IDAS.lnk"

echo.
echo [4/5] Building exe (this takes a few minutes)...
"%PY%" -m PyInstaller idas.spec --noconfirm
if errorlevel 1 goto :error

if exist "%KEEP%\" (
    echo [INFO] Restoring kept files into dist\IDAS\_internal\assets\
    if not exist "dist\IDAS\_internal\assets\" mkdir "dist\IDAS\_internal\assets"
    for %%f in ("%KEEP%\*") do (
        if exist "dist\IDAS\_internal\assets\%%~nxf" (
            echo [WARN] %%~nxf is already in the new build - the kept copy stays in build_keep_assets\
        ) else (
            copy /Y "%%f" "dist\IDAS\_internal\assets\" >nul
            del /q "%%f"
        )
    )
    rmdir /q "%KEEP%" 2>nul
)

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
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%CD%\IDAS.lnk');$s.TargetPath='%APPDIR%\IDAS.exe';$s.WorkingDirectory='%APPDIR%';$s.Description='IDAS';$s.IconLocation='%APPDIR%\IDAS.exe,0';$s.Save()"
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
