@echo off
rem Type "explain <set number>" or "explain <part key>" from any terminal
rem (this folder is on the user PATH). Examples:
rem     explain 76342
rem     explain P3001@5
cd /d "%~dp0"
python value.py explain %*
