@echo off
rem MLJQ - monthly CFB inventory value. Reads both portals (US->CAD at the Bank of Canada
rem rate) for each configured seller, stores a snapshot, and writes value/pieces into the
rem JUST-CLOSED month's row: Binobrick -> financial "Resultats mensuels" V; MLJQ -> W plus
rem accounting "Sommaire mensuel" B/D. A seller not yet on the portal is skipped. Run 1st.
set PY="C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
cd /d "%~dp0"
if not exist "%~dp0reports" mkdir "%~dp0reports"
echo ---- cfb_inventory %date% %time% ---->> "%~dp0reports\cfb_inventory.log"
%PY% -m sortpack.cfb_inventory --store --post --prev-month>> "%~dp0reports\cfb_inventory.log" 2>&1
echo done %date% %time%>> "%~dp0reports\cfb_inventory.log"
