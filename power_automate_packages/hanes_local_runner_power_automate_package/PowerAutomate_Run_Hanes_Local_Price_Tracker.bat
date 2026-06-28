@echo off
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RUN_STAMP=%%i"
set "LOG_FILE=logs\power_automate_local_run_%RUN_STAMP%.log"

call :log "================================================================"
call :log "Hanes local price runner started by Power Automate"
call :log "================================================================"

set "RUNNER_EXTRA_ARGS="
if /i "%HANES_DRY_RUN%"=="1" (
  set "RUNNER_EXTRA_ARGS=--dry-run"
  call :log "DRY RUN enabled by HANES_DRY_RUN=1. No websites will be opened."
)

set "PYTHON_EXE="
set "PYTHON_ARGS="
set "ANACONDA_PYTHON=%USERPROFILE%\anaconda3\python.exe"
if exist "%ANACONDA_PYTHON%" set "PYTHON_EXE=%ANACONDA_PYTHON%"
if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
  )
)

if not defined PYTHON_EXE (
  call :log "ERROR: Python was not found."
  call :log "Install Python 3.10+ or Anaconda, then run this flow again."
  exit /b 1
)

call :log "Using Python: %PYTHON_EXE% %PYTHON_ARGS%"
call :log "Checking Python package dependencies..."
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import pandas, openpyxl, playwright, requests" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "Missing dependencies detected. Installing from requirements_local.txt..."
  "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade pip >> "%LOG_FILE%" 2>&1
  if errorlevel 1 exit /b 1
  "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -r requirements_local.txt >> "%LOG_FILE%" 2>&1
  if errorlevel 1 exit /b 1
) else (
  call :log "Python packages already installed."
)

if /i "%HANES_DRY_RUN%"=="1" (
  call :log "Dry run: skipping Playwright Chromium install check."
) else (
  call :log "Ensuring Playwright Chromium browser is installed..."
  "%PYTHON_EXE%" %PYTHON_ARGS% -m playwright install chromium >> "%LOG_FILE%" 2>&1
  if errorlevel 1 exit /b 1
)

set "INPUT_FILE=input\current_price_master.xlsx"
if not exist "%INPUT_FILE%" set "INPUT_FILE=input\current_price_master.csv"
if not exist "%INPUT_FILE%" set "INPUT_FILE=input\retail_wip_links_import.csv"
call :log "Input table: %INPUT_FILE%"

call :log "Running local browser retailers: Target, Dollar General, TJ Maxx, JCPenney"
"%PYTHON_EXE%" %PYTHON_ARGS% local_price_runner.py ^
  --input "%INPUT_FILE%" ^
  --output-dir "local_outputs" ^
  --retailers "Target,Dollar General,TJ Maxx,JCPenney" ^
  --only-missing ^
  --save-every 25 ^
  --delay-min 5 ^
  --delay-max 9 ^
  --browser-restart-every 40 ^
  --browser-rest-min 120 ^
  --browser-rest-max 240 ^
  --long-rest-every 100 ^
  --long-rest-min 480 ^
  --long-rest-max 720 ^
  --consecutive-miss-restart 8 ^
  --stop-retailer-on-block ^
  --target-delay-min 8 ^
  --target-delay-max 20 ^
  --target-restart-every 25 ^
  --target-rest-min 180 ^
  --target-rest-max 360 ^
  --target-long-rest-every 75 ^
  --target-long-rest-min 600 ^
  --target-long-rest-max 1200 ^
  --target-consecutive-miss-restart 6 ^
  --target-soft-rest-min 300 ^
  --target-soft-rest-max 600 ^
  --target-soft-cooldown-minutes 60 ^
  --target-soft-cooldown-max-minutes 90 ^
  --target-hard-cooldown-hours 24 ^
  --target-repeat-hard-cooldown-hours 48 ^
  --wait-for-target-soft-cooldown ^
  --target-max-rows-per-run 50 ^
  --other-browser-max-rows-per-retailer 150 ^
  --cooldown-state-file "state\retailer_cooldowns.json" %RUNNER_EXTRA_ARGS% >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "ERROR: Local price runner failed. See log: %LOG_FILE%"
  exit /b 1
)

call :log "Local price runner finished successfully."
call :log "Upload the newest *_full_master.csv or *_full_master.xlsx from local_outputs into Streamlit, then Save to GitHub."
goto :eof

:log
echo(%~1
>> "%LOG_FILE%" echo(%~1
goto :eof
