@echo off
REM ============================================================
REM  IDAS - clean build artifacts
REM
REM  Removes the virtual environment, build output and shortcut so
REM  you can rebuild from a clean state. Source files are NOT touched.
REM
REM  IMPORTANT: assets\korea.pmtiles (the offline basemap, ~371 MB) is
REM  NOT deleted. It is excluded from git, so once removed it can only
REM  come back by copying it in again by hand.
REM ============================================================

setlocal
cd /d "%~dp0"

tasklist /FI "IMAGENAME eq IDAS.exe" 2>nul | find /I "IDAS.exe" >nul
if not errorlevel 1 (
    echo.
    echo [ERROR] IDAS.exe is running. Close the app first.
    echo.
    pause
    exit /b 1
)

echo.
echo This will delete, in "%CD%":
echo.
echo   .venv\          virtual environment
echo   dist\           build output (including IDAS.exe)
echo   build\          PyInstaller work folder
echo   IDAS.lnk   shortcut
echo   __pycache__\    python caches
echo.
echo Source files, idas.spec and assets\korea.pmtiles are kept.
echo.
choice /c YN /m "Delete these"
if errorlevel 2 (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
if exist ".venv\"        rmdir /s /q ".venv"
if exist "dist\"         rmdir /s /q "dist"
if exist "build\"        rmdir /s /q "build"
if exist "IDAS.lnk" del /q "IDAS.lnk"
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

echo Done.
echo.

REM The online/offline map choice and the "do not show again" boxes live in the
REM user's data area, not in this folder, so they survive clean + rebuild:
REM   %LOCALAPPDATA%\IDAS\settings.json   (map mode, key overrides)
REM   HKCU\Software\IDAS                    (notice preferences, QSettings)
REM Case history and evidence folders (history.db, cases\) are NEVER touched here.
echo The map online/offline choice and notice preferences are stored in
echo   %LOCALAPPDATA%\IDAS\settings.json  and  HKCU\Software\IDAS
echo They are NOT deleted by this script. Reset them so the app asks again
echo at the next start? Case history and evidence folders are kept either way.
choice /c YN /d N /t 15 /m "Reset app settings (auto-no in 15s)"
if not errorlevel 2 (
    if exist "%LOCALAPPDATA%\IDAS\settings.json" del /q "%LOCALAPPDATA%\IDAS\settings.json"
    reg delete "HKCU\Software\IDAS" /f >nul 2>&1
    echo App settings reset. The app will ask online/offline at the next start.
) else (
    echo App settings kept. Use Settings ^> Reset map settings inside the app instead.
)
echo.
if exist "assets\korea.pmtiles" (
    echo Basemap kept: assets\korea.pmtiles
) else (
    echo [NOTE] assets\korea.pmtiles is missing. The map will show the
    echo        GPS track on a blank background until you copy it in.
)
echo.
echo Next: run build_windows.bat
echo.
pause
exit /b 0
