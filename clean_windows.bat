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
REM "cd /d" fails silently on a UNC path and the delete loop below would then run in
REM C:\Windows (review #128). Stop unless we are really in the project folder.
pushd "%~dp0" || (
    echo [ERROR] Cannot change to the script folder: %~dp0
    pause
    exit /b 1
)
if not exist "idas.spec" (
    echo [ERROR] idas.spec not found in "%CD%". Refusing to delete anything here.
    pause
    exit /b 1
)

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
echo (Only the map choice is removed from settings.json; key overrides stay.)
choice /c YN /d N /t 15 /m "Reset app settings (auto-no in 15s)"
if not errorlevel 2 (
    REM Same as the app's own Reset: drop only map_mode. Deleting the whole file
    REM also threw away re-issued key overrides (review #127).
    if exist "%LOCALAPPDATA%\IDAS\settings.json" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path $env:LOCALAPPDATA 'IDAS\settings.json'; try { $j = Get-Content -Raw -Encoding UTF8 $p | ConvertFrom-Json } catch { exit 0 }; if ($j -and $j.PSObject.Properties['map_mode']) { $j.PSObject.Properties.Remove('map_mode'); $j | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $p }"
    )
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
