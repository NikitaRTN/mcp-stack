@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (py -3 run.py & goto :eof)
where python >nul 2>nul && (python run.py & goto :eof)
echo.
echo Python 3.8 or newer is required.
echo Download: https://www.python.org/downloads/windows/
echo During setup enable "Add Python to PATH".
echo.
pause
