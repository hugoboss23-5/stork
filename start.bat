@echo off
REM ─────────────────────────────────────────────
REM  Stork Startup — Server + Cloudflare Tunnel
REM ─────────────────────────────────────────────
REM  Starts the MCP server and cloudflared tunnel.
REM  Ctrl+C kills both. Run from C:\Users\bulli\stork
REM
REM  Prerequisites:
REM    1. Python 3.11+ with stork installed (pip install -e .)
REM    2. cloudflared installed and tunnel "stork" created
REM    3. Claude CLI installed (for agent spawning)
REM ─────────────────────────────────────────────

REM CRITICAL: Without this, Claude.ai rejects the MCP connection
set MCP_DISABLE_TRANSPORT_SECURITY=1

REM Stork config (override via env vars before running this script)
if not defined STORK_PORT set STORK_PORT=8000
if not defined STORK_HOST set STORK_HOST=0.0.0.0

echo.
echo  ============================================
echo   STORK Campaign Engine — Starting up
echo  ============================================
echo.

REM Start the MCP server in background
echo [1/2] Starting Stork MCP server on %STORK_HOST%:%STORK_PORT%...
start /b "StorkServer" python -m stork.server --port %STORK_PORT%
timeout /t 2 /nobreak >nul

REM Start cloudflared tunnel in background
echo [2/2] Starting Cloudflare tunnel...
start /b "StorkTunnel" cloudflared tunnel run stork

echo.
echo  ============================================
echo   Stork is running
echo  ============================================
echo.
echo   MCP Server:  http://localhost:%STORK_PORT%/mcp
echo   Health:      http://localhost:%STORK_PORT%/
echo   Tunnel:      Check cloudflared output above for URL
echo.
echo   To connect from Claude.ai:
echo     1. Go to Claude.ai Settings ^> MCP Servers
echo     2. Add server URL: https://YOUR-TUNNEL-URL/mcp
echo.
echo   Press Ctrl+C to stop everything.
echo.

REM Wait for Ctrl+C, then kill both processes
:wait
timeout /t 5 /nobreak >nul
goto wait
