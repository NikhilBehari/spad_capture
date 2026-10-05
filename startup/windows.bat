@echo off
REM Double-click to open the dashboard, setting up the environment first if needed.
setlocal
cd /d "%~dp0.."

set URL=http://127.0.0.1:8888
set INSTALL=startup\install\install.ps1

call :find_spad
if not exist "%SPAD%" (
  powershell -ExecutionPolicy Bypass -File %INSTALL% -Yes || (pause & exit /b 1)
  call :find_spad
)

start "" /b powershell -NoProfile -Command ^
  "1..60 | %% { try { iwr -UseBasicParsing %URL% > $null; start %URL%; break } catch { sleep -m 500 } }"

"%SPAD%" tmf capture --mode manual --viz || pause
exit /b

:find_spad
set PREFIX=
for /f "delims=" %%i in ('powershell -ExecutionPolicy Bypass -File %INSTALL% -Prefix') do set PREFIX=%%i
set SPAD=%PREFIX%\Scripts\spad.exe
exit /b
