@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: Check if Ollama is already running
set OLLAMA_STARTED=0
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if errorlevel 1 (
    start "" /B ollama serve >NUL 2>&1
    set OLLAMA_STARTED=1
    timeout /t 2 /nobreak >NUL
)

:: Launch GUI (blocks until window is closed)
call venv\Scripts\activate.bat
venv\Scripts\python.exe gui.py

:: Stop Ollama if we started it
if !OLLAMA_STARTED!==1 (
    taskkill /F /IM ollama.exe >NUL 2>&1
)
endlocal
