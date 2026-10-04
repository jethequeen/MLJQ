@echo off
rem MLJQ - full catalog value baseline. Runs every 60 days via Task Scheduler.
rem Snapshots the current price guide (idempotent, keyed by BrickStore's price date)
rem and today's value of every set, into history\mljq_history.sqlite.
rem NOTE: full path to python.exe (the bare "python" alias does not resolve inside
rem Task Scheduler). If Python is upgraded, update PY below.
set PY="C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
cd /d "%~dp0"
echo ---- baseline %date% %time% ---->> "%~dp0history\snapshot.log"
%PY% value.py --snapshot-prices>> "%~dp0history\snapshot.log" 2>&1
%PY% value.py --all --snapshot>> "%~dp0history\snapshot.log" 2>&1
echo done %date% %time%>> "%~dp0history\snapshot.log"
