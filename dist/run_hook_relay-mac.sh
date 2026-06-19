#!/usr/bin/env bash
# hook_relay 실행 래퍼 (macOS) — env 로드 후 역할별로 HOST:PORT 바인딩하여 uvicorn 기동.
#   launchd plist 또는 수동:  run_hook_relay-mac.sh <api|web>
#     - api 역할 → $APP_PORT,  web 역할 → $UI_PORT  (둘 다 $HOST 에 바인딩)
#     - 인터프리터는 $UVICORN_PYTHON (없으면 venv → python3)
set -euo pipefail

ROLE="${1:?역할을 주세요: api 또는 web}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

if [[ -f "$APP_DIR/hook_relay.env" ]]; then
  set -a; . "$APP_DIR/hook_relay.env"; set +a
fi

HOST="${HOST:-0.0.0.0}"
case "$ROLE" in
  api) PORT="${APP_PORT:-20000}" ;;
  web) PORT="${UI_PORT:-20001}" ;;
  *)   echo "역할은 api 또는 web 이어야 합니다 (받음: $ROLE)" >&2; exit 1 ;;
esac

PY="${UVICORN_PYTHON:-$APP_DIR/venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"

export HOOK_RELAY_ROLE="$ROLE"   # app.py 의 web 역할 암호 게이트에 사용
echo "hook_relay[$ROLE] binding ${HOST}:${PORT} via ${PY}" >&2
exec "$PY" -m uvicorn app:app --host "$HOST" --port "$PORT"
