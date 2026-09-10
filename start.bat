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
if not exist data mkdir data
python server.py
