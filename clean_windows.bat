@echo off
REM ============================================================
REM  GPS Tracer - clean build artifacts
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

echo.
echo This will delete, in "%CD%":
echo.
echo   .venv\          virtual environment
echo   dist\           build output (including GPSTracer.exe)
echo   build\          PyInstaller work folder
echo   GPSTracer.lnk   shortcut
echo   __pycache__\    python caches
echo.
echo Source files, gpstracer.spec and assets\korea.pmtiles are kept.
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
if exist "GPSTracer.lnk" del /q "GPSTracer.lnk"
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

echo Done.
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
