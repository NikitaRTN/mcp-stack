@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (py -3 run.py --stop & goto :eof)
python run.py --stop
pause
