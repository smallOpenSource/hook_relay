# hook_relay 사용자 설치 가이드 (Windows / macOS / Linux)

내 PC의 **Claude Code**가 작업을 끝내거나(Stop) 입력을 기다릴 때(Notification) **Slack / Telegram / Discord**로
알림을 받도록 설정하는 가이드입니다. OS 탭만 따라 복사·붙여넣기 하면 됩니다.

```
[내 PC] Claude Code 후크 ──HTTP──▶ 중계 서버(relay.example.com:20000) ──▶ 내 채널(Slack…)
```

- **중계 서버 API**: `http://relay.example.com:20000` (후크가 여기로 보냄)
- **관리 웹 UI**: `https://relay.example.com:20001` (매핑 추가·조회, 암호 접근)

---

## 0. 사전 준비 (필수)

1. **Claude Code 설치 + 로그인** — `~/.claude.json`(Windows는 `%USERPROFILE%\.claude.json`)이 있어야 합니다.
2. **서버 도달 가능** — 사내망/VPN 등. 아래로 확인:
   - Linux/macOS: `curl -fsS http://relay.example.com:20000/health` → `{"ok":true}`
   - Windows: `Invoke-RestMethod http://relay.example.com:20000/health`
3. **내 알림 매핑 등록** — `(app, username) → channel`. **이게 없으면 알림이 어디로 갈지 몰라 전송되지 않습니다(404).**
   → [1. 매핑 등록·확인](#1-매핑-등록확인-공통) 참고.
4. **필요 도구**
   - Windows: **PowerShell 5+** (윈도우 기본 내장 — 추가 설치 불필요)
   - macOS / Linux: **jq**, **curl**

---

## 1. 매핑 등록·확인 (공통)

알림을 받으려면 내 `username`이 채널과 연결돼 있어야 합니다. `username`은 자유롭게 정하면 되고(미지정 시 PC 사용자명),
설치 시 `NOTIFY_USER`로 같은 값을 씁니다.

**등록 방법 (둘 중 하나)**
- **웹 UI**: `https://relay.example.com:20001` 접속 → 로그인 모달에 **운영자에게 받은 암호** 입력 →
  "새 라우트 연결"에서 `app=slack`, `username=<나>`, `channel=#내채널` 추가.
  *(Slack은 봇이 그 채널에 초대돼 있어야 발송됩니다. Telegram=`chat_id`, Discord=`channel_id`.)*
- **운영자 요청**: `(app, username, channel)` 등록을 요청.

> 🛠 채널을 **처음 연결하는 운영자**라면, 먼저 [부록 A — 플랫폼별 봇 토큰 발급](#부록-a--플랫폼별-봇-토큰-발급-운영자용)에서
> 봇 토큰부터 발급·설정하세요(토큰은 사용자가 아니라 **서버**가 보관합니다).

**확인 (공개 조회 — 누구나 가능)**
```bash
curl -fsS "http://relay.example.com:20000/channels?app=slack&username=<나>"
# → {"channels":[{"app":"slack","username":"<나>","channel":"#내채널"}]}  이면 OK
# → {"channels":[]}  이면 아직 매핑 없음 (위에서 등록)
```

---

## 2. 설치 (OS 선택)

> 공통 3단계: **① 후크 스크립트 배치 → ② 설정 패치 → ③ 검증.**
> 아래 명령은 **`dist/` 폴더 안에서** 실행합니다. `<나>` 는 1번에서 정한 매핑 username으로 바꾸세요.

### 🪟 Windows (PowerShell)

```powershell
# dist 폴더에서 PowerShell 실행

# ① 후크 스크립트 배치
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\hooks" | Out-Null
Copy-Item .\claude-notify.ps1 "$env:USERPROFILE\.claude\hooks\claude-notify.ps1" -Force

# ② 설정 패치 (settings.json 에 Stop/Notification 후크 + env 주입)
$env:NOTIFY_API_URL = 'http://relay.example.com:20000'
$env:NOTIFY_APP     = 'slack'          # slack | telegram | discord
$env:NOTIFY_USER    = '<나>'
powershell -NoProfile -ExecutionPolicy Bypass -File .\patch-claude-config.ps1
```

### 🍎 macOS

```bash
# 사전: jq 없으면 설치
command -v jq >/dev/null || brew install jq

# ① 후크 스크립트 배치
mkdir -p ~/.claude/hooks
cp claude-notify-mac.sh ~/.claude/hooks/claude-notify-mac.sh
chmod +x ~/.claude/hooks/claude-notify-mac.sh

# ② 설정 패치
NOTIFY_API_URL='http://relay.example.com:20000' NOTIFY_APP='slack' NOTIFY_USER='<나>' \
  bash patch-claude-config-mac.sh
```

### 🐧 Linux

```bash
# 사전: jq 없으면 설치 (배포판에 맞게)
command -v jq >/dev/null || sudo dnf install -y jq    # 또는: sudo apt-get install -y jq

# ① 후크 스크립트 배치
mkdir -p ~/.claude/hooks
cp claude-notify.sh ~/.claude/hooks/claude-notify.sh
chmod +x ~/.claude/hooks/claude-notify.sh

# ② 설정 패치
NOTIFY_API_URL='http://relay.example.com:20000' NOTIFY_APP='slack' NOTIFY_USER='<나>' \
  bash patch-claude-config.sh
```

패치 스크립트는 `~/.claude/settings.json`을 **백업(`*.bak.<시각>`) 후** `.hooks.Stop`·`.hooks.Notification`과
`.env`(NOTIFY_API_URL/APP/USER)를 병합하고, 구독 계정(`~/.claude.json`의 이메일)을 확인합니다.

---

## 3. 검증 (③)

```bash
# settings.json 에 후크가 들어갔는지
jq '.hooks, .env' ~/.claude/settings.json          # Windows: Get-Content ... | ConvertFrom-Json

# (선택) 실제 발송 테스트 — 매핑돼 있으면 진짜 알림이 옵니다
curl -fsS -X POST http://relay.example.com:20000/notify -H 'Content-Type: application/json' \
  -d '{"app":"slack","username":"<나>","status":"task_complete","session_name":"setup-test"}'
```
Windows:
```powershell
Invoke-RestMethod -Method Post -Uri http://relay.example.com:20000/notify -ContentType 'application/json' `
  -Body '{"app":"slack","username":"<나>","status":"task_complete","session_name":"setup-test"}'
```

**실사용 확인**: Claude Code(CLI)에서 아무 작업이나 끝내면(Stop) 알림이 옵니다.
`/rename` 으로 지정한 세션 이름이 알림에 함께 표시됩니다.

---

## 4. 환경변수 레퍼런스

| 변수 | 필수 | 기본 | 설명 |
|---|---|---|---|
| `NOTIFY_API_URL` | ✅ | — | 중계 서버 베이스 URL. `http://relay.example.com:20000` (끝에 `/notify` 붙이지 말 것) |
| `NOTIFY_APP` | | `slack` | `slack` \| `telegram` \| `discord` |
| `NOTIFY_USER` | | PC 사용자명 | `(app,username)→channel` 매핑 키. **매핑에 등록한 값과 동일하게** |
| `NOTIFY_ACCOUNT` | | `~/.claude.json` 이메일 | 구독 계정 직접 지정(SSO 등으로 이메일이 비어있을 때) |
| `HOOK_PATH` | | OS별 기본 경로 | 후크 스크립트 위치를 바꿀 때만 |

---

## 5. 문제 해결

| 증상 | 원인·해결 |
|---|---|
| `/health` 실패 | 서버 미도달 — 사내망/VPN/방화벽 확인 |
| 알림이 전혀 안 옴 | ① 매핑 없음(404) → 1번에서 등록 ② `NOTIFY_USER`가 매핑과 다름 ③ (Slack) 봇이 채널에 미초대 |
| **작업완료(Stop)는 오는데 입력대기·선택지대기(Notification)는 안 옴** | 정상 — Notification 은 **CLI에서만** 발화합니다(VSCode/IDE 확장 버그). |
| `jq 가 필요합니다` | macOS `brew install jq` / Linux `sudo dnf install -y jq` (또는 apt) |
| Windows 실행 차단 | 명령에 `-ExecutionPolicy Bypass` 가 이미 포함돼 있습니다. 회사 정책상 막히면 관리자 문의 |
| 원래대로 되돌리기 | `~/.claude/settings.json.bak.<시각>` 를 `settings.json` 으로 복원 |

---

## 6. 제거

`~/.claude/settings.json` 의 `.hooks.Stop` / `.hooks.Notification` 항목을 지우거나, 설치 시 만들어진
`settings.json.bak.<시각>` 백업을 복원하면 됩니다. 후크 스크립트(`~/.claude/hooks/claude-notify*`)도 삭제 가능합니다.

## 부록 A — 플랫폼별 봇 토큰 발급 (운영자용)

> 봇 토큰은 **사용자가 아니라 중계 서버**가 보관합니다. **채널별이 아니라 플랫폼(앱)별로 1개**를 발급해
> 서버의 `hook_relay.env`(0600)에 넣습니다. 일반 사용자(후크 설치)는 토큰이 필요 없고 `NOTIFY_USER` 매핑만 하면 됩니다.
> 아래는 채널을 처음 연결하는 **운영자**용 절차입니다.

| 플랫폼 | env 키 | 토큰 형식 | 발급처 | 매핑 `channel` 값 | 필요 권한 |
|---|---|---|---|---|---|
| Slack | `SLACK_BOT_TOKEN` | `xoxb-…` | api.slack.com/apps | `#채널명` 또는 채널 ID | 봇 스코프 `chat:write` + 채널 초대 |
| Telegram | `TELEGRAM_BOT_TOKEN` | `123456:ABC…` | @BotFather | `chat_id` | 봇을 대화/그룹에 추가 |
| Discord | `DISCORD_BOT_TOKEN` | (긴 문자열) | discord.com/developers | `channel_id` | `View Channel` + `Send Messages` |

발급·변경 후에는 항상 서버에 반영: `hook_relay.env` 수정 → `systemctl --user restart hook_relay`.

### Slack (`SLACK_BOT_TOKEN`)

1. <https://api.slack.com/apps> → **Create New App**(From scratch) → 워크스페이스 선택.
2. 좌측 **OAuth & Permissions** → *Scopes* → **Bot Token Scopes** 에 **`chat:write`** 추가.
   *(봇을 초대하지 않은 공개 채널에도 보내려면 `chat:write.public` 추가.)*
3. 상단 **Install to Workspace** → 승인.
4. 다시 **OAuth & Permissions** 페이지의 **Bot User OAuth Token**(`xoxb-…`)을 복사 — **토큰은 여기서 확인**합니다.
   바로가기 URL: 앱 → OAuth & Permissions (`https://api.slack.com/apps/<APP_ID>/oauth`).
5. 발송할 채널에 봇 초대: 채널 입력창에서 `/invite @봇이름`.

**▶ Slack 토큰 "바로가기" 확인** — 별도의 *토큰 복사 버튼*은 없습니다(토큰은 비밀이라 **OAuth & Permissions 페이지에서만** 표시).
가장 빠른 길은 ① 위치 = OAuth & Permissions 페이지, ② **유효성 검증 = `auth.test` 한 줄**:

```bash
curl -X POST https://slack.com/api/auth.test -H "Authorization: Bearer xoxb-…"
# 유효: {"ok":true,"url":"https://<워크스페이스>.slack.com/","team":"…","user":"…","team_id":"…","user_id":"…","bot_id":"…"}
# 무효: {"ok":false,"error":"invalid_auth"}   ← 토큰 오류/스코프 부족
```

### Telegram (`TELEGRAM_BOT_TOKEN`)

1. 텔레그램에서 **@BotFather** → `/newbot` → 이름·username 입력 → 토큰(`123456:ABC…`) 발급.
2. 봇을 대상 그룹/채널에 추가(채널은 관리자 권한으로).
3. `chat_id` 확인: 봇에 메시지를 보낸 뒤 `curl "https://api.telegram.org/bot<토큰>/getUpdates"` → 응답의 `result[].message.chat.id`.
4. 매핑 `channel` = 그 `chat_id`. 토큰 확인: `curl "https://api.telegram.org/bot<토큰>/getMe"` → `{"ok":true,…}`.

### Discord (`DISCORD_BOT_TOKEN`)

1. <https://discord.com/developers/applications> → **New Application** → 좌측 **Bot** → **Reset Token** → 토큰 복사.
2. **OAuth2 → URL Generator**: scope `bot` + 권한 `View Channel`·`Send Messages` 체크 → 생성된 URL로 서버에 봇 초대.
3. 대상 채널의 `channel_id` 복사: Discord *설정 → 고급 → 개발자 모드* 켠 뒤 채널 우클릭 **ID 복사**.
4. 매핑 `channel` = `channel_id`.

### 토큰 보안

- 토큰은 **서버 `hook_relay.env`(0600)에만** 두고, 코드·문서·채팅에 붙여넣거나 커밋하지 마세요(`*.env`·`ssl/`는 `.gitignore`).
- 유출 시 즉시 재발급: **Slack** = OAuth & Permissions에서 *Reinstall/Rotate* · **Telegram** = BotFather `/revoke` · **Discord** = *Reset Token*.

**공식 문서**: [Quickstart](https://docs.slack.dev/quickstart/) · [Tokens](https://docs.slack.dev/authentication/tokens/) · [Token types](https://api.slack.com/authentication/token-types) · [auth.test](https://docs.slack.dev/reference/methods/auth.test/).

---

서버 운영·재배포는 [`README.md`](README.md), 전체 개요는 상위 [`../README.md`](../README.md) 를 참고하세요.
