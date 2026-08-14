@echo off
REM ===========================================================================
REM  Register the weekly buy-check sweep with Windows Task Scheduler.
REM
REM  Run this ONCE, from an Administrator command prompt, in the repo root:
REM
REM      tools\setup_weekly_jobs.cmd
REM
REM  It is safe to re-run: /F replaces an existing task of the same name rather
REM  than erroring or creating a duplicate.
REM
REM  WHAT THIS SCHEDULES (and what it does not)
REM  ------------------------------------------
REM  The buy-check sweep ONLY. It uses ADK and Gemini, so it has to run on this
REM  machine, where the agent stack is installed.
REM
REM  The ledger snapshot job (src/cron/run_outcome_snapshot.py) is NOT scheduled
REM  here. It has no LLM in it, and it belongs on the Raspberry Pi alongside
REM  magic-stock-tracker's daily check. Its crontab line is:
REM
REM      0 18 * * 5 /path/to/magic-stock-picker/.venv/bin/python \
REM          /path/to/magic-stock-picker/src/cron/run_outcome_snapshot.py
REM
REM  WHEN IT RUNS
REM  ------------
REM  Saturdays at 09:00. Deliberately after the week's close rather than during
REM  market hours: a trigger evaluated mid-session is being compared against a
REM  price that is still moving, and there is nothing to act on until Monday
REM  anyway. Saturday also means a fired trigger arrives with a weekend to read
REM  the buy case before deciding.
REM
REM  If the machine is asleep at 09:00, the task runs when it next wakes
REM  (/F plus the StartWhenAvailable setting applied below).
REM ===========================================================================

setlocal

set "TASK_NAME=MagicStockPicker-BuyCheckSweep"
set "REPO_ROOT=%~dp0.."
pushd "%REPO_ROOT%"
set "REPO_ROOT=%CD%"
popd

REM Prefer the repo's virtualenv if one exists; fall back to whatever python is
REM on PATH. A task registered against the wrong interpreter fails at 09:00 on a
REM Saturday with nobody watching, so this is worth getting right up front.
set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%REPO_ROOT%\venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYTHON_FALLBACK set "PYTHON_FALLBACK=%%P"
    )
    set "PYTHON=%PYTHON_FALLBACK%"
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

echo.
echo Repo:       %REPO_ROOT%
echo Interpreter:%PYTHON%
echo Script:     %SCRIPT%
echo Task:       %TASK_NAME%  (Saturdays 09:00)
echo.

REM /RU %USERNAME% runs it as you, so it reads the same .env and has the same
REM network access you do when you run it by hand. Deliberately NOT SYSTEM,
REM which would have a different profile and no access to a user-scoped venv.
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
    echo FAILED to register the task. The usual cause is not running this from
    echo an Administrator command prompt.
    exit /b 1
)

REM Run it if the machine was off or asleep at the scheduled time. schtasks
REM cannot set this at create time, so it is applied as a follow-up change.
powershell -NoProfile -Command ^
  "$s = Get-ScheduledTask -TaskName '%TASK_NAME%';" ^
  "$s.Settings.StartWhenAvailable = $true;" ^
  "$s.Settings.ExecutionTimeLimit = 'PT1H';" ^
  "Set-ScheduledTask -TaskName '%TASK_NAME%' -Settings $s.Settings | Out-Null" 2>nul

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
