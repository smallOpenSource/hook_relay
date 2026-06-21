# hook_relay

Claude Code 후크 이벤트(Stop / Notification)를 **Slack / Telegram / Discord** 알림으로 중계하는 경량 FastAPI 서비스.
호출 측은 `app`+`username`만 보내고, 서버가 `(app,username) → channel`로 분기해 발송한다.
**모든 설정값(host·port·인터프리터·토큰·CSV·TLS 인증서)은 코드 하드코딩 없이 `hook_relay.env`로만 관리**한다.

> 이 문서는 **현재 운영 배포본**(이 머신)의 운영 가이드다.
> 다른 OS로의 완전 재배포·클라이언트 후크 설치는 [`dist/README.md`](dist/README.md)를, 전체 작업 핸드오프는
> `/.remember/remember.md`를 본다.

---

## 아키텍처

- 루트(`/app/hook_relay`) = **런타임**, `dist/` = **완전 배포판**(어느 OS로든 가져가 배포).
- 동일한 `app.py`를 **역할(role) 2개**로 기동한다 — 실행 래퍼 `run_hook_relay.sh <api|web>`가 env에서
  `HOST`·역할별 포트·인터프리터(`UVICORN_PYTHON`)·TLS를 읽어 uvicorn에 바인딩한다.

| 서비스(systemd --user) | 역할 | 포트 | 프로토콜 | 용도 |
|---|---|---|---|---|
| `hook_relay` | `api` | `20000` | **HTTP(평문)** | 후크 수신·발송·채널 CRUD/조회 |
| `hook_relay-web` | `web` | `20001` | **HTTPS(TLS)** | 웹 UI(암호 접근) — PWA·서비스워커 |

- **web 역할만** `SSL_CERTFILE`/`SSL_KEYFILE`가 설정·존재하면 TLS로 서빙한다. **api 역할은 평문 유지**
  → 클라이언트 후크(`http://<서버>:20000`)가 무중단으로 동작한다.

## 디렉터리

```
/app/hook_relay/                  ← 런타임(이 디렉터리)
├─ app.py                  FastAPI 앱: 백엔드 + 웹 UI + PWA + favicon 임베드(단일 파일)
├─ run_hook_relay.sh       실행 래퍼: 역할(api|web) → HOST:PORT, web는 TLS 주입
├─ hook_relay.env          환경변수(0600, 비밀 — 버전관리 제외)
├─ channels.csv            (app,username,channel) 매핑
├─ ssl/<도메인>/           TLS 인증서 fullchain/privkey…(비밀 — 버전관리 제외)
└─ dist/                   완전 배포판(OS별 setup/run + 클라 hook/patch + env.example + README)
```

## 엔드포인트

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| `POST` | `/notify` | 공개 | `{app, username, status, session_name, …}` → 라우팅 발송(매핑 없으면 404) |
| `GET` | `/channels?app=&username=` | 공개(api) | 매핑 조회 |
| `POST` | `/channels` | 쓰기 | `{app, username, channel}` upsert |
| `DELETE` | `/channels` | 쓰기 | `{app, username}` 삭제 |
| `GET` | `/health` | 공개 | `{"ok": true}` |
| `GET` | `/` | 공개 | 웹 UI 셸(데이터 로드는 인증) |
| `GET` | `/manifest.webmanifest` · `/sw.js` · `/favicon.svg` · `/favicon.ico` · `/icon-*.png` | 공개 | PWA 자산·파비콘 |

**인증 모델**
- **api 포트(20000)**: 쓰기(`POST`/`DELETE /channels`)는 `Authorization: Bearer <DATA_ADMIN_TOKEN>`. 조회는 공개.
- **web 포트(20001)**: 미들웨어가 위 "공개" 자산을 제외한 전 경로에 암호를 요구한다(`WEB_PASSWORD`, 비우면
  `DATA_ADMIN_TOKEN`). 브라우저는 **앱 내 로그인 모달**로, 프로그램은 **HTTP Basic**(`curl -u x:<암호>`)으로
  접근한다. 인증 후 CRUD 전부 허용.

## 환경변수 (`hook_relay.env`, 0600)

값은 비밀이므로 여기 적지 않는다 — 키 목록과 의미만. 템플릿은 [`dist/hook_relay.env.example`](dist/hook_relay.env.example) 참고.

| 키 | 의미 |
|---|---|
| `HOST` / `APP_PORT` / `UI_PORT` | 바인드 주소 · api 포트(20000) · web 포트(20001) |
| `UVICORN_PYTHON` | uvicorn 구동 인터프리터 경로(비우면 `python3`) |
| `CHANNELS_CSV` | 매핑 CSV 경로 |
| `DATA_ADMIN_TOKEN` | 채널 쓰기 Bearer 토큰(= web 암호 폴백). **강한 랜덤 권장** |
| `WEB_PASSWORD` | 웹 UI 접근 암호(비우면 `DATA_ADMIN_TOKEN`) |
| `SSL_CERTFILE` / `SSL_KEYFILE` | 둘 다 설정 시 **web 역할만 HTTPS**. `fullchain.pem` + `privkey.pem` |
| `SLACK_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` | 쓰는 앱만. 발급법: [`dist/GUIDE.md` 부록 A](dist/GUIDE.md) |
| `SLACK_API_URL` / `TELEGRAM_API_BASE` / `DISCORD_API_BASE` | 선택 오버라이드(프록시/테스트) |

## 운영

```bash
# 상태 / 재시작 (설정값 변경 후)
systemctl --user status hook_relay hook_relay-web
systemctl --user restart hook_relay hook_relay-web

# 로그 (영구저장 꺼져 있으면 status 로 최근 줄 확인)
systemctl --user status hook_relay-web --no-pager -n 20

# 방화벽
sudo firewall-cmd --permanent --add-port=20000/tcp --add-port=20001/tcp && sudo firewall-cmd --reload
```

**TLS 인증서 갱신** — `ssl/<도메인>/`의 파일은 certbot/ZeroSSL 레이아웃이므로 **이동·개명 금지**(갱신 호환).
갱신 후 web만 재기동하면 반영된다:

```bash
systemctl --user restart hook_relay-web
# 확인
curl -s https://<도메인>:20001/health        # 200 + 인증서 체인 검증
echo | openssl s_client -connect <도메인>:20001 -servername <도메인> 2>/dev/null | openssl x509 -noout -subject -dates
```

> 인증서 만료 시 **웹 UI·PWA만 중단**되고 api·클라이언트 후크는 영향이 없다.

## 웹 UI (HTTPS, 20001)

- **2축 테마**: `amber`(웜 콘솔) ⇄ `nothing`(모노크롬) × `dark` ⇄ `light` — HUD 토글 2개, `localStorage` 영속,
  OS 설정 추종 안 함.
- **로그인 모달**: 브라우저 기본 Basic 창 대신 앱 내 모달. `curl -u` 등 프로그램 Basic 접근은 그대로 호환.
- **인라인 CRUD**: 각 행의 "수정"으로 그 자리에서 채널 편집, 하단 패널은 신규 추가.
- **PWA**: manifest + 서비스워커(셸 캐시·오프라인) + 아이콘. HTTPS(또는 localhost) 보안 컨텍스트에서 설치 가능.
- **파비콘**: ⇋ 릴레이 모티프 — `/favicon.svg`(주) + `/favicon.ico`(폴백).
- **테마 설계·검증 노트**: 16 디자인 × dark/light 테마 시스템 · 마크(타이틀) 가독성 규칙 · HUD 레이아웃
  주의(956px 경계) · **라이브 검증 방법론(테마 전환은 반드시 실제 토글 클릭으로 등)** 은
  [`docs/ui-theming-and-verification.md`](docs/ui-theming-and-verification.md) 참고.

## 데이터 (`channels.csv`)

컬럼 `app,username,channel`:
- **slack**: `#채널명` 또는 채널 ID (봇이 채널에 초대돼 있어야)
- **telegram**: `chat_id` · **discord**: `channel_id` (봇 접근·전송 권한 필요)

> 실 매핑 파일 `channels.csv`는 개인 `chat_id` 등을 담아 **`.gitignore`로 제외**된다.
> 클론 후 `cp channels.csv.example channels.csv` 로 만들어 실값을 채운다.

> 발송측 봇 토큰·채널 형식과 발급 함정(텔레그램 chat_id·프라이버시 모드, 디스코드 권한, 메시지 포맷 `build_text`)
> 및 **메시지 발송 없이·토큰 미출력으로 검증하는 레시피**는 [`docs/messenger-setup-and-verification.md`](docs/messenger-setup-and-verification.md) 참고.

## 클라이언트(후크) 설치

대상 머신에서 `dist/`의 스크립트로 설치한다 — **OS별 단계별 사용자 가이드: [`dist/GUIDE.md`](dist/GUIDE.md)**(Windows/macOS/Linux 복사·붙여넣기), 재배포 상세: [`dist/README.md`](dist/README.md). 요지:

```bash
# Linux 예시
cp dist/claude-notify.sh ~/.claude/hooks/ && chmod +x ~/.claude/hooks/claude-notify.sh
NOTIFY_API_URL='http://<서버>:20000' NOTIFY_APP='slack' NOTIFY_USER='<사용자>' bash dist/patch-claude-config.sh
```

후크는 `transcript_path`에서 `/rename` 세션 이름을 추출해 `session: <이름>`으로 함께 보낸다(없으면 세션 ID).

> 후크가 **메인 세션 vs 서브에이전트/팀원**·**완료 vs 선택지/입력 대기**를 어떻게 구분해 `status`를 정하는지(+진단·무발송 검증)는 [`docs/notification-semantics-and-hook-events.md`](docs/notification-semantics-and-hook-events.md) 참고.

## 보안 주의

- **비밀값은 `hook_relay.env`로만**(코드 하드코딩 금지). `hook_relay.env`·`channels.csv`·`ssl/`·`.remember/`·`.omc/`·`.ruff_cache/`·`work/`는
  `.gitignore`로 버전관리에서 제외된다 — 토큰·개인키·개인 chat_id를 커밋하지 말 것.
  **공개 발행 정책·커밋 전 비밀 게이트**는 [`docs/publishing-and-secret-hygiene.md`](docs/publishing-and-secret-hygiene.md) 참고.
- `DATA_ADMIN_TOKEN`/`WEB_PASSWORD`는 강한 랜덤으로 운용하고, 포트는 방화벽으로 소스를 제한하기를 권장한다.
- 웹 로그인은 **브루트포스 throttle**로 보호된다(틀린 암호 누적 시 429, 정답은 즉시 통과). 인증·throttle **설계 근거·결정 기록·라이브 검증 방법**은 [`docs/security-and-verification.md`](docs/security-and-verification.md) 참고.
- 가짜·목업 데이터 금지.
