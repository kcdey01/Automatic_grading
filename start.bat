@echo off
cd /d "%~dp0"
python start.py
if %errorlevel% neq 0 pause