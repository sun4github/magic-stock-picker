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
    REM -----------------------------------------------------------------------
    REM Via a temp file rather than `for /f ... in ('"%PYTHON%" -c "..."')`.
    REM
    REM for /f runs its command through `cmd /c`, which strips the first and last
    REM quote of the whole string. With a QUOTED interpreter path followed by a
    REM quoted -c argument that leaves `C:\...\python.exe" -c "import` as the
    REM thing it tries to execute, and it reports that the command is not
    REM recognised - which reads like a missing Python rather than a quoting bug.
    REM There is an escape that works (^"..^" around the lot); this is plainer,
    REM and this script's whole job is to be reliable in front of someone
    REM setting up for the first time.
    REM -----------------------------------------------------------------------
    set "TOKEN_TMP=%TEMP%\msp_mcp_token_%RANDOM%.tmp"
    "%PYTHON%" -c "import secrets; print(secrets.token_urlsafe(32))" > "!TOKEN_TMP!" 2>nul
    if exist "!TOKEN_TMP!" set /p TOKEN=<"!TOKEN_TMP!"
    del "!TOKEN_TMP!" 2>nul
    if not defined TOKEN (
        echo ERROR: could not generate a token.
        echo Check that "%PYTHON%" runs and that %TEMP% is writable.
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

REM ---------------------------------------------------------------------------
REM Pick the trigger by whether we are elevated.
REM
REM /SC ONSTART registers a BOOT-time task, and Windows will not let a
REM non-administrator create one - schtasks fails with a bare "Access is
REM denied" that reads like a broken script rather than a missing privilege.
REM (This is why tools\setup_weekly_jobs.cmd needs no elevation: /SC WEEKLY is
REM allowed to an ordinary user.)
REM
REM ONLOGON is the unelevated equivalent and is the right default here anyway:
REM the task runs as you, reads your .env and your venv, so on a workstation
REM there is nothing for it to do before you have logged in. Elevate only if you
REM want the server up on a machine that reboots unattended.
REM ---------------------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (set "ELEVATED=0") else (set "ELEVATED=1")

echo.
echo Repo:        %REPO_ROOT%
echo Interpreter: %PYTHON%
echo Endpoint:    http://0.0.0.0:%PORT%/mcp
echo.

if "!ELEVATED!"=="0" goto :startup_folder

REM /RU %USERNAME% runs it as you, so it reads the same .env and has the same
REM network access you do by hand. Deliberately NOT SYSTEM, which has a
REM different profile and no access to a user-scoped venv.
REM
REM cmd /c ... keeps the working directory at the repo root, which is what makes
REM `python -m agent_mcp.server` resolve and what the server's own path
REM detection expects.
echo Registering a boot-start scheduled task ^(elevated^)...
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "cmd /c cd /d \"%REPO_ROOT%\" && \"%PYTHON%\" -m agent_mcp.server --transport http --host 0.0.0.0 --port %PORT% --stateless" ^
    /SC ONSTART ^
    /RU "%USERNAME%" ^
    /RL LIMITED ^
    /F

if errorlevel 1 (
    echo.
    echo FAILED to register the task even though this prompt is elevated.
    echo Falling back to the Startup folder.
    goto :startup_folder
)
goto :configure_task

REM ---------------------------------------------------------------------------
REM :startup_folder - the unelevated path.
REM
REM Windows lets an ordinary user create a WEEKLY task but refuses ONLOGON and
REM ONSTART, both with a bare "Access is denied". That is why
REM tools\setup_weekly_jobs.cmd installs without elevation and this does not.
REM The per-user Startup folder has no such restriction and gives the same
REM practical result on a workstation you log into.
REM ---------------------------------------------------------------------------
:startup_folder
echo Not elevated - installing a Startup-folder launcher instead of a
echo scheduled task. ^(Windows reserves logon/boot-triggered tasks for
echo administrators; re-run this from an Administrator prompt if you need the
echo server up BEFORE anyone logs in.^)
echo.
"%PYTHON%" "%REPO_ROOT%\tools\mcp_service.py" install --port %PORT% --python "%PYTHON%"
if errorlevel 1 (
    echo ERROR: could not install the Startup launcher.
    exit /b 1
)
goto :after_register

:configure_task

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

:after_register
echo.
echo Installed. To start it NOW without waiting:
echo.
if "!ELEVATED!"=="1" (
    echo     schtasks /Run /TN "%TASK_NAME%"
) else (
    echo     "%PYTHON%" tools\mcp_service.py start
)
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
if "!ELEVATED!"=="1" (
    echo   Stop it:      schtasks /End    /TN "%TASK_NAME%"
    echo   Remove it:    schtasks /Delete /TN "%TASK_NAME%" /F
    echo   Status:       schtasks /Query  /TN "%TASK_NAME%" /V /FO LIST
) else (
    echo   Start it:     "%PYTHON%" tools\mcp_service.py start
    echo   Stop it:      "%PYTHON%" tools\mcp_service.py stop
    echo   Status:       "%PYTHON%" tools\mcp_service.py status
    echo   Remove it:    "%PYTHON%" tools\mcp_service.py remove
    echo   Server log:   %REPO_ROOT%\logs\mcp_server.log
)
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
