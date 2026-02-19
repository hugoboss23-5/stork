@echo off
REM ─────────────────────────────────────────────
REM  Stork Startup — Server + ngrok Tunnel
REM ─────────────────────────────────────────────
REM  Launches both processes minimized and exits.
REM  Designed to run at Windows login via Startup folder.
REM
REM  Prerequisites:
REM    1. Python 3.11+ with stork installed (pip install -e .)
REM    2. ngrok installed and authenticated (ngrok config add-authtoken)
REM    3. Claude CLI installed (for agent spawning)
REM ─────────────────────────────────────────────

REM CRITICAL: Without this, Claude.ai rejects the MCP connection
set MCP_DISABLE_TRANSPORT_SECURITY=1

REM Config
set STORK_PORT=8000
set STORK_HOST=0.0.0.0
set NGROK_DOMAIN=cartographical-bari-unfeasible.ngrok-free.dev

REM Kill any leftover processes from a previous run
taskkill /f /fi "WINDOWTITLE eq StorkServer" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq StorkTunnel" >nul 2>&1

REM Kill any old cloudflared tunnel processes (cleanup from previous setup)
taskkill /f /im cloudflared.exe >nul 2>&1

REM Start the MCP server in a minimized window
start /min "StorkServer" cmd /c "cd /d C:\Users\bulli\stork && set MCP_DISABLE_TRANSPORT_SECURITY=1 && python -m stork.server --port %STORK_PORT%"

REM Wait for server to be ready
ping -n 4 127.0.0.1 >nul

REM Start ngrok tunnel in a minimized window
start /min "StorkTunnel" cmd /c "ngrok http --url=%NGROK_DOMAIN% %STORK_PORT%"

echo.
echo  ============================================
echo   Stork is running
echo  ============================================
echo.
echo   MCP Server:  http://localhost:%STORK_PORT%/mcp
echo   Health:      http://localhost:%STORK_PORT%/
echo   Tunnel:      https://%NGROK_DOMAIN%/mcp
echo.
echo   Claude.ai MCP URL: https://%NGROK_DOMAIN%/mcp
echo.
echo   To stop: close the StorkServer and StorkTunnel windows,
echo            or run: taskkill /f /fi "WINDOWTITLE eq StorkServer"
echo                    taskkill /f /fi "WINDOWTITLE eq StorkTunnel"
echo.
