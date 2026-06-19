@echo off
REM hook_relay 실행 래퍼 (Windows) - env 로드 후 역할별 HOST:PORT 로 uvicorn 기동.
REM   예약작업/수동:  run_hook_relay.bat ^<api^|web^>
REM ※ 지연확장(delayed expansion) 미사용 - '!' 포함 토큰 안전.

setlocal
if "%~1"=="" ( echo 역할을 주세요: api 또는 web & exit /b 1 )
set "ROLE=%~1"
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
cd /d "%APP_DIR%"

REM -- env 파일 로드 (KEY=VALUE; eol=# 주석/빈줄 스킵; '!' 안전) --
if exist "%APP_DIR%\hook_relay.env" (
  for /f "usebackq eol=# tokens=1* delims==" %%A in ("%APP_DIR%\hook_relay.env") do set "%%A=%%B"
)

if "%HOST%"=="" set "HOST=0.0.0.0"
if /i "%ROLE%"=="api" set "PORT=%APP_PORT%"
if /i "%ROLE%"=="web" set "PORT=%UI_PORT%"
if "%PORT%"=="" ( echo 역할은 api 또는 web 이어야 합니다 & exit /b 1 )

set "PY=%UVICORN_PYTHON%"
if "%PY%"=="" set "PY=%APP_DIR%\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "HOOK_RELAY_ROLE=%ROLE%"
echo hook_relay[%ROLE%] binding %HOST%:%PORT%
"%PY%" -m uvicorn app:app --host %HOST% --port %PORT%
endlocal
