@echo off
setlocal
set "PROJETO=%~dp0"
if not exist "%PROJETO%.venv\Scripts\pythonw.exe" (
  echo Ambiente Python nao encontrado em .venv.
  echo Execute a instalacao descrita no README.md.
  pause
  exit /b 1
)
start "" "%PROJETO%.venv\Scripts\pythonw.exe" "%PROJETO%gui.py"
endlocal
