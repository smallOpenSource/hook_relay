#!/usr/bin/env bash
# hook_relay 서버 구성 (Linux, systemd) — 다중 메신저 + 채널 라우팅 + 웹 UI
#
# 실행 (dist/ 안에서; 필요한 앱 토큰만 주면 됨):
#   SLACK_BOT_TOKEN='xoxb-...' DATA_ADMIN_TOKEN='정한값' bash setup_hook_relay.sh
#
# 모든 설정은 환경변수 (기본값 존재):
#   APP_DIR(기본 /app/hook_relay) · CONDA_BASE(기본 /app/miniconda3) · ENV_NAME(기본 hook_relay)
#   HOST(기본 0.0.0.0) · APP_PORT(기본 20000) · UI_PORT(기본 20001)
#   SLACK_BOT_TOKEN / TELEGRAM_BOT_TOKEN / DISCORD_BOT_TOKEN / DATA_ADMIN_TOKEN
#
# 결과:
#   $APP_DIR/{app.py, run_hook_relay.sh, channels.csv, hook_relay.env}
#   ~/.config/systemd/user/{hook_relay,hook_relay-web}.service   (wrapper + 역할)
# 비고: host·port·interpreter 모두 env 기반. 기존 hook_relay.env 가 있으면 보존(비밀값 유지).

set -euo pipefail

APP_DIR="${APP_DIR:-/app/hook_relay}"
CONDA_BASE="${CONDA_BASE:-/app/miniconda3}"
ENV_NAME="${ENV_NAME:-hook_relay}"
HOST="${HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-20000}"
UI_PORT="${UI_PORT:-20001}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$CONDA_BASE/envs/$ENV_NAME/bin/python"
PIP="$CONDA_BASE/envs/$ENV_NAME/bin/pip"

[[ -f "$SRC_DIR/app.py" ]] || { echo "app.py 가 이 스크립트(dist)와 같은 폴더에 있어야 합니다" >&2; exit 1; }
[[ -f "$SRC_DIR/run_hook_relay.sh" ]] || { echo "run_hook_relay.sh 가 같은 폴더에 있어야 합니다" >&2; exit 1; }

mkdir -p "$APP_DIR"

# -- conda env --
set +u
source "$CONDA_BASE/etc/profile.d/conda.sh"
set -u
conda env list | grep -qE "^${ENV_NAME}[[:space:]]" || conda create -y -n "$ENV_NAME" python=3.11
"$PIP" install --upgrade fastapi "uvicorn[standard]" requests

# -- app.py / wrapper 배치 (소스와 다르면 복사) --
[[ "$SRC_DIR/app.py" -ef "$APP_DIR/app.py" ]] || cp "$SRC_DIR/app.py" "$APP_DIR/app.py"
[[ "$SRC_DIR/run_hook_relay.sh" -ef "$APP_DIR/run_hook_relay.sh" ]] || cp "$SRC_DIR/run_hook_relay.sh" "$APP_DIR/run_hook_relay.sh"
chmod +x "$APP_DIR/run_hook_relay.sh"

# -- channels.csv 초기화(있으면 보존) --
[[ -f "$APP_DIR/channels.csv" ]] || printf 'app,username,channel\n' > "$APP_DIR/channels.csv"

# -- env (없을 때만 생성; 비밀값 보존) --
if [[ -f "$APP_DIR/hook_relay.env" ]]; then
  echo "기존 hook_relay.env 보존(덮어쓰지 않음). 값 변경은 파일 직접 수정 후 재시작."
else
  umask 077
  cat > "$APP_DIR/hook_relay.env" <<EOF
# hook_relay 환경변수 (0600 · 커밋 금지 · 모든 설정값을 여기서 관리)
HOST=${HOST}
APP_PORT=${APP_PORT}
UI_PORT=${UI_PORT}
UVICORN_PYTHON=${PY}
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

# -- systemd user services (wrapper + 역할, 같은 app.py 두 포트) --
U="$HOME/.config/systemd/user"
mkdir -p "$U"
make_unit() {  # $1=name $2=role $3=desc
  cat > "$U/$1.service" <<EOF
[Unit]
Description=$3
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/hook_relay.env
ExecStart=$APP_DIR/run_hook_relay.sh $2
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
}
make_unit hook_relay api "hook_relay API (Claude Code -> messengers)"
make_unit hook_relay-web web "hook_relay web UI (channel routing)"

loginctl enable-linger "$USER" || true
systemctl --user daemon-reload
systemctl --user enable --now hook_relay.service hook_relay-web.service
systemctl --user --no-pager status hook_relay.service hook_relay-web.service || true

echo
echo "API(hook): $HOST:$APP_PORT   웹 UI: http://<서버>:$UI_PORT"
echo "방화벽:"
echo "  sudo firewall-cmd --permanent --add-port=$APP_PORT/tcp --add-port=$UI_PORT/tcp && sudo firewall-cmd --reload"
