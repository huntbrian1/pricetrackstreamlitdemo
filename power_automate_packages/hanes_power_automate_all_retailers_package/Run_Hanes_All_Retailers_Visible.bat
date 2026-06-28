@echo off
setlocal
cd /d "%~dp0"

set "RUNNER_EXTRA_ARGS="
if /i "%HANES_DRY_RUN%"=="1" (
  set "RUNNER_EXTRA_ARGS=--dry-run"
  echo DRY RUN enabled by HANES_DRY_RUN=1. No websites will be opened and no ScrapingDog credits will be used.
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
  echo ERROR: Could not find Python.
  echo Install Python 3.10+ or Anaconda, then run this again.
  pause
  exit /b 1
)

echo Using Python: "%PYTHON_EXE%" %PYTHON_ARGS%
echo Checking Python package dependencies...
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import pandas, openpyxl, playwright, requests" >nul 2>nul
if errorlevel 1 (
  echo Missing dependencies detected. Installing from requirements_local.txt...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade pip
  if errorlevel 1 goto fail
  "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -r requirements_local.txt
  if errorlevel 1 goto fail
) else (
  echo Python packages already installed.
)

if /i "%HANES_DRY_RUN%"=="1" (
  echo Dry run: skipping Playwright Chromium install check.
) else (
  echo Ensuring Playwright Chromium browser is installed...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m playwright install chromium
  if errorlevel 1 goto fail
)

if not defined SCRAPINGDOG_API_KEY (
  if exist "config\scrapingdog_api_key.txt" (
    set /p SCRAPINGDOG_API_KEY=<"config\scrapingdog_api_key.txt"
  )
)

if not defined SCRAPINGDOG_API_KEY (
  echo ERROR: ScrapingDog API key was not found.
  echo Create config\scrapingdog_api_key.txt with the real key on the first line, or set SCRAPINGDOG_API_KEY.
  pause
  exit /b 1
)

echo %SCRAPINGDOG_API_KEY% | findstr /i "PASTE YOUR_KEY API_KEY EXAMPLE" >nul
if %ERRORLEVEL%==0 (
  echo ERROR: ScrapingDog API key looks like placeholder text.
  echo Edit config\scrapingdog_api_key.txt and paste the real key on the first line.
  pause
  exit /b 1
)

echo.
echo Starting Hanes ALL-retailer price run...
echo This runs Target, Dollar General, TJ Maxx, JCPenney, Walmart, and Amazon.
echo Walmart and Amazon use paid ScrapingDog API credits.
echo.

if not exist "master" mkdir "master"
set "INPUT_FILE=master\hanes_all_retailers_price_master.xlsx"
if not exist "%INPUT_FILE%" set "INPUT_FILE=master\hanes_all_retailers_price_master.csv"
if not exist "%INPUT_FILE%" set "INPUT_FILE=input\current_price_master.xlsx"
if not exist "%INPUT_FILE%" set "INPUT_FILE=input\current_price_master.csv"
if not exist "%INPUT_FILE%" set "INPUT_FILE=input\retail_wip_links_import.csv"
echo Input table: %INPUT_FILE%
echo Persistent master: master\hanes_all_retailers_price_master.xlsx and .csv
echo.

"%PYTHON_EXE%" %PYTHON_ARGS% local_price_runner.py ^
  --input "%INPUT_FILE%" ^
  --output-dir "all_retailer_outputs" ^
  --retailers "Target,Dollar General,TJ Maxx,JCPenney" ^
  --include-api-retailers ^
  --only-missing ^
  --persistent-master-dir "master" ^
  --persistent-master-name "hanes_all_retailers_price_master" ^
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
  --cooldown-state-file "state\retailer_cooldowns.json" %RUNNER_EXTRA_ARGS%

echo.
echo Finished.
echo Daily run files are in all_retailer_outputs.
echo Fixed master workbook is master\hanes_all_retailers_price_master.xlsx.
pause
exit /b 0

:fail
echo.
echo Setup or run failed. Check the error above.
pause
exit /b 1
