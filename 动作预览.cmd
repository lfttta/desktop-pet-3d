@echo off
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%run-dev.local.cmd" call "%ROOT%run-dev.local.cmd"

if not defined GODOT for /f "delims=" %%I in ('where godot 2^>nul') do if not defined GODOT set "GODOT=%%I"
if not defined GODOT (
  echo Missing Godot. Set GODOT to the Godot 4.7 executable or add it to PATH.
  pause
  exit /b 1
)

start "" "%GODOT%" --path "%ROOT%app" -- --review
