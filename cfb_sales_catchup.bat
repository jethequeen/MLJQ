@echo off
rem MLJQ - daily CFB sales catch-up. Posts every complete week (Mon-Fri) since the anchor
rem that isn't already in the Journal (idempotent), converting US at the Bank of Canada
rem rate for the week-ending date. Cookie expiry emails once and leaves weeks pending.
rem NOTE: full path to python.exe (the bare "python" alias does not resolve in Task
rem Scheduler). If Python is upgraded, update PY below.
set PY="C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
cd /d "%~dp0"
if not exist "%~dp0reports" mkdir "%~dp0reports"
echo ---- cfb_sales %date% %time% ---->> "%~dp0reports\cfb_sales.log"
%PY% -m sortpack.cfb_sales --catchup --commit>> "%~dp0reports\cfb_sales.log" 2>&1
echo done %date% %time%>> "%~dp0reports\cfb_sales.log"
