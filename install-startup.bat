@echo off
REM ─────────────────────────────────────────────
REM  Install Stork into Windows Startup
REM ─────────────────────────────────────────────
REM  Run this ONCE. Creates a shortcut to start.bat
REM  in the Windows Startup folder so Stork launches
REM  automatically when Hugo logs in.
REM ─────────────────────────────────────────────

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set STORK_DIR=C:\Users\bulli\stork
set SHORTCUT=%STARTUP_DIR%\Stork.lnk

echo Creating Stork startup shortcut...

REM Use PowerShell to create a proper .lnk shortcut
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%SHORTCUT%'); $sc.TargetPath = '%STORK_DIR%\start.bat'; $sc.WorkingDirectory = '%STORK_DIR%'; $sc.WindowStyle = 7; $sc.Description = 'Stork Campaign Engine'; $sc.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo  SUCCESS: Shortcut created at:
    echo  %SHORTCUT%
    echo.
    echo  Stork will auto-start next time you log in.
    echo  To remove: delete the shortcut from the Startup folder.
    echo.
) else (
    echo.
    echo  FAILED: Could not create shortcut.
    echo  Manual alternative: copy start.bat to:
    echo  %STARTUP_DIR%
    echo.
)

pause
