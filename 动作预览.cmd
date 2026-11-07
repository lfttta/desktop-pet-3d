@echo off
setlocal
set "GODOT=D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe"
if not exist "%GODOT%" (
  echo Missing Godot: %GODOT%
  pause
  exit /b 1
)
start "" "%GODOT%" --path "%~dp0app" -- --review
