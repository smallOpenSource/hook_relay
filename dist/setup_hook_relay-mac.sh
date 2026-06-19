#!/usr/bin/env bash
# hook_relay 서버 구성 (macOS, launchd) — 다중 메신저 + 채널 라우팅 + 웹 UI
#
# 실행 (dist/ 안에서; 필요한 앱 토큰만):
#   SLACK_BOT_TOKEN='xoxb-...' DATA_ADMIN_TOKEN='정한값' bash setup_hook_relay-mac.sh
#
# 환경변수(기본값): APP_DIR(기본: 이 폴더) HOST(0.0.0.0) APP_PORT(20000) UI_PORT(20001) PYTHON(python3)
# 결과: $APP_DIR/{app.py, run_hook_relay-mac.sh, channels.csv, hook_relay.env, venv/}
#       ~/Library/LaunchAgents/com.hookrelay.{api,web}.plist   (wrapper + 역할)
# 비고: host·port·interpreter 모두 env 기반. 기존 hook_relay.env 보존(비밀값 유지).

set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${APP_DIR:-$SRC_DIR}"
HOST="${HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-20000}"
UI_PORT="${UI_PORT:-20001}"
PYTHON="${PYTHON:-python3}"
VENV="$APP_DIR/venv"
RUNNER="$APP_DIR/run_hook_relay-mac.sh"
LA="$HOME/Library/LaunchAgents"

[[ -f "$SRC_DIR/app.py" ]] || { echo "app.py 가 같은 폴더에 있어야 합니다" >&2; exit 1; }
[[ -f "$SRC_DIR/run_hook_relay-mac.sh" ]] || { echo "run_hook_relay-mac.sh 가 같은 폴더에 있어야 합니다" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "$PYTHON 필요 (brew install python)" >&2; exit 1; }

mkdir -p "$APP_DIR" "$LA"
[[ "$SRC_DIR/app.py" -ef "$APP_DIR/app.py" ]] || cp "$SRC_DIR/app.py" "$APP_DIR/app.py"
[[ "$SRC_DIR/run_hook_relay-mac.sh" -ef "$RUNNER" ]] || cp "$SRC_DIR/run_hook_relay-mac.sh" "$RUNNER"
chmod +x "$RUNNER"

[[ -d "$VENV" ]] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install --upgrade fastapi "uvicorn[standard]" requests

[[ -f "$APP_DIR/channels.csv" ]] || printf 'app,username,channel\n' > "$APP_DIR/channels.csv"

if [[ -f "$APP_DIR/hook_relay.env" ]]; then
  echo "기존 hook_relay.env 보존(덮어쓰지 않음)."
else
  umask 077
  cat > "$APP_DIR/hook_relay.env" <<EOF
# hook_relay 환경변수 (0600 · 커밋 금지)
HOST=${HOST}
APP_PORT=${APP_PORT}
UI_PORT=${UI_PORT}
UVICORN_PYTHON=${VENV}/bin/python
CHANNELS_CSV=${APP_DIR}/channels.csv
DATA_ADMIN_TOKEN=${DATA_ADMIN_TOKEN:-}
WEB_PASSWORD=${WEB_PASSWORD:-}
SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
# 선택: SLACK_API_URL / TELEGRAM_API_BASE / DISCORD_API_BASE
EOF
  chmod 600 "$APP_DIR/hook_relay.env"
  [[ -n "${DATA_ADMIN_TOKEN:-}" ]] || echo "주의: DATA_ADMIN_TOKEN 미설정 — 채널 추가/수정/삭제가 막힙니다."
fi

make_plist() {  # $1=label $2=role
  cat > "$LA/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER</string>
    <string>$2</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/$1.out.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/$1.err.log</string>
</dict>
</plist>
EOF
}
make_plist com.hookrelay.api api
make_plist com.hookrelay.web web

launchctl unload "$LA/com.hookrelay.api.plist" 2>/dev/null || true
launchctl unload "$LA/com.hookrelay.web.plist" 2>/dev/null || true
launchctl load "$LA/com.hookrelay.api.plist"
launchctl load "$LA/com.hookrelay.web.plist"

echo
echo "API(hook): $HOST:$APP_PORT   웹 UI: http://<서버>:$UI_PORT"
echo "상태: launchctl list | grep hookrelay ; curl http://localhost:$APP_PORT/health"
echo "해제: launchctl unload $LA/com.hookrelay.api.plist $LA/com.hookrelay.web.plist"
