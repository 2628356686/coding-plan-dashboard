@echo off
rem Coding Plan Dashboard - run from source (Windows)
rem Usage: double-click or run `start.bat`; open http://127.0.0.1:8080
cd /d %~dp0
set PORT=8080
set APP_DIR=%CD%
set INDEX_HTML_PATH=%CD%\index.html
set SNAPSHOT_PATH=%CD%\data\snapshot.json
set REQUESTS_PATH=%CD%\data\requests.json
set RESULTS_PATH=%CD%\data\results.json
set CREDENTIALS_PATH=%CD%\data\credentials.json
set ORDER_PATH=%CD%\data\order.json
set GATEWAY_CONFIG_PATH=%CD%\data\gateway.json
set GATEWAY_STATS_PATH=%CD%\data\gateway_stats.json
if not exist data mkdir data

rem Kill any existing process on port 8080
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%.*LISTENING"') do (
    echo Killing old process PID %%a ...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting Coding Plan Dashboard on http://127.0.0.1:%PORT% ...
python server.py
if errorlevel 1 (
    echo.
    echo Server exited with error. Press any key to close...
    pause >nul
)
