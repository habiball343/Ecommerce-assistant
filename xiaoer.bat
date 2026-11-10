@echo off
rem ---------------------------------------------------------------------------
rem  Xiaoer launcher wrapper.
rem  Usage: xiaoer.bat start / stop / status / test / reset  [options]
rem  It only locates a Python interpreter (through uv when available) and hands
rem  over to scripts\xiaoer.py, which contains all the real logic.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "UV_EXE="
where uv >nul 2>nul
if not errorlevel 1 set "UV_EXE=uv"
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_EXE if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if defined UV_EXE goto run_with_uv

where py >nul 2>nul
if not errorlevel 1 goto run_with_py
where python >nul 2>nul
if not errorlevel 1 goto run_with_python

echo.
echo [ERROR] Neither uv nor Python was found on this computer.
echo.
echo Install uv first (it downloads and manages Python for you):
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
echo.
echo Then open a new terminal window and run this script again.
exit /b 1

:run_with_uv
"%UV_EXE%" run --no-project scripts\xiaoer.py %*
exit /b %ERRORLEVEL%

:run_with_py
py -3 scripts\xiaoer.py %*
exit /b %ERRORLEVEL%

:run_with_python
python scripts\xiaoer.py %*
exit /b %ERRORLEVEL%
