@echo off
cd /d "%~dp0.."
python engine\setup_voice.py %*
pause
