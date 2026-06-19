# Claude Code hook -> hook_relay (slack / telegram / discord) — Windows PowerShell
#
# stdin : Claude Code hook JSON (Stop / Notification)
# 의존  : PowerShell 5+ (Invoke-RestMethod, ConvertFrom-Json 내장)
#
# 환경변수
#   NOTIFY_API_URL  (필수) hook_relay 베이스 URL. 예: http://relay.example.com:20000
#                   (끝에 /notify 를 붙이지 말 것 — 스크립트가 붙인다)
#   NOTIFY_APP      (선택) slack | telegram | discord.  기본 slack.
#   NOTIFY_USER     (선택) (app,username)->channel 매핑 키.  기본 $env:USERNAME.
#   NOTIFY_ACCOUNT  (선택) 구독 계정 직접 지정. 미지정 시
#                   %USERPROFILE%\.claude.json 의 oauthAccount.emailAddress 사용.

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

$sessionId = if ($hook -and $hook.session_id)      { $hook.session_id }      else { '' }
$cwd       = if ($hook -and $hook.cwd)             { $hook.cwd }             else { '' }
$event     = if ($hook -and $hook.hook_event_name) { $hook.hook_event_name } else { '' }
$transcriptPath = if ($hook -and $hook.transcript_path) { $hook.transcript_path } else { '' }

# -- 상태 매핑 --
switch ($event) {
  'Stop'         { $status = 'task_complete' }
  'Notification' { $status = 'awaiting_input' }
  default        { $status = $event }
}

# -- 세션 이름: /rename 값(transcript) > 캐시 > session_id --
$sessionName = ''
if ($transcriptPath -and (Test-Path $transcriptPath)) {
  foreach ($ln in [System.IO.File]::ReadLines($transcriptPath)) {
    if ($ln -notlike '*local_command*') { continue }
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
