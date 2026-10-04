@echo off
rem =====================================================================
rem  MLJQ — Sort & Pack Helper : lanceur portable.
rem  Double-clique ce fichier pour ouvrir l'application. Pour un raccourci
rem  sur le Bureau : clic droit -> "Creer un raccourci", puis deplace-le.
rem  Fonctionne sur tout ordinateur ou Python 3 est installe (voir README,
rem  section "Installer sur un autre ordinateur").
rem =====================================================================
cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw gui.py
    exit /b
)
where python >nul 2>&1
if %errorlevel%==0 (
    python gui.py
    exit /b
)

echo.
echo   Python 3 est introuvable sur cet ordinateur.
echo.
echo   1. Installe-le depuis https://www.python.org/downloads/
echo      (coche "Add python.exe to PATH" pendant l'installation).
echo   2. Ouvre ce dossier dans un terminal et lance :  pip install -r requirements.txt
echo   3. Relance ce fichier.
echo.
pause
