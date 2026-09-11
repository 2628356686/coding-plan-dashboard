@echo off
rem Coding Plan Dashboard - run from source (Windows)
rem Usage: double-click or run `start.bat`; open the shown URL in a browser
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
if not exist log mkdir log
set LOG_PATH=%CD%\log\dashboard.log
if not exist data mkdir data

rem Service port: a "port" set in data/gateway.json (gateway settings page) wins over PORT
set LISTEN_PORT=%PORT%
if exist data\gateway.json (
    for /f "tokens=2 delims=:, " %%p in ('findstr /C:"\"port\"" data\gateway.json 2^>nul') do (
        if not "%%p"=="null" if not "%%p"=="" set LISTEN_PORT=%%p
    )
)

rem Kill any existing process on the listen port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%LISTEN_PORT%.*LISTENING"') do (
    echo Killing old process PID %%a ...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting Coding Plan Dashboard on http://127.0.0.1:%LISTEN_PORT% ...
python server.py
if errorlevel 1 (
    echo.
    echo Server exited with error. Press any key to close...
    pause >nul
)
