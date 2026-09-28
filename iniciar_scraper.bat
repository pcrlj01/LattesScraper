@echo off
cd /d "%~dp0"
title LattesScraper
echo ======================================================
echo             LATTES SCRAPER & EXTRACTOR
echo ======================================================
echo.
.venv\Scripts\python.exe run.py
echo.
pause
