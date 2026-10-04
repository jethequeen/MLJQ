@echo off
rem MLJQ - weekly Amazon buy report, headless. Scrapes Brickset/Amazon, stores the
rem price history, ranks the buy list into reports\buy_list.csv, and POSTs it to the
rem Google Sheet when BUY_REPORT_POST_URL is configured. No browser window.
rem NOTE: full path to python.exe (the bare "python" alias does not resolve in Task
rem Scheduler). If Python is upgraded, update PY below.
set PY="C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
cd /d "%~dp0"
if not exist "%~dp0reports" mkdir "%~dp0reports"
echo ---- buyreport %date% %time% ---->> "%~dp0reports\buyreport.log"
%PY% value.py job --post>> "%~dp0reports\buyreport.log" 2>&1
echo done %date% %time%>> "%~dp0reports\buyreport.log"
