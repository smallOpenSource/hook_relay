# hook_relay

Claude Code 후크 이벤트를 **Slack / Telegram / Discord** 알림으로 중계하는 경량 서비스.

호출 측은 `app`+`username`만 보내고, 서버가 `(app,username)→channel`로 분기해 발송한다.
**모든 설정값(host·port·인터프리터·토큰·CSV경로)은 코드 하드코딩 없이 `hook_relay.env`로만 관리**한다.

## 배포 구조

```
dist/                      ← 완전한 배포판 (어느 OS로든 가져가 배포)
  app.py                   FastAPI 릴레이 서버(공용)
  run_hook_relay.sh        Linux 실행 래퍼   (역할: api|web → HOST:PORT)
  run_hook_relay-mac.sh    macOS 실행 래퍼
  run_hook_relay.bat       Windows 실행 래퍼
  setup_hook_relay.sh      Linux 설치 (systemd user service 2종)
  setup_hook_relay-mac.sh  macOS 설치 (launchd LaunchAgent 2종)
  setup_hook_relay.bat     Windows 설치 (예약작업 2종)
  claude-notify.sh/.ps1/-mac.sh        클라이언트 후크 (Linux/Win/macOS)
  patch-claude-config.sh/.ps1/-mac.sh  클라이언트 설치 패처
  hook_relay.env.example   환경변수 템플릿
  channels.csv             CSV 템플릿(헤더만)
  README.md                이 문서

런타임(설치 후 APP_DIR, 기본 /app/hook_relay):
  app.py · run_hook_relay.sh · hook_relay.env(0600) · channels.csv
```

- **20000**: API/hook 수신·발송·채널 CRUD/조회 — service `hook_relay` (역할 `api`)
- **20001**: 같은 `app.py`를 다른 포트로 기동, 웹 UI(same-origin) — service `hook_relay-web` (역할 `web`)
- 두 서비스는 **실행 래퍼**(`run_hook_relay*`)를 `api`/`web` 역할로 호출하고, 래퍼가 env에서 `HOST`와
  역할별 포트(`APP_PORT`/`UI_PORT`)·인터프리터(`UVICORN_PYTHON`)를 읽어 uvicorn을 바인딩한다.

## 환경변수 (`hook_relay.env`, 0600)

| 변수 | 기본 | 설명 |
|------|------|------|
| `HOST` | `0.0.0.0` | 바인드 주소. 외부 노출 막으려면 `127.0.0.1` |
| `APP_PORT` | `20000` | API/hook 포트 (역할 api) |
| `UI_PORT` | `20001` | 웹 UI 포트 (역할 web) |
| `UVICORN_PYTHON` | (비면 python3) | uvicorn 구동 인터프리터 경로 |
| `CHANNELS_CSV` | `channels.csv` | 매핑 CSV 경로 |
| `DATA_ADMIN_TOKEN` | — | API(20000) 채널 쓰기용 Bearer 토큰. 미설정 시 CRUD 500. **강한 랜덤 권장** |
| `WEB_PASSWORD` | (비면 `DATA_ADMIN_TOKEN`) | 웹 UI(20001) 접근 암호(HTTP Basic). 인증 시 CRUD 전부 허용 |
| `SSL_CERTFILE` / `SSL_KEYFILE` | — | 둘 다 설정 시 **web 역할(20001)만 HTTPS 서빙**. `fullchain.pem`+`privkey.pem`. api(20000)는 평문 유지 |
| `SLACK_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` | — | 쓰는 앱만 |
| `SLACK_API_URL` / `TELEGRAM_API_BASE` / `DISCORD_API_BASE` | 공식 URL | 선택 오버라이드(프록시/테스트) |

> 비밀값은 코드에 하드코딩 금지 — env 파일로만. 가짜·목업 데이터 금지.

## 서버 설치

```bash
# Linux (systemd) — dist/ 안에서
SLACK_BOT_TOKEN='xoxb-...' DATA_ADMIN_TOKEN='정한값' bash setup_hook_relay.sh
# macOS (launchd)
SLACK_BOT_TOKEN='xoxb-...' DATA_ADMIN_TOKEN='정한값' bash setup_hook_relay-mac.sh
# Windows (예약작업) — set 으로 토큰 지정 후
setup_hook_relay.bat
```

설정값 변경: `hook_relay.env` 직접 수정 후 재시작.

```bash
# Linux
systemctl --user restart hook_relay hook_relay-web
```

방화벽(Linux): `sudo firewall-cmd --permanent --add-port=20000/tcp --add-port=20001/tcp && sudo firewall-cmd --reload`

### HTTPS (웹 UI TLS) — PWA 설치/서비스워커에 필요

서비스워커 등록·PWA 설치는 **보안 컨텍스트(HTTPS 또는 localhost)** 를 요구한다. 공개 도메인으로 웹 UI를
노출하려면 web 역할(20001)에 TLS 를 입힌다.

```bash
# 인증서/키 경로를 env 에 지정(파일은 제자리 참조 — 이동·개명 금지, certbot 갱신 호환)
#   SSL_CERTFILE=/app/hook_relay/ssl/<도메인>/fullchain.pem
#   SSL_KEYFILE=/app/hook_relay/ssl/<도메인>/privkey.pem
systemctl --user restart hook_relay-web      # web 만 HTTPS 로 재기동(api 는 평문 유지)
```

- 래퍼(`run_hook_relay.sh web`)가 `SSL_CERTFILE`+`SSL_KEYFILE` 가 설정·존재하면 uvicorn 에
  `--ssl-certfile/--ssl-keyfile` 를 붙인다. **web 역할에서만** 적용 → 후크 수신 api(20000)는 평문 유지.
- SSH 터널로 노출 시: 원격 `https://<도메인>:20001` → 터널 → 로컬 TLS uvicorn. 인증서 CN/SAN 이 그 도메인과
  일치해야 브라우저가 신뢰한다.
- 파비콘은 `/favicon.svg`(주)·`/favicon.ico`(폴백)로 무인증 제공된다.

### 엔드포인트 (APP_PORT)

| 메서드 | 경로 | 인증 | 설명 |
|--------|------|------|------|
| `POST` | `/notify` | 공개 | `{app, username, status, session_name, project_path, hostname, time, claude_account}` → 라우팅 발송. 매핑 없으면 404 |
| `GET` | `/channels?app=&username=` | 공개 | 매핑 조회 |
| `POST` | `/channels` | Bearer | `{app, username, channel}` upsert |
| `DELETE` | `/channels` | Bearer | `{app, username}` 삭제 |
| `GET` | `/health` | 공개 | `{"ok": true}` |
| `GET` | `/` | 공개 | 웹 UI(UI_PORT에서 사용) |

### 데이터 (CSV) — `channels.csv`

컬럼 `app,username,channel`:
- **slack**: `#채널명` 또는 채널 ID (봇이 채널에 초대돼 있어야)
- **telegram**: `chat_id` (봇이 그 대화/그룹에 있어야)
- **discord**: `channel_id` (봇이 접근·전송 권한)

### 웹 UI (UI_PORT) — 암호 접근

`http://<서버>:<UI_PORT>` 접속 시 **HTTP Basic 암호**(`WEB_PASSWORD`, 비우면 `DATA_ADMIN_TOKEN`)를 요구한다.
사용자명은 아무거나, 암호만 일치하면 됨. 인증 후 **조회·추가·수정·삭제(CRUD 전부)** 가능.
(API 20000 의 `GET /channels` 공개 조회 규약은 그대로 — 웹 포트만 암호 게이트.)

## 클라이언트(후크) 설치

> 👉 **OS별 단계별 사용자 가이드: [`GUIDE.md`](GUIDE.md)** — Windows/macOS/Linux 복사·붙여넣기 + 검증·문제해결.

대상 머신에서:

```bash
# Linux
cp claude-notify.sh ~/.claude/hooks/claude-notify.sh && chmod +x ~/.claude/hooks/claude-notify.sh
NOTIFY_API_URL='http://<서버>:20000' NOTIFY_APP='slack' NOTIFY_USER='minsu' bash patch-claude-config.sh
# macOS: claude-notify-mac.sh + patch-claude-config-mac.sh
# Windows: claude-notify.ps1 + patch-claude-config.ps1
```

### 후크 환경변수

| 변수 | 필수 | 설명 |
|------|------|------|
| `NOTIFY_API_URL` | ✅ | hook_relay 베이스 URL (끝에 `/notify` 붙이지 말 것) |
| `NOTIFY_APP` | | `slack`\|`telegram`\|`discord`. 기본 `slack` |
| `NOTIFY_USER` | | `(app,username)→channel` 매핑 키. 기본 호스트 사용자명 |
| `NOTIFY_ACCOUNT` | | 구독 계정 직접 지정. 미지정 시 `~/.claude.json`의 oauthAccount 이메일 |

> **제약**: Notification 알림은 CLI에서만 발화한다(VSCode/IDE 확장 버그). Stop은 양쪽 OK.

## 상태 매핑

| 후크 이벤트 | 조건 | 상태 | 표시 |
|-------------|------|------|------|
| `Stop` | 메인 세션 최종 완료(아래 제외) | `task_complete` | ✅ 작업 완료 |
| `Stop` | `agent_id` 있음(서브에이전트·팀원) | — | (발송 안 함) |
| `Stop` | `stop_hook_active=true`(자율 루프 진행 중) | — | (발송 안 함) |
| `SubagentStop` | 서브에이전트 완료 | — | (발송 안 함) |
| `Notification` | `elicitation_dialog`·`permission_prompt`(사용자 결정 대기) | `awaiting_choice` | ❓ 선택지 대기 |
| `Notification` | `idle_prompt`(유휴) | `awaiting_input` | ⌨️ 입력 대기 |

> 서브에이전트/팀원 완료와 자율 루프(autopilot/ralph 등) 중간 `Stop`은 **메인 세션의 완료가 아니므로 보내지 않는다**. 유휴(`idle_prompt`)는 "입력 대기", 실제 사용자 결정 대기(plan 승인·권한 `elicitation_dialog`/`permission_prompt`)는 "선택지 대기"로 구분된다.

> **세션 이름**: 후크는 `transcript_path` 에서 `/rename` 으로 지정한 세션 이름을 추출해 `session: <이름>` 으로 전송한다(없으면 세션 ID 사용).
