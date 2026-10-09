@echo off
rem Starts the API + dashboard with the settings from .env (see .env.example).
cd /d "%~dp0.."
if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" set "%%a=%%b"
  )
)
if not exist logs mkdir logs
echo [%date% %time%] starting gold-trader server >> logs\server.log
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" -m server >> logs\server.log 2>&1
