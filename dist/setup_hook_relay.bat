@echo off
REM hook_relay 서버 구성 (Windows) - 다중 메신저 + 채널 라우팅 + 웹 UI
REM
REM 실행 (dist 안에서; 토큰만):
REM   set SLACK_BOT_TOKEN=xoxb-...
REM   set DATA_ADMIN_TOKEN=정한값
REM   setup_hook_relay.bat
REM 환경변수(기본값): HOST(0.0.0.0) APP_PORT(20000) UI_PORT(20001) PYTHON(python)
REM 결과: %APP_DIR%\{app.py, run_hook_relay.bat, channels.csv, hook_relay.env, venv\}
REM       예약작업 HookRelayApi(api)/HookRelayWeb(web) - 로그온 시 자동 기동
REM 비고: host·port·interpreter 모두 env 기반. 기존 hook_relay.env 보존.
REM       (지연확장 미사용: '!' 포함 토큰 안전)

setlocal
if "%HOST%"=="" set "HOST=0.0.0.0"
if "%APP_PORT%"=="" set "APP_PORT=20000"
if "%UI_PORT%"=="" set "UI_PORT=20001"
if "%PYTHON%"=="" set "PYTHON=python"
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "VENV=%APP_DIR%\venv"
set "RUNNER=%APP_DIR%\run_hook_relay.bat"

if not exist "%APP_DIR%\app.py" ( echo [ERROR] app.py 가 같은 폴더에 있어야 합니다 & exit /b 1 )
if not exist "%RUNNER%" ( echo [ERROR] run_hook_relay.bat 가 같은 폴더에 있어야 합니다 & exit /b 1 )
where %PYTHON% >nul 2>&1 || ( echo [ERROR] %PYTHON% 이 PATH 에 없습니다 - https://python.org & exit /b 1 )

if not exist "%VENV%\Scripts\python.exe" %PYTHON% -m venv "%VENV%"
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --upgrade fastapi uvicorn[standard] requests || exit /b 1

if not exist "%APP_DIR%\channels.csv" ( > "%APP_DIR%\channels.csv" echo app,username,channel )

if exist "%APP_DIR%\hook_relay.env" (
  echo 기존 hook_relay.env 보존.
) else (
  > "%APP_DIR%\hook_relay.env" echo HOST=%HOST%
  >> "%APP_DIR%\hook_relay.env" echo APP_PORT=%APP_PORT%
  >> "%APP_DIR%\hook_relay.env" echo UI_PORT=%UI_PORT%
  >> "%APP_DIR%\hook_relay.env" echo UVICORN_PYTHON=%VENV%\Scripts\python.exe
  >> "%APP_DIR%\hook_relay.env" echo CHANNELS_CSV=%APP_DIR%\channels.csv
  >> "%APP_DIR%\hook_relay.env" echo DATA_ADMIN_TOKEN=%DATA_ADMIN_TOKEN%
  >> "%APP_DIR%\hook_relay.env" echo WEB_PASSWORD=%WEB_PASSWORD%
  >> "%APP_DIR%\hook_relay.env" echo SLACK_BOT_TOKEN=%SLACK_BOT_TOKEN%
  >> "%APP_DIR%\hook_relay.env" echo TELEGRAM_BOT_TOKEN=%TELEGRAM_BOT_TOKEN%
  >> "%APP_DIR%\hook_relay.env" echo DISCORD_BOT_TOKEN=%DISCORD_BOT_TOKEN%
  if "%DATA_ADMIN_TOKEN%"=="" echo [WARN] DATA_ADMIN_TOKEN 미설정 - 채널 추가/수정/삭제가 막힙니다.
)

schtasks /Create /TN HookRelayApi /TR "\"%RUNNER%\" api" /SC ONLOGON /RL LIMITED /F
schtasks /Create /TN HookRelayWeb /TR "\"%RUNNER%\" web" /SC ONLOGON /RL LIMITED /F
start "" /MIN "%RUNNER%" api
start "" /MIN "%RUNNER%" web

echo.
echo API(hook): %HOST%:%APP_PORT%    Web UI: http://^<server^>:%UI_PORT%
echo status :  curl http://localhost:%APP_PORT%/health
echo firewall: netsh advfirewall firewall add rule name="hook_relay" dir=in action=allow protocol=TCP localport=%APP_PORT%,%UI_PORT%
echo remove :  schtasks /Delete /TN HookRelayApi /F  ^&  schtasks /Delete /TN HookRelayWeb /F
endlocal
