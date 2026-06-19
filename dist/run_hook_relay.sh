#!/usr/bin/env bash
# hook_relay 실행 래퍼 (Linux) — env 로드 후 역할별로 HOST:PORT 바인딩하여 uvicorn 기동.
#   systemd ExecStart 또는 수동 실행:  run_hook_relay.sh <api|web>
#     - api 역할 → $APP_PORT,  web 역할 → $UI_PORT  (둘 다 $HOST 에 바인딩)
#     - 인터프리터는 $UVICORN_PYTHON (없으면 python3) — 경로 하드코딩 없음
#   ★ host·port·interpreter 가 전부 env 기반 → 값 변경 후 서비스 재시작만으로 반영.

set -euo pipefail

ROLE="${1:?역할을 주세요: api 또는 web}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# env 파일 로드(있으면) — systemd EnvironmentFile 과 별개로 standalone 실행도 지원
if [[ -f "$APP_DIR/hook_relay.env" ]]; then
  set -a; . "$APP_DIR/hook_relay.env"; set +a
fi

HOST="${HOST:-0.0.0.0}"
case "$ROLE" in
  api) PORT="${APP_PORT:-20000}" ;;
  web) PORT="${UI_PORT:-20001}" ;;
  *)   echo "역할은 api 또는 web 이어야 합니다 (받음: $ROLE)" >&2; exit 1 ;;
esac

PY="${UVICORN_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

export HOOK_RELAY_ROLE="$ROLE"   # app.py 의 web 역할 암호 게이트에 사용

# TLS: web 역할에서만 적용(SSL_CERTFILE+SSL_KEYFILE 가 설정·존재할 때). 경로는 env 로만(하드코딩 없음).
#   ★ api(hook 수신) 역할은 평문 유지 → 기존 후크(http://…:APP_PORT) 무중단.
SSL_ARGS=()
if [[ "$ROLE" == "web" && -n "${SSL_CERTFILE:-}" && -n "${SSL_KEYFILE:-}" ]]; then
  if [[ -f "$SSL_CERTFILE" && -f "$SSL_KEYFILE" ]]; then
    SSL_ARGS=(--ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE")
    echo "hook_relay[web] TLS enabled (cert: ${SSL_CERTFILE})" >&2
  else
    echo "hook_relay[web] WARN: SSL_CERTFILE/SSL_KEYFILE 설정됐으나 파일 없음 → 평문 기동" >&2
  fi
fi

echo "hook_relay[$ROLE] binding ${HOST}:${PORT} via ${PY}" >&2
exec "$PY" -m uvicorn app:app --host "$HOST" --port "$PORT" "${SSL_ARGS[@]}"
