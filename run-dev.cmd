@echo off
setlocal
set "ROOT=%~dp0"
set "PET3D_PYTHON=D:\danjigame\3D-pet\3D-pet-gtpdo\.venv\Scripts\python.exe"
set "GODOT=D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe"
set "DATA=%LOCALAPPDATA%\Pet3D-v3-dev"

if not exist "%PET3D_PYTHON%" (
  echo Missing Python venv: %PET3D_PYTHON%
  pause
  exit /b 1
)
if not exist "%GODOT%" (
  echo Missing Godot: %GODOT%
  pause
  exit /b 1
)

start "" "%GODOT%" --path "%ROOT%app" -- --data-dir="%DATA%"
