@echo off
REM Windows Task Scheduler entry point. Edit the path below to your project folder.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m portfolio_pulse run-daily
