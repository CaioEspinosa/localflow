@echo off
cd /d "%~dp0"
tasklist /FI "IMAGENAME eq ollama.exe" | find /I "ollama.exe" >nul || start "" /B "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
.venv\Scripts\python.exe app.py
pause
