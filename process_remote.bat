@echo off
rem MLJQ - process the phone -> PC command queue once (Appliquer / Construire CFB requested
rem from the mobile web-app). Runs headless: writes remarks / builds CFB files locally where
rem BrickStore + the .bsx live, and writes each request's result back for the phone to show.
rem The GUI also processes these automatically while it's open; this .bat is for running them
rem WITHOUT the GUI, e.g. from Task Scheduler every few minutes so requests apply while the PC
rem is on. Remote "Construire CFB" writes the files but does NOT email (config.REMOTE_CFB_EMAIL).
set PY="C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
cd /d "%~dp0"
if not exist "%~dp0reports" mkdir "%~dp0reports"
echo ---- remote %date% %time% ---->> "%~dp0reports\remote.log"
%PY% -m sortpack.remote>> "%~dp0reports\remote.log" 2>&1
echo done %date% %time%>> "%~dp0reports\remote.log"
