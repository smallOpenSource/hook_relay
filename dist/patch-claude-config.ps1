# Claude Code 설정 패치 (Windows PowerShell) — settings.json 병합 (vi 미사용)
#
# 실행:
#   $env:NOTIFY_API_URL='http://relay.example.com:20000'
#   $env:NOTIFY_APP='slack'; $env:NOTIFY_USER='minsu'
#   powershell -NoProfile -ExecutionPolicy Bypass -File patch-claude-config.ps1
#
# 선택 환경변수:
#   HOOK_PATH    hook 스크립트 경로 (기본: %USERPROFILE%\.claude\hooks\claude-notify.ps1)
#   NOTIFY_APP   slack|telegram|discord (settings.json env 에 주입; 미지정 시 생략)
#   NOTIFY_USER  (app,username)->channel 매핑 키 (미지정 시 생략)
#
# 제약: Notification 알림은 CLI 에서만 발화한다(VSCode/IDE 확장 버그). Stop 은 양쪽 OK.

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrEmpty($env:NOTIFY_API_URL)) { Write-Error 'NOTIFY_API_URL 환경변수가 필요합니다'; exit 1 }

$hookPath = if ($env:HOOK_PATH) { $env:HOOK_PATH } else { Join-Path $env:USERPROFILE '.claude\hooks\claude-notify.ps1' }
$settings = Join-Path $env:USERPROFILE '.claude\settings.json'

New-Item -ItemType Directory -Force -Path (Split-Path $settings) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $hookPath) | Out-Null
if (-not (Test-Path $settings)) { '{}' | Set-Content -Encoding UTF8 $settings }

# 백업
Copy-Item $settings ("{0}.bak.{1}" -f $settings, [int][double]::Parse((Get-Date -UFormat %s)))

# 로드 (해시테이블로 다뤄 병합 단순화)
$json = Get-Content -Raw $settings
$cfg  = if ([string]::IsNullOrWhiteSpace($json)) { @{} } else { $json | ConvertFrom-Json -AsHashtable }
if ($null -eq $cfg) { $cfg = @{} }

# .env 병합
if (-not $cfg.ContainsKey('env') -or $null -eq $cfg['env']) { $cfg['env'] = @{} }
$cfg['env']['NOTIFY_API_URL'] = $env:NOTIFY_API_URL
if ($env:NOTIFY_APP)  { $cfg['env']['NOTIFY_APP']  = $env:NOTIFY_APP }
if ($env:NOTIFY_USER) { $cfg['env']['NOTIFY_USER'] = $env:NOTIFY_USER }

# hooks (command: powershell 로 .ps1 실행)
$cmd  = 'powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $hookPath
$cfg['hooks'] = @{
  Stop         = @(@{ hooks = @(@{ type = 'command'; command = $cmd; async = $true }) })
  Notification = @(@{ matcher = 'idle_prompt|permission_prompt'; hooks = @(@{ type = 'command'; command = $cmd; async = $true }) })
}

$cfg | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 $settings
Write-Host "패치 완료: $settings"

if (Test-Path $hookPath) {
  Write-Host "hook 확인: $hookPath"
} else {
  Write-Host "참고: $hookPath 에 claude-notify.ps1 를 배치하세요."
}

# 구독 계정 검증
$cj = Join-Path $env:USERPROFILE '.claude.json'
if (Test-Path $cj) {
  $email = ''
  try { $email = (Get-Content -Raw $cj | ConvertFrom-Json).oauthAccount.emailAddress } catch { $email = '' }
  if ($email) { Write-Host "구독 계정 확인: $email" }
  else { Write-Host "경고: .claude.json 에 oauthAccount.emailAddress 가 없습니다. NOTIFY_ACCOUNT 로 직접 지정하세요." }
} else {
  Write-Host "경고: %USERPROFILE%\.claude.json 이 없습니다. Claude Code 로그인 후 다시 실행하세요."
}
