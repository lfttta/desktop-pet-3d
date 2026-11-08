@echo off
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%run-dev.local.cmd" call "%ROOT%run-dev.local.cmd"

if not defined PET3D_PYTHON if exist "%ROOT%.venv\Scripts\python.exe" set "PET3D_PYTHON=%ROOT%.venv\Scripts\python.exe"
if not defined PET3D_PYTHON if exist "%ROOT%..\..\3D-pet-gtpdo\.venv\Scripts\python.exe" set "PET3D_PYTHON=%ROOT%..\..\3D-pet-gtpdo\.venv\Scripts\python.exe"
if not defined PET3D_PYTHON for /f "delims=" %%I in ('where python 2^>nul') do if not defined PET3D_PYTHON set "PET3D_PYTHON=%%I"
if not defined PET3D_PYTHON (
  echo Missing Python. Install Python 3.13 or set PET3D_PYTHON to python.exe.
  pause
  exit /b 1
)

if not defined GODOT for /f "delims=" %%I in ('where godot 2^>nul') do if not defined GODOT set "GODOT=%%I"
if not defined GODOT (
  echo Missing Godot. Set GODOT to the Godot 4.7 executable or add it to PATH.
  pause
  exit /b 1
)

if not defined PET3D_DATA set "PET3D_DATA=%LOCALAPPDATA%\Pet3D-v3-dev"

start "" "%GODOT%" --path "%ROOT%app" -- --data-dir="%PET3D_DATA%"
