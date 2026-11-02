@echo off
setlocal
set "PET_NATIVE_DIR=%~dp0"
set "PET_BUILD_TOOLS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
if defined VS_BUILDTOOLS set "PET_BUILD_TOOLS=%VS_BUILDTOOLS%"
call "%PET_BUILD_TOOLS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
if not exist "%PET_NATIVE_DIR%build" mkdir "%PET_NATIVE_DIR%build"
pushd "%PET_NATIVE_DIR%build"
cl /nologo /std:c++17 /utf-8 /W4 /WX /EHsc /Od /MT /Zi /fsanitize=address /DPET_WINDOW_TESTING /DUNICODE /D_UNICODE /DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00 /I"%PET_NATIVE_DIR%src" "%PET_NATIVE_DIR%..\tests\native\lifetime_test.cpp" "%PET_NATIVE_DIR%src\window_bridge.cpp" /Fe:lifetime_asan.exe /Fd:lifetime_asan_compile.pdb /link /INCREMENTAL:NO /DEBUG user32.lib shell32.lib comctl32.lib
if errorlevel 1 goto :failed
lifetime_asan.exe
if errorlevel 1 goto :failed
popd
exit /b 0
:failed
popd
exit /b 1
