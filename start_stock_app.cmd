@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "PORT=8765"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "APP_FILE=%~dp0stock_app.py"
set "APP_URL=http://127.0.0.1:%PORT%/"

if not exist "%PYTHON_EXE%" (
  echo Python venv not found: %PYTHON_EXE%
  echo Please run setup_venv.cmd first.
  pause
  exit /b 1
)

if not exist "%APP_FILE%" (
  echo App file not found: %APP_FILE%
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import mootdx, pandas" >nul 2>nul
if errorlevel 1 (
  echo Required Python packages are missing.
  echo Please run setup_venv.cmd first.
  pause
  exit /b 1
)

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if %ERRORLEVEL% EQU 0 (
  echo Port %PORT% is already listening. Opening page.
  start "" "%APP_URL%"
  pause
  exit /b 0
)

echo Starting stock return app...
echo URL: %APP_URL%
echo.
echo Closing this window will stop the service.

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"
"%PYTHON_EXE%" "%APP_FILE%" --port %PORT%

echo.
echo Service exited.
pause
