@echo off
setlocal
set "PET_NATIVE_DIR=%~dp0"
rem Safe default: compile only. Publishing requires the explicit stage1 target.
if not "%~1"=="" if /I not "%~1"=="--build-only" if /I not "%~1"=="--stage1" (
    echo Usage: build.cmd [--build-only ^| --stage1]
    exit /b 2
)
set "PET_OUTPUT=%PET_NATIVE_DIR%..\app\addons\pet_window\bin"
set "PET_BUILD_TOOLS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
if defined VS_BUILDTOOLS set "PET_BUILD_TOOLS=%VS_BUILDTOOLS%"
call "%PET_BUILD_TOOLS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
if not exist "%PET_NATIVE_DIR%build" mkdir "%PET_NATIVE_DIR%build"
pushd "%PET_NATIVE_DIR%build"
rem Release: optimized, static CRT. This is the DLL the app/export consumes.
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /O2 /MT /Zi /Brepro /DUNICODE /D_UNICODE /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" /I"%PET_NATIVE_DIR%vendor\godot" /LD "%PET_NATIVE_DIR%src\window_bridge.cpp" "%PET_NATIVE_DIR%src\stage1_resources.cpp" "%PET_NATIVE_DIR%src\godot_entry.cpp" /Fe:pet_window.dll /link /INCREMENTAL:NO /Brepro /DEBUG:FULL /PDB:pet_window.pdb user32.lib shell32.lib comctl32.lib advapi32.lib bcrypt.lib
if errorlevel 1 goto :failed
rem Debug: unoptimized with full symbols, separate output. Still /MT so no
rem debug-CRT install is required on development machines.
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /Od /MT /Zi /Brepro /DUNICODE /D_UNICODE /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" /I"%PET_NATIVE_DIR%vendor\godot" /LD "%PET_NATIVE_DIR%src\window_bridge.cpp" "%PET_NATIVE_DIR%src\stage1_resources.cpp" "%PET_NATIVE_DIR%src\godot_entry.cpp" /Fe:pet_window_debug.dll /link /INCREMENTAL:NO /Brepro /DEBUG:FULL /PDB:pet_window_debug.pdb user32.lib shell32.lib comctl32.lib advapi32.lib bcrypt.lib
if errorlevel 1 goto :failed
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /O2 /MT /Zi /Brepro /DUNICODE /D_UNICODE /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" "%PET_NATIVE_DIR%..\tests\native\window_bridge_test.cpp" "%PET_NATIVE_DIR%src\window_bridge.cpp" "%PET_NATIVE_DIR%src\stage1_resources.cpp" /Fe:window_bridge_test.exe /link /INCREMENTAL:NO /Brepro user32.lib shell32.lib comctl32.lib advapi32.lib bcrypt.lib
if errorlevel 1 goto :failed
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /O2 /MT /Zi /Brepro /DUNICODE /D_UNICODE /DPET_WINDOW_TESTING /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" "%PET_NATIVE_DIR%..\tests\native\lifetime_test.cpp" "%PET_NATIVE_DIR%src\window_bridge.cpp" "%PET_NATIVE_DIR%src\stage1_resources.cpp" /Fe:lifetime_test.exe /link /INCREMENTAL:NO /Brepro user32.lib shell32.lib comctl32.lib advapi32.lib bcrypt.lib
if errorlevel 1 goto :failed
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /O2 /MT /Zi /Brepro /DUNICODE /D_UNICODE /DPET_WINDOW_TESTING /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" "%PET_NATIVE_DIR%..\tests\native\stage1_test.cpp" "%PET_NATIVE_DIR%src\window_bridge.cpp" "%PET_NATIVE_DIR%src\stage1_resources.cpp" /Fe:stage1_test.exe /link /INCREMENTAL:NO /Brepro user32.lib shell32.lib comctl32.lib advapi32.lib bcrypt.lib
if errorlevel 1 goto :failed
if /I not "%~1"=="--stage1" goto :built
if not exist "%PET_OUTPUT%" mkdir "%PET_OUTPUT%"
copy /y pet_window.dll "%PET_OUTPUT%\pet_window.windows.release.x86_64.dll" >nul
if errorlevel 1 goto :failed
copy /y pet_window_debug.dll "%PET_OUTPUT%\pet_window.windows.debug.x86_64.dll" >nul
if errorlevel 1 goto :failed
copy /y pet_window.pdb "%PET_OUTPUT%\pet_window.pdb" >nul
if errorlevel 1 goto :failed
copy /y pet_window_debug.pdb "%PET_OUTPUT%\pet_window_debug.pdb" >nul
if errorlevel 1 goto :failed
:built
popd
echo Built x64 bridge (optimized release + unoptimized debug DLL) and three native regression executables.
exit /b 0
:failed
popd
exit /b 1
