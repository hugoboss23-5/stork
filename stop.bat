@echo off
REM ─────────────────────────────────────────────
REM  Stop Stork — Kill server + tunnel
REM ─────────────────────────────────────────────

echo Stopping Stork...

taskkill /f /fi "WINDOWTITLE eq StorkServer" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq StorkTunnel" >nul 2>&1
taskkill /f /im ngrok.exe >nul 2>&1

echo Done. Stork processes killed.
