@echo off
REM ===========================================================================
REM  Register the weekly buy-check sweep with Windows Task Scheduler.
REM
REM  Run this ONCE. It works from any directory - the repo root is derived from
REM  this script's own location.
REM
REM  An ordinary command prompt is normally enough: the task is registered to run
REM  as the current user (/RU), which does not require elevation. If schtasks
REM  refuses, re-run it from an Administrator prompt.
REM
REM      tools\setup_weekly_jobs.cmd
REM
REM  Safe to re-run: /F replaces an existing task of the same name rather than
REM  erroring or creating a duplicate.
REM
REM  WHAT THIS SCHEDULES (and what it does not)
REM  ------------------------------------------
REM  The buy-check sweep ONLY. It uses ADK and Gemini, so it has to run on this
REM  machine, where the agent stack is installed.
REM
REM  The ledger snapshot job (src/cron/run_outcome_snapshot.py) is NOT scheduled
REM  here. It has no LLM in it, and it belongs on the Raspberry Pi alongside
REM  magic-stock-tracker's daily check. It now runs daily (not weekly - a
REM  per-subject snapshot is still spaced out by config, this just shrinks the
REM  catch-up window on a missed run). Its crontab line is:
REM
REM      0 18 * * * /path/to/magic-stock-picker/.venv/bin/python \
REM          /path/to/magic-stock-picker/src/cron/run_outcome_snapshot.py
REM
REM  WHEN IT RUNS
REM  ------------
REM  Saturdays at 09:00. Deliberately after the week's close rather than during
REM  market hours: a trigger evaluated mid-session is compared against a price
REM  that is still moving, and there is nothing to act on until Monday anyway.
REM  Saturday also means a fired trigger arrives with a weekend to read the buy
REM  case before deciding.
REM
REM  If the machine is asleep at 09:00 the task runs when it next wakes
REM  (StartWhenAvailable, applied below).
REM ===========================================================================

setlocal

set "TASK_NAME=MagicStockPicker-BuyCheckSweep"
set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"
set "REPO_ROOT=%CD%"
popd

REM ---------------------------------------------------------------------------
REM Find an interpreter.
REM
REM Prefer the repo's virtualenv, then a `venv` variant, then PATH. A task
REM registered against the wrong interpreter fails at 09:00 on a Saturday with
REM nobody watching, so this is worth getting right up front - which is also why
REM the chosen one is smoke-tested at the bottom before anything is registered.
REM
REM NOTE ON BATCH SEMANTICS: each candidate is assigned DIRECTLY, never via a
REM temporary that is then read back inside the same parenthesised block. A
REM block is parsed as a unit, so a %VAR% inside one expands to its value BEFORE
REM the block runs - which silently blanked this variable in the first version
REM of this script and reported "could not find a python interpreter" on a
REM machine that had python on PATH.
REM ---------------------------------------------------------------------------
set "PYTHON="

if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PYTHON if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PYTHON=%REPO_ROOT%\venv\Scripts\python.exe"

REM `where` can return several hits, so each is offered to :try_python, which
REM keeps the FIRST acceptable one. A subroutine rather than an inline test
REM because chaining `if` with a pipe and `||` inside a for-block parses in ways
REM that differ from how it reads.
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
if not exist "%PYTHON%" (
    echo ERROR: python not found at "%PYTHON%".
    exit /b 1
)

set "SCRIPT=%REPO_ROOT%\src\cron\run_buy_check_sweep.py"
if not exist "%SCRIPT%" (
    echo ERROR: sweep script not found at "%SCRIPT%".
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Smoke-test before registering. An interpreter that exists but cannot import
REM the project's dependencies produces a task that fails every Saturday with
REM its only trace in a log nobody opens. Better to fail here, in front of a
REM human who can fix it.
REM ---------------------------------------------------------------------------
echo Checking the interpreter...
"%PYTHON%" -c "import sys; print('  python', sys.version.split()[0])" 2>nul
if errorlevel 1 (
    echo ERROR: "%PYTHON%" did not run.
    exit /b 1
)
"%PYTHON%" -c "import psycopg2, requests, yaml, dotenv, google.adk" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: "%PYTHON%" cannot import this project's dependencies.
    echo The sweep needs the full agent stack, not just the cron subset:
    echo     "%PYTHON%" -m pip install -r "%REPO_ROOT%\requirements.txt"
    echo Nothing has been scheduled.
    exit /b 1
)
echo   dependencies OK

echo.
echo Repo:        %REPO_ROOT%
echo Interpreter: %PYTHON%
echo Script:      %SCRIPT%
echo Task:        %TASK_NAME%  (Saturdays 09:00)
echo.

REM /RU %USERNAME% runs it as you, so it reads the same .env and has the same
REM network access you do when running it by hand. Deliberately NOT SYSTEM,
REM which has a different profile and no access to a user-scoped venv.
schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "\"%PYTHON%\" \"%SCRIPT%\"" ^
    /SC WEEKLY ^
    /D SAT ^
    /ST 09:00 ^
    /RU "%USERNAME%" ^
    /RL LIMITED ^
    /F

if errorlevel 1 (
    echo.
    echo FAILED to register the task. If the message above mentions access or
    echo privileges, re-run this from an Administrator command prompt.
    exit /b 1
)

REM Run it if the machine was off or asleep at the scheduled time, and cap the
REM runtime so a hung model call cannot leave a task running all week. schtasks
REM cannot set either at create time, so both are applied as a follow-up.
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName '%TASK_NAME%';" ^
  "$t.Settings.StartWhenAvailable = $true;" ^
  "$t.Settings.ExecutionTimeLimit = 'PT1H';" ^
  "Set-ScheduledTask -TaskName '%TASK_NAME%' -Settings $t.Settings | Out-Null" 2>nul

echo.
echo Registered. Useful commands:
echo.
echo   Test the free pre-filter (no LLM calls, no cost, no alerts):
echo     "%PYTHON%" "%SCRIPT%" --dry-run
echo.
echo   Run the real sweep now, without waiting for Saturday:
echo     schtasks /Run /TN "%TASK_NAME%"
echo.
echo   See when it last ran and what it returned:
echo     schtasks /Query /TN "%TASK_NAME%" /V /FO LIST
echo.
echo   Remove it:
echo     schtasks /Delete /TN "%TASK_NAME%" /F
echo.
echo Log file: %REPO_ROOT%\logs\buy_check_sweep.log
echo.
echo NOTE: alerts need the Pushover/Plunk credentials in this repo's .env --
echo the SAME variable names magic-stock-tracker already uses:
echo   PO_APP_KEY, PO_USER_KEY, PO_DEVICE
echo   PLUNK_EMAIL_KEY, PLUNK_FROM_EMAIL, PLUNK_FROM_NAME, ALERT_EMAIL_TO
echo Without them the sweep still runs and still logs; it just cannot notify.
echo.

endlocal
exit /b 0


REM ---------------------------------------------------------------------------
REM :try_python <path>  - accept this interpreter unless one is already chosen,
REM                       or unless it is the Microsoft Store stub.
REM
REM Windows ships an App Execution Alias at
REM   %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe
REM which is NOT an interpreter: it opens the Microsoft Store. `where python`
REM lists it alongside a real install, and on some machines lists it FIRST.
REM Registered as a scheduled task it would fail silently every Saturday, so it
REM is rejected by path.
REM ---------------------------------------------------------------------------
:try_python
if defined PYTHON goto :eof
echo %~1 | find /i "\WindowsApps\" >nul && goto :eof
set "PYTHON=%~1"
goto :eof
