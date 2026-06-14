@echo off
color 01
title J.A.R.V.I.S
mode con: cols=90 lines=25
cls
echo.
echo  ========================================
echo   J.A.R.V.I.S  ^|  Online
echo  ========================================
echo.
cd /d "%~dp0"
python jarvis.py
pause
