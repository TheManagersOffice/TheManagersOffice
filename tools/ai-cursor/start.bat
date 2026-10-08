@echo off
title AI Cursor
cd /d "%~dp0"
python ai_cursor.py %*
if errorlevel 1 pause
