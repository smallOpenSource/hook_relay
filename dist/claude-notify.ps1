# Claude Code hook -> hook_relay (slack / telegram / discord) — Windows PowerShell
#
# stdin : Claude Code hook JSON (Stop / SubagentStop / Notification)
# 의존  : PowerShell 5+ (Invoke-RestMethod, ConvertFrom-Json 내장)
#
# 환경변수
#   NOTIFY_API_URL  (필수) hook_relay 베이스 URL. 예: http://relay.example.com:20000
#                   (끝에 /notify 를 붙이지 말 것 — 스크립트가 붙인다)
#   NOTIFY_APP      (선택) slack | telegram | discord.  기본 slack.
#   NOTIFY_USER     (선택) (app,username)->channel 매핑 키.  기본 $env:USERNAME.
#   NOTIFY_ACCOUNT  (선택) 구독 계정 직접 지정. 미지정 시
#                   %USERPROFILE%\.claude.json 의 oauthAccount.emailAddress 사용.
#   NOTIFY_DEBUG    (선택) =1 이면 후크 원본 JSON 을 NOTIFY_DEBUG_LOG 에 적재
#                   (기본 %USERPROFILE%\.claude\logs\notify-debug.jsonl). 묵음 이벤트도 기록.
#   NOTIFY_DRYRUN   (선택) =1 이면 발송하지 않고 결정된 status 만 출력(테스트용).
#   NOTIFY_IDLE     (선택) =0 이면 유휴 '입력 대기'(idle_prompt) 알림만 끔(완료·선택지 대기는 유지). 미설정=발송.

$ErrorActionPreference = 'Stop'

$apiUrl = $env:NOTIFY_API_URL
if ([string]::IsNullOrEmpty($apiUrl)) { Write-Error 'NOTIFY_API_URL 환경변수가 필요합니다'; exit 1 }
$apiUrl  = $apiUrl.TrimEnd('/')
$app     = if ($env:NOTIFY_APP)  { $env:NOTIFY_APP }  else { 'slack' }
$userKey = if ($env:NOTIFY_USER) { $env:NOTIFY_USER } else { $env:USERNAME }

# -- hook stdin JSON 파싱 --
$raw  = [Console]::In.ReadToEnd()
$hook = $null
if (-not [string]::IsNullOrWhiteSpace($raw)) { try { $hook = $raw | ConvertFrom-Json } catch { $hook = $null } }

$sessionId      = if ($hook -and $hook.session_id)      { $hook.session_id }      else { '' }
$cwd            = if ($hook -and $hook.cwd)             { $hook.cwd }             else { '' }
$event          = if ($hook -and $hook.hook_event_name){ $hook.hook_event_name } else { '' }
$transcriptPath = if ($hook -and $hook.transcript_path){ $hook.transcript_path } else { '' }
$agentId        = if ($hook -and $hook.agent_id)       { [string]$hook.agent_id }   else { '' }   # 서브에이전트 컨텍스트에서만 채워짐
$agentType      = if ($hook -and $hook.agent_type)     { [string]$hook.agent_type } else { '' }
$stopActive     = if ($hook -and $hook.stop_hook_active) { [bool]$hook.stop_hook_active } else { $false }
$ntype          = if ($hook -and $hook.notification_type) { [string]$hook.notification_type } else { '' }

# -- 디버그 캡처(옵션): 묵음 포함 모든 이벤트의 원본 JSON 적재 --
if ($env:NOTIFY_DEBUG -eq '1') {
  $dbg = if ($env:NOTIFY_DEBUG_LOG) { $env:NOTIFY_DEBUG_LOG } else { Join-Path $env:USERPROFILE '.claude\logs\notify-debug.jsonl' }
  try { New-Item -ItemType Directory -Force -Path (Split-Path $dbg) | Out-Null; Add-Content -Path $dbg -Value $raw } catch {}
}

# -- OMC 팀 워커 세션(orchestration 내부 워커)은 사용자에게 알리지 않음 (메인 완료엔 영향 없음) --
if ($env:OMC_TEAM_WORKER -or $env:OMX_TEAM_WORKER) { exit 0 }

# -- OMC orchestration 세션(ralph/autopilot 등)은 사용자에게 알리지 않음 --
#    cwd 위쪽 .omc/state/sessions/<sessionId>/ 에 활성 모드 상태파일이 있으면 orchestration 중 → 묵음.
if ($sessionId) {
  $d = $cwd
  while ($d) {
    if (Test-Path (Join-Path $d '.omc')) {
      $sdir = Join-Path (Join-Path (Join-Path (Join-Path $d '.omc') 'state') 'sessions') $sessionId
      $hit = $false
      foreach ($m in @('ralph','autopilot','ultrawork','ultrapilot','pipeline','autoresearch','self-improve','ultraqa','team','omc-teams')) {
        if (Test-Path (Join-Path $sdir "$m-state.json")) { $hit = $true; break }
      }
      if (-not $hit -and (Test-Path (Join-Path $sdir 'boulder.json'))) { $hit = $true }
      if ($hit) { exit 0 }
      break
    }
    $parent = Split-Path $d -Parent
    if (-not $parent -or $parent -eq $d) { break }
    $d = $parent
  }
}

# -- 상태 매핑: "메인 세션의 실제 상태"만 보고 --
#   Stop         : 메인 세션 최종 완료만 알림 (agent_id/agent_type 있으면 서브·팀원 → 묵음,
#                  stop_hook_active=true 면 자율 루프 진행중 → 묵음)
#   SubagentStop : 서브에이전트 완료 → 항상 묵음
#   Notification : elicitation_dialog/permission_prompt → 선택지 대기, idle_prompt → 입력 대기, 그 외 묵음
$status = $null
switch ($event) {
  'Stop' {
    if ($agentId -or $agentType) { exit 0 }
    if ($stopActive)             { exit 0 }
    $status = 'task_complete'
  }
  'SubagentStop' { exit 0 }
  'Notification' {
    switch ($ntype) {
      'elicitation_dialog' { $status = 'awaiting_choice' }
      'permission_prompt'  { $status = 'awaiting_choice' }
      'idle_prompt'        { if ($env:NOTIFY_IDLE -eq '0') { exit 0 }; $status = 'awaiting_input' }
      default              { exit 0 }
    }
  }
  default { exit 0 }
}

# -- 세션 이름: /rename 값(transcript) > 캐시 > session_id --
$sessionName = ''
if ($transcriptPath -and (Test-Path $transcriptPath)) {
  foreach ($ln in [System.IO.File]::ReadLines($transcriptPath)) {
    if ($ln -notlike '*Session renamed to:*') { continue }
    try { $o = $ln | ConvertFrom-Json } catch { continue }
    if ($o.type -eq 'system' -and $o.content -match 'Session renamed to: (.+?)(?:<|$)') {
      $sessionName = $matches[1].Trim()
    }
  }
}
if (-not $sessionName -and $cwd) {
  $nameCache = Join-Path $cwd '.claude/session-name'
  if (Test-Path $nameCache) { $sessionName = (Get-Content -Raw $nameCache).Trim() }
}
if (-not $sessionName) { $sessionName = $sessionId }

# -- 구독 계정 --
$account = $env:NOTIFY_ACCOUNT
if ([string]::IsNullOrEmpty($account)) {
  $cj = Join-Path $env:USERPROFILE '.claude.json'
  if (Test-Path $cj) {
    try { $account = (Get-Content -Raw $cj | ConvertFrom-Json).oauthAccount.emailAddress } catch { $account = '' }
  }
}
if ($null -eq $account) { $account = '' }

# -- DRYRUN: 발송 대신 결정 결과만 출력 --
if ($env:NOTIFY_DRYRUN -eq '1') {
  Write-Host "SEND status=$status event=$event ntype=$ntype app=$app user=$userKey session=$sessionName"
  exit 0
}

# -- 페이로드 구성 + 발송 --
$payload = @{
  app            = $app
  username       = $userKey
  status         = $status
  session_name   = $sessionName
  project_path   = $cwd
  time           = (Get-Date -Format o)
  hostname       = $env:COMPUTERNAME
  claude_account = $account
} | ConvertTo-Json -Compress

Invoke-RestMethod -Method Post -Uri "$apiUrl/notify" -ContentType 'application/json' -Body $payload | Out-Null
