@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%~dp0requirements.txt"
set "APP_FILE=%~dp0stock_app.py"

if not exist "%REQUIREMENTS%" (
  echo requirements.txt not found: %REQUIREMENTS%
  pause
  exit /b 1
)

if /I "%~1"=="--recreate" (
  if exist "%VENV_DIR%" (
    echo Removing existing venv: %VENV_DIR%
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" (
      echo Failed to remove existing venv. Close processes using it and retry.
      pause
      exit /b 1
    )
  )
)

if not exist "%PYTHON_EXE%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python command not found.
    echo Install Python 3.14+ or add Python to PATH, then rerun this script.
    pause
    exit /b 1
  )

  echo Creating venv: %VENV_DIR%
  python -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Failed to create venv.
    pause
    exit /b 1
  )
) else (
  echo Reusing existing venv: %VENV_DIR%
)

echo Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

echo Installing dependencies from requirements.txt...
"%PYTHON_EXE%" -m pip install -r "%REQUIREMENTS%"
if errorlevel 1 (
  echo Failed to install dependencies.
  pause
  exit /b 1
)

if exist "%APP_FILE%" (
  echo Checking Python syntax...
  "%PYTHON_EXE%" -m py_compile "%APP_FILE%"
  if errorlevel 1 (
    echo stock_app.py syntax check failed.
    pause
    exit /b 1
  )
)

echo.
echo Venv is ready.
echo Start the app with: start_stock_app.cmd
pause
