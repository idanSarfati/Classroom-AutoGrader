@echo off
REM Daily Classroom check - read-only / simulation only.
REM Usage:
REM   run_daily_check.bat                 (single scan now)
REM   run_daily_check.bat --daemon --time 06:00   (background, daily 06:00)
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m src.daily_check %*
) else (
  python -m src.daily_check %*
)

