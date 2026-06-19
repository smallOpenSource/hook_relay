#!/usr/bin/env bash
# Claude Code hook -> hook_relay (slack / telegram / discord) — macOS
#
# stdin : Claude Code hook JSON (Stop / Notification)
# 의존  : jq, curl  (Bash 3.2 호환)
#
# 환경변수
#   NOTIFY_API_URL  (필수) hook_relay 베이스 URL. 예: http://relay.example.com:20000
#                   (끝에 /notify 를 붙이지 말 것 — 스크립트가 붙인다)
#   NOTIFY_APP      (선택) slack | telegram | discord.  기본 slack.
#   NOTIFY_USER     (선택) (app,username)->channel 매핑 키.  기본 $(whoami).
#   NOTIFY_ACCOUNT  (선택) 구독 계정 직접 지정. 미지정 시
#                   ~/.claude.json 의 oauthAccount.emailAddress 사용.

set -euo pipefail

API_URL="${NOTIFY_API_URL:?NOTIFY_API_URL 환경변수가 필요합니다}"
API_URL="${API_URL%/}"                       # 끝 슬래시 제거
APP="${NOTIFY_APP:-slack}"
USER_KEY="${NOTIFY_USER:-$(whoami)}"

# -- hook stdin JSON 파싱 --
INPUT="$(cat)"
session_id="$(jq -r '.session_id // ""'      <<<"$INPUT")"
cwd="$(jq -r        '.cwd // ""'             <<<"$INPUT")"
event="$(jq -r      '.hook_event_name // ""' <<<"$INPUT")"
transcript_path="$(jq -r '.transcript_path // ""' <<<"$INPUT")"

# -- 상태 매핑: 작업완료 vs 선택지대기 --
case "$event" in
  Stop)         status="task_complete" ;;
  Notification) status="awaiting_input" ;;
  *)            status="$event" ;;
esac

# -- 세션 이름: /rename 값(transcript) > 캐시 파일 > session_id --
session_name=""
if [[ -n "$transcript_path" && -f "$transcript_path" ]]; then
  session_name="$(jq -rR 'fromjson? // empty
    | select((.type // "") == "system")
    | (.content // "" | tostring)
    | select(test("Session renamed to:"))
    | capture("Session renamed to: (?<n>[^<\"]+)").n' "$transcript_path" 2>/dev/null | tail -1)"
fi
if [[ -z "$session_name" ]]; then
  name_cache="${cwd}/.claude/session-name"
  [[ -f "$name_cache" ]] && session_name="$(cat "$name_cache")"
fi
[[ -z "$session_name" ]] && session_name="$session_id"

# -- 구독 계정 (override 우선, 없으면 ~/.claude.json) --
account="${NOTIFY_ACCOUNT:-}"
if [[ -z "$account" && -f "$HOME/.claude.json" ]]; then
  account="$(jq -r '.oauthAccount.emailAddress // empty' "$HOME/.claude.json" 2>/dev/null || true)"
fi

# -- 시스템 정보 (macOS BSD date 는 %z 지원) --
host="$(hostname)"
ts="$(date +%Y-%m-%dT%H:%M:%S%z)"

# -- 페이로드 구성 + 발송 (app + username 로 라우팅; channel 은 서버가 결정) --
payload="$(jq -n \
  --arg app          "$APP" \
  --arg username     "$USER_KEY" \
  --arg session_name "$session_name" \
  --arg project_path "$cwd" \
  --arg status       "$status" \
  --arg time         "$ts" \
  --arg hostname     "$host" \
  --arg account      "$account" \
  '{app:$app, username:$username, status:$status,
    session_name:$session_name, project_path:$project_path,
    time:$time, hostname:$hostname, claude_account:$account}')"

curl -fsS -X POST "$API_URL/notify" \
  -H "Content-Type: application/json" \
  -d "$payload" >/dev/null
