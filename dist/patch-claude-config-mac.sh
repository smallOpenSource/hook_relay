#!/usr/bin/env bash
# Claude Code 설정 패치 (macOS) — settings.json 병합 (vi 미사용, jq)
#
# 실행:
#   NOTIFY_API_URL='http://relay.example.com:20000' \
#   NOTIFY_APP='slack' NOTIFY_USER='minsu' bash patch-claude-config-mac.sh
#
# 선택 환경변수:
#   HOOK_PATH    hook 스크립트 경로 (기본: ~/.claude/hooks/claude-notify-mac.sh)
#   NOTIFY_APP   slack|telegram|discord (settings.json env 에 주입; 미지정 시 생략)
#   NOTIFY_USER  (app,username)->channel 매핑 키 (미지정 시 생략)
#
# 제약: Notification 알림은 CLI 에서만 발화한다(VSCode/IDE 확장 버그). Stop 은 양쪽 OK.

set -euo pipefail

: "${NOTIFY_API_URL:?NOTIFY_API_URL 환경변수가 필요합니다}"
HOOK_PATH="${HOOK_PATH:-$HOME/.claude/hooks/claude-notify-mac.sh}"
SETTINGS="$HOME/.claude/settings.json"

command -v jq >/dev/null || { echo "jq 가 필요합니다 (brew install jq)" >&2; exit 1; }

mkdir -p "$(dirname "$SETTINGS")" "$(dirname "$HOOK_PATH")"
[[ -f "$SETTINGS" ]] || echo '{}' > "$SETTINGS"

# 백업
cp "$SETTINGS" "${SETTINGS}.bak.$(date +%s)"

# settings.json 병합
tmp="$(mktemp)"
jq \
  --arg cmd  "$HOOK_PATH" \
  --arg url  "$NOTIFY_API_URL" \
  --arg app  "${NOTIFY_APP:-}" \
  --arg user "${NOTIFY_USER:-}" \
  '
  .env = ((.env // {}) + {NOTIFY_API_URL: $url}
          + (if $app  != "" then {NOTIFY_APP:  $app}  else {} end)
          + (if $user != "" then {NOTIFY_USER: $user} else {} end))
  | .hooks = (.hooks // {})
  | .hooks.Stop = [ { hooks: [ { type: "command", command: $cmd, async: true } ] } ]
  | .hooks.Notification = [ {
      matcher: "idle_prompt|permission_prompt|elicitation_dialog",
      hooks: [ { type: "command", command: $cmd, async: true } ]
    } ]
  ' "$SETTINGS" > "$tmp"
mv "$tmp" "$SETTINGS"
echo "패치 완료: $SETTINGS"

# hook 실행권한
[[ -f "$HOOK_PATH" ]] && chmod +x "$HOOK_PATH" || \
  echo "참고: $HOOK_PATH 에 claude-notify-mac.sh 를 배치하고 chmod +x 하세요."

# 구독 계정 검증
CJ="$HOME/.claude.json"
if [[ -f "$CJ" ]]; then
  email="$(jq -r '.oauthAccount.emailAddress // empty' "$CJ" 2>/dev/null || true)"
  if [[ -n "$email" ]]; then
    echo "구독 계정 확인: $email"
  else
    echo "경고: ~/.claude.json 에 oauthAccount.emailAddress 가 없습니다. NOTIFY_ACCOUNT 로 직접 지정하세요."
  fi
else
  echo "경고: ~/.claude.json 이 없습니다. Claude Code 로그인 후 다시 실행하세요."
fi
