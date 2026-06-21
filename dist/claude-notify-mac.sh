#!/usr/bin/env bash
# Claude Code hook -> hook_relay (slack / telegram / discord) — macOS
#
# stdin : Claude Code hook JSON (Stop / SubagentStop / Notification)
# 의존  : jq, curl  (Bash 3.2 호환)
#
# 환경변수
#   NOTIFY_API_URL  (필수) hook_relay 베이스 URL. 예: http://relay.example.com:20000
#                   (끝에 /notify 를 붙이지 말 것 — 스크립트가 붙인다)
#   NOTIFY_APP      (선택) slack | telegram | discord.  기본 slack.
#   NOTIFY_USER     (선택) (app,username)->channel 매핑 키.  기본 $(whoami).
#   NOTIFY_ACCOUNT  (선택) 구독 계정 직접 지정. 미지정 시
#                   ~/.claude.json 의 oauthAccount.emailAddress 사용.
#   NOTIFY_DEBUG    (선택) =1 이면 후크 원본 JSON 을 NOTIFY_DEBUG_LOG 에 적재
#                   (기본 ~/.claude/logs/notify-debug.jsonl). 묵음 이벤트도 기록.
#   NOTIFY_DRYRUN   (선택) =1 이면 발송하지 않고 결정된 status 만 stdout 출력(테스트용).

set -euo pipefail

API_URL="${NOTIFY_API_URL:?NOTIFY_API_URL 환경변수가 필요합니다}"
API_URL="${API_URL%/}"                       # 끝 슬래시 제거
APP="${NOTIFY_APP:-slack}"
USER_KEY="${NOTIFY_USER:-$(whoami)}"

# -- hook stdin JSON 파싱 --
INPUT="$(cat)"
session_id="$(jq -r      '.session_id // ""'          <<<"$INPUT")"
cwd="$(jq -r             '.cwd // ""'                 <<<"$INPUT")"
event="$(jq -r           '.hook_event_name // ""'     <<<"$INPUT")"
transcript_path="$(jq -r '.transcript_path // ""'     <<<"$INPUT")"
agent_id="$(jq -r        '.agent_id // ""'            <<<"$INPUT")"     # 서브에이전트 컨텍스트에서만 채워짐
agent_type="$(jq -r      '.agent_type // ""'          <<<"$INPUT")"
stop_active="$(jq -r     '.stop_hook_active // false' <<<"$INPUT")"     # true = 자율 루프가 세션을 계속 잇는 중
ntype="$(jq -r           '.notification_type // ""'   <<<"$INPUT")"

# -- 디버그 캡처(옵션): 묵음 포함 모든 이벤트의 원본 JSON 적재 --
if [[ "${NOTIFY_DEBUG:-}" == "1" ]]; then
  dbg="${NOTIFY_DEBUG_LOG:-$HOME/.claude/logs/notify-debug.jsonl}"
  mkdir -p "$(dirname "$dbg")" 2>/dev/null || true
  printf '%s\n' "$INPUT" >> "$dbg" 2>/dev/null || true
fi

# -- OMC 팀 워커 세션(orchestration 내부 워커)은 사용자에게 알리지 않음 --
#    OMC가 워커 세션 환경에만 OMC_TEAM_WORKER 를 주입한다. 메인/대화형 세션엔 없으므로 메인 완료는 영향 없음.
[[ -n "${OMC_TEAM_WORKER:-}${OMX_TEAM_WORKER:-}" ]] && exit 0

# -- 상태 매핑: "메인 세션의 실제 상태"만 보고 --
#   Stop         : 메인 세션 최종 완료만 알림
#                  · agent_id/agent_type 있음(서브에이전트·팀원) → 묵음 (메인 아님)
#                  · stop_hook_active=true(autopilot/ralph 등 자율 루프 진행중) → 묵음 (아직 작업중)
#   SubagentStop : 서브에이전트 완료 → 항상 묵음 (메인 세션 상태 아님)
#   Notification : elicitation_dialog/permission_prompt → 선택지 대기(사용자 결정 대기)
#                  idle_prompt → 입력 대기(유휴) — 선택지 대기와 구분
#                  그 외(auth_success, elicitation_complete/response 등) → 묵음
case "$event" in
  Stop)
    [[ -n "$agent_id" || -n "$agent_type" ]] && exit 0
    [[ "$stop_active" == "true" ]]           && exit 0
    status="task_complete"
    ;;
  SubagentStop)
    exit 0
    ;;
  Notification)
    case "$ntype" in
      elicitation_dialog|permission_prompt) status="awaiting_choice" ;;
      idle_prompt)                          status="awaiting_input" ;;
      *)                                    exit 0 ;;
    esac
    ;;
  *)
    exit 0
    ;;
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

# -- DRYRUN: 발송 대신 결정 결과만 출력 --
if [[ "${NOTIFY_DRYRUN:-}" == "1" ]]; then
  printf 'SEND status=%s event=%s ntype=%s app=%s user=%s session=%s\n' \
    "$status" "$event" "$ntype" "$APP" "$USER_KEY" "$session_name"
  exit 0
fi

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
