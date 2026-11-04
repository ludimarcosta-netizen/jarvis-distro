@echo off
cd /d "%~dp0.."
set "HERMES_HOME=%CD%"
"hermes-agent\venv\Scripts\python.exe" "jarvis\hermes_runner.py" %*
