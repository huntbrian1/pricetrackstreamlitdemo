@echo off
setlocal
cd /d "%~dp0"

echo.
echo Installing Hanes local price runner dependencies...
echo.

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
  echo Install Python 3.10+ or Anaconda, then run this installer again.
  pause
  exit /b 1
)

echo Using Python: "%PYTHON_EXE%" %PYTHON_ARGS%
"%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade pip
if errorlevel 1 goto fail

"%PYTHON_EXE%" %PYTHON_ARGS% -m pip install -r requirements_local.txt
if errorlevel 1 goto fail

"%PYTHON_EXE%" %PYTHON_ARGS% -m playwright install chromium
if errorlevel 1 goto fail

echo.
echo Install complete.
pause
exit /b 0

:fail
echo.
echo Install failed. Check the error above.
pause
exit /b 1
