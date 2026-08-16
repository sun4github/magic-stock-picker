@echo off
REM ===========================================================================
REM  Register the MCP server as an always-on Windows service (Task Scheduler),
REM  so OpenClaw or any other remote MCP client can reach it.
REM
REM  Run this ONCE. It works from any directory - the repo root is derived from
REM  this script's own location.
REM
REM      tools\setup_mcp_server.cmd
REM
REM  Safe to re-run: /F replaces an existing task of the same name rather than
REM  erroring or creating a duplicate. Re-running does NOT regenerate the token
REM  if one already exists in .env, so it will not silently break a client that
REM  is already configured.
REM
REM  WHY A SERVICE AT ALL
REM  --------------------
REM  A client on ANOTHER machine cannot start a process on this one. Over stdio
REM  the client spawns the server itself and nothing needs registering - but
REM  stdio only works locally. For OpenClaw across the network the server has to
REM  be listening before the client connects, and stay listening. Hence this.
REM
REM  WHAT IT REGISTERS
REM  -----------------
REM  `python -m agent_mcp.server --transport http --host 0.0.0.0 --stateless`
REM  at boot, as the current user. Port 8765 by default; pass a different one as
REM  the first argument:  tools\setup_mcp_server.cmd 9000
REM
REM  THE TOKEN
REM  ---------
REM  Clients authenticate with a shared secret. It is not issued by anything -
REM  it is a random string you invent, kept in this repo's .env alongside the
REM  other credentials, and repeated in the client's config. If .env has no
REM  MSP_MCP_TOKEN this script generates one and appends it, then prints the
REM  matching OpenClaw configuration at the end.
REM
REM  NOTE ON SCOPE: the task launches the SERVER only. The runs the server
REM  starts are ordinary pipeline invocations and still need this machine's
REM  full agent stack and .env - same as running them by hand.
REM ===========================================================================

setlocal EnableDelayedExpansion

set "TASK_NAME=MagicStockPicker-MCP"
set "PORT=%~1"
if not defined PORT set "PORT=8765"

set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"
set "REPO_ROOT=%CD%"
popd

REM ---------------------------------------------------------------------------
REM Find an interpreter. Same approach, and same traps, as
REM tools\setup_weekly_jobs.cmd - see the long note there. Each candidate is
REM assigned DIRECTLY rather than through a temporary read back inside the same
REM parenthesised block, because a block is parsed as a unit and %VAR% inside
REM one expands before the block runs.
REM ---------------------------------------------------------------------------
set "PYTHON="

if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PYTHON if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PYTHON=%REPO_ROOT%\venv\Scripts\python.exe"

if not defined PYTHON (
    for /f "delims=" %%P in ('where python 2^>nul') do call :try_python "%%P"
)
if not defined PYTHON (
    for /f "delims=" %%P in ('where py 2^>nul') do call :try_python "%%P"
)

if not defined PYTHON (
    echo ERROR: could not find a python interpreter.
    echo Create a virtualenv at "%REPO_ROOT%\.venv" or put python on PATH.
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Smoke-test before registering. A server that cannot import `mcp` produces a
REM task that dies at every boot with its only trace in a log nobody opens.
REM ---------------------------------------------------------------------------
echo Checking the interpreter...
"%PYTHON%" -c "import sys; print('  python', sys.version.split()[0])" 2>nul
if errorlevel 1 (
    echo ERROR: "%PYTHON%" did not run.
    exit /b 1
)
"%PYTHON%" -c "import mcp, yaml, dotenv, uvicorn" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: "%PYTHON%" cannot import the MCP layer's dependencies:
    echo     "%PYTHON%" -m pip install -r "%REPO_ROOT%\agent_mcp\requirements.txt"
    echo Nothing has been scheduled.
    exit /b 1
)
echo   dependencies OK

REM ---------------------------------------------------------------------------
REM Token. Read .env first; generate and append only if absent, so re-running
REM this script never invalidates a client that is already talking to us.
REM ---------------------------------------------------------------------------
set "TOKEN="
if exist "%REPO_ROOT%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%REPO_ROOT%\.env") do (
        if /i "%%A"=="MSP_MCP_TOKEN" set "TOKEN=%%B"
    )
)

if not defined TOKEN (
    echo.
    echo No MSP_MCP_TOKEN in .env - generating one.
    for /f "delims=" %%T in ('"%PYTHON%" -c "import secrets; print(secrets.token_urlsafe(32))"') do set "TOKEN=%%T"
    if not defined TOKEN (
        echo ERROR: could not generate a token.
        exit /b 1
    )
    >>"%REPO_ROOT%\.env" echo.
    >>"%REPO_ROOT%\.env" echo # Bearer token for the MCP layer ^(agent_mcp/^), added by tools\setup_mcp_server.cmd
    >>"%REPO_ROOT%\.env" echo MSP_MCP_TOKEN=!TOKEN!
    echo   written to .env
) else (
    echo.
    echo Using the MSP_MCP_TOKEN already in .env.
)

echo.
echo Repo:        %REPO_ROOT%
echo Interpreter: %PYTHON%
echo Task:        %TASK_NAME%  ^(at every boot^)
echo Endpoint:    http://0.0.0.0:%PORT%/mcp
echo.

REM /RU %USERNAME% runs it as you, so it reads the same .env and has the same
REM network access you do by hand. Deliberately NOT SYSTEM, which has a
REM different profile and no access to a user-scoped venv.
REM
REM cmd /c ... keeps the working directory at the repo root, which is what makes
REM `python -m agent_mcp.server` resolve and what the server's own path
REM detection expects.
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "cmd /c cd /d \"%REPO_ROOT%\" && \"%PYTHON%\" -m agent_mcp.server --transport http --host 0.0.0.0 --port %PORT% --stateless" ^
    /SC ONSTART ^
    /RU "%USERNAME%" ^
    /RL LIMITED ^
    /F

if errorlevel 1 (
    echo.
    echo FAILED to register the task. If the message above mentions access or
    echo privileges, re-run this from an Administrator command prompt.
    exit /b 1
)

REM A server is meant to stay up, so unlike the weekly sweep this gets NO
REM execution time limit - the default would kill it after three days - and it
REM is told to restart if it ever stops.
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName '%TASK_NAME%';" ^
  "$t.Settings.ExecutionTimeLimit = 'PT0S';" ^
  "$t.Settings.StartWhenAvailable = $true;" ^
  "$t.Settings.RestartCount = 3;" ^
  "$t.Settings.RestartInterval = 'PT1M';" ^
  "$t.Settings.DisallowStartIfOnBatteries = $false;" ^
  "$t.Settings.StopIfGoingOnBatteries = $false;" ^
  "Set-ScheduledTask -TaskName '%TASK_NAME%' -Settings $t.Settings | Out-Null" 2>nul

echo.
echo Registered. It will start at every boot. To start it NOW:
echo.
echo     schtasks /Run /TN "%TASK_NAME%"
echo.
echo Then check it is listening ^(no token needed for this^):
echo.
echo     curl http://localhost:%PORT%/health
echo.

REM ---------------------------------------------------------------------------
REM Print the client configuration, token filled in. The LAN address is offered
REM rather than guessed at - a machine can have several, and the right one is
REM whichever the client can actually route to.
REM ---------------------------------------------------------------------------
echo ---------------------------------------------------------------------------
echo  OpenClaw configuration  ^(~/.openclaw/openclaw.json^)
echo ---------------------------------------------------------------------------
echo.
echo   Replace THIS-HOST with whichever of this machine's addresses the client
echo   can reach:
echo.
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /c:"IPv4 Address"') do echo       %%I
echo.
echo {
echo   "mcp": {
echo     "servers": {
echo       "magic-stock-picker": {
echo         "url": "http://THIS-HOST:%PORT%/mcp",
echo         "transport": "streamable-http",
echo         "headers": { "Authorization": "Bearer !TOKEN!" },
echo         "requestTimeoutMs": 30000,
echo         "connectionTimeoutMs": 5000,
echo         "toolFilter": {
echo           "include": [],
echo           "exclude": ["start_pipeline", "start_analysis_from_csv"]
echo         }
echo       }
echo     }
echo   }
echo }
echo.
echo   toolFilter hides the two operations that spend $11-13 a call, so the
echo   remote agent cannot start one even by confirming. Everything cheap, every
echo   check and everything read-only stays available. Drop the exclude list if
echo   you want the agent to be able to run a full screen-and-analyse cycle.
echo.
echo ---------------------------------------------------------------------------
echo  Other useful commands
echo ---------------------------------------------------------------------------
echo.
echo   Stop it:      schtasks /End    /TN "%TASK_NAME%"
echo   Remove it:    schtasks /Delete /TN "%TASK_NAME%" /F
echo   Status:       schtasks /Query  /TN "%TASK_NAME%" /V /FO LIST
echo.
echo   If a client gets 401, the token in its config does not match the one in
echo   .env. If it cannot connect at all, check the firewall allows inbound TCP
echo   on port %PORT%:
echo.
echo     netsh advfirewall firewall add rule name="MagicStockPicker MCP" ^
echo       dir=in action=allow protocol=TCP localport=%PORT%
echo.

endlocal
exit /b 0


REM ---------------------------------------------------------------------------
REM :try_python <path>  - accept this interpreter unless one is already chosen,
REM                       or unless it is the Microsoft Store stub at
REM                       %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe, which
REM                       opens the Store rather than running Python and which
REM                       `where python` sometimes lists FIRST.
REM ---------------------------------------------------------------------------
:try_python
if defined PYTHON goto :eof
echo %~1 | find /i "\WindowsApps\" >nul && goto :eof
set "PYTHON=%~1"
goto :eof
