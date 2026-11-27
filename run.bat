@echo off
cd /d "%~dp0"
python extract_meta_ai.py %*
echo.
pause
