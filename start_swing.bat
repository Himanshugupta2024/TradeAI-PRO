@echo off
title Swing + Intraday Server
color 0A
cls

echo.
echo  ============================================================
echo   SWING + INTRADAY LIGHTWEIGHT SERVER
echo   Sirf Swing aur Intraday tab active rahenge
echo   Baaki sab BAND hai (CPU/heat save mode)
echo  ============================================================
echo.
echo  [1] Normal mode   - 60 symbols, daily data fetch (2-3 min warmup)
echo  [2] Fast mode     - Sirf nightly shortlist load (instant, no fetch)
echo  [3] Custom        - Apne symbols count likho
echo  [4] Exit
echo.

set /p choice="  Apna option chunno (1/2/3/4): "

if "%choice%"=="1" goto NORMAL
if "%choice%"=="2" goto FAST
if "%choice%"=="3" goto CUSTOM
if "%choice%"=="4" goto END
goto INVALID

:NORMAL
echo.
echo  Starting Normal mode (60 symbols)...
echo  Browser mein kholo: http://localhost:8000
echo  Swing tab mein data 30-60 sec mein aayega
echo.
cd /d "%~dp0"
call venv_torch\Scripts\activate.bat 2>nul || call venv\Scripts\activate.bat 2>nul || call .venv\Scripts\activate.bat 2>nul
python swing_server.py --host 127.0.0.1 --port 8000 --symbols 60
goto END

:FAST
echo.
echo  Starting Fast mode (no daily fetch)...
echo  Sirf nightly shortlist dikhega - instant start!
echo  Browser mein kholo: http://localhost:8000
echo.
cd /d "%~dp0"
call venv_torch\Scripts\activate.bat 2>nul || call venv\Scripts\activate.bat 2>nul || call .venv\Scripts\activate.bat 2>nul
python swing_server.py --host 127.0.0.1 --port 8000 --no-daily
goto END

:CUSTOM
echo.
set /p sym_count="  Kitne symbols scan karein? (10-200): "
echo.
echo  Starting Custom mode (%sym_count% symbols)...
echo  Browser mein kholo: http://localhost:8000
echo.
cd /d "%~dp0"
call venv_torch\Scripts\activate.bat 2>nul || call venv\Scripts\activate.bat 2>nul || call .venv\Scripts\activate.bat 2>nul
python swing_server.py --host 127.0.0.1 --port 8000 --symbols %sym_count%
goto END

:INVALID
echo.
echo  Galat option! Dobara chalao.
echo.
pause
goto END

:END
echo.
echo  Server band ho gaya.
pause
