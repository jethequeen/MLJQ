@echo off
rem MLJQ - traite UNE fois la file du telephone + la passe d'auto-apply, sans GUI.
rem
rem (Ce fichier est en ASCII sans accents: cmd.exe lit les .bat en page de code OEM, pas en
rem UTF-8, et un seul caractere accentue suffit a casser l'analyse de tout le script.)
rem
rem C'est le mode normal depuis 2026-10-15: un PROCESSUS NEUF a chaque passage relit le code
rem depuis Drive, donc une modification prend effet toute seule. Le GUI, lui, garde ses
rem modules en memoire et demandait un redemarrage a la main apres chaque changement.
rem
rem La machine qui n'est pas proprietaire du dossier partage se retire d'elle-meme
rem (sortpack/lock.py), et deux passes de la MEME machine ne se marchent pas dessus
rem (lock.single_instance). Ce .bat peut donc tourner partout sans rien casser.
setlocal

rem Le projet vit sur Drive: avant l'ouverture de session, G: n'existe pas encore. On sort
rem sans rien dire - il n'y a meme pas ou ecrire un journal. La passe suivante le retrouvera.
if not exist "%~dp0sortpack\remote.py" exit /b 0
cd /d "%~dp0" || exit /b 0

rem Python: le lanceur officiel d'abord, puis le PATH, puis le chemin du Store en dernier
rem recours. L'ancien chemin etait code en dur pour un seul profil utilisateur - il echouait
rem en silence sur toute autre machine.
set "PY="
where py >nul 2>&1
if %errorlevel% equ 0 set "PY=py -3"
if defined PY goto :gotpy
where python >nul 2>&1
if %errorlevel% equ 0 set "PY=python"
if defined PY goto :gotpy
set "PY=C:\Users\jerem\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe"
:gotpy

rem Le journal porte des caracteres comme la pastille de pause: on fixe l encodage
rem plutot que de dependre de la page de code du poste. Se relit avec
rem   Get-Content reports\remote.log -Encoding UTF8
set "PYTHONIOENCODING=utf-8"

if not exist "%~dp0reports" mkdir "%~dp0reports"
set "LOG=%~dp0reports\remote.log"
rem Aux 5 minutes, un journal qui ne tourne pas finit par remplir le disque: au-dela de
rem 2 Mo on repart a neuf.
if exist "%LOG%" for %%F in ("%LOG%") do if %%~zF GTR 2000000 del "%LOG%"

echo ---- remote %date% %time% ---->> "%LOG%"
%PY% -m sortpack.remote>> "%LOG%" 2>&1
echo done %date% %time%>> "%LOG%"
endlocal
