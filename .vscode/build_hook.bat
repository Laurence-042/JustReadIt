@echo off
:: Locate the latest Visual Studio installation via vswhere, then
:: initialize the x64 MSVC environment and invoke build.ps1.

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo ERROR: vswhere.exe not found.
    echo Install Visual Studio 2019 / 2022 with the "Desktop development with C++" workload.
    exit /b 1
)

set "VSINSTALL="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -property installationPath`) do set "VSINSTALL=%%i"

if not defined VSINSTALL (
    echo ERROR: No Visual Studio installation found.
    exit /b 1
)

call "%VSINSTALL%\VC\Auxiliary\Build\vcvarsall.bat" x64 > nul 2>&1
if errorlevel 1 (
    echo ERROR: vcvarsall.bat x64 failed.
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0..\src\hook\build.ps1" %*
exit /b %ERRORLEVEL%
