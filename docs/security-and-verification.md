# 보안 설계 · 결정 기록 · 라이브 검증 방법론

> 운영 개요·엔드포인트·env는 [`../README.md`](../README.md), 사용자 설치·봇 토큰 발급은 [`../dist/GUIDE.md`](../dist/GUIDE.md).
> 이 문서는 **왜 그렇게 설계했는지**(rationale)·**확정된 결정**·**이 서비스를 어떻게 라이브로 검증하는지**를 담는다.
> 코드 라인 번호는 drift하므로 **함수명으로 grep**해서 찾을 것(아래 `app.py` 함수명 기준).

---

## 1. 인증 모델 (요약)

`app.py` 한 파일을 역할 2개로 기동한다(`run_hook_relay.sh <api|web>` → `HOOK_RELAY_ROLE` export).

- **web(20001, HTTPS)**: `_web_gate` 미들웨어(`HOOK_RELAY_ROLE == "web"` 일 때만 동작)가 공개 자산
  (`/`·`/health`·PWA·favicon·`/client/`)을 제외한 전 경로에 HTTP Basic 암호를 요구한다(`_web_password_ok`).
  통과하면 `request.state.web_authed = True`.
- **api(20000, 평문)**: `_web_gate`는 no-op(역할≠web). 쓰기(`POST`/`DELETE /channels`)는 `require_admin`이
  `Authorization: Bearer <DATA_ADMIN_TOKEN>`를 요구. 조회(`GET /channels`)·`/notify`는 공개.
- `require_admin`은 **web Basic을 이미 통과한 요청**(`web_authed`)이면 Bearer 검사를 건너뛴다 → 웹 UI는
  자기 오리진(20001) 상대경로로 CRUD를 모두 수행한다.

비밀은 `hook_relay.env`(0600)에만. 약한 기본 토큰을 쓰고 있어(값은 문서에 적지 않는다) 아래 2장 보호가 필요하다.

---

## 2. 브루트포스 보호 (2026-06-18, 코드 레벨)

약한 비밀을 **교체 없이** 무력화하기 위한 인메모리 throttle. 함수: `_auth_recent_fails` / `_auth_note_fail` /
`_auth_clear` + `_web_gate` 내 분기. 튜너블 env(비밀 아님, 기본값 내장): `AUTH_FAIL_LIMIT`(15) ·
`AUTH_FAIL_WINDOW`(300s) · `AUTH_FAIL_DELAY`(1s).

동작: web 비공개 경로에서 `_web_password_ok`를 **먼저** 검사 →
- **정답**: `_auth_clear(ip)` 후 통과.
- **틀린 Basic 암호**: 실패 타임스탬프 기록. 윈도 내 실패 ≥ LIMIT면 이후 오답에 `await asyncio.sleep(DELAY)` + **429**(Retry-After), 아니면 401.
- **무자격**(Authorization 없음/Basic 아님): 그냥 401, **미카운트**.

### 설계 근거 (비자명 — 반드시 보존)

| 결정 | 이유 |
|---|---|
| **정답을 먼저 검사 → 항상 즉시 통과** | 정상 사용자는 throttle 상태와 무관하게 절대 잠기지 않는다. SSH 터널 때문에 web 클라 IP가 대개 `127.0.0.1`(공유 키)이라, 공격자가 그 키를 throttle시켜도 **정답 아는 사용자는 막히면 안 되기** 때문. (이 한 줄이 lockout 방지의 핵심.) |
| **틀린 Basic만 카운트**(무자격 미카운트) | 페이지 로드 시 무자격 `/channels` → 401(모달 트리거)이 **정상 동작**이다. 이걸 세면 평범한 새로고침이 사용자를 throttle시킨다. |
| **api(role≠web) 미적용 + `async` sleep** | `/notify`(후크 수신)는 **별 프로세스(api)**라 `_web_gate` 자체가 안 돈다. 또 `asyncio.sleep`은 이벤트 루프를 블록하지 않으므로, 동기 `time.sleep`로 스레드풀을 고갈시켜 `/notify`(같은 워커의 공개 경로)를 간접 마비시키는 일을 피한다. **후크 가용성 > throttle 강도.** |
| **항상 append + 리스트 길이 캡**(throttled 시에도 기록, 단 `LIMIT*4` 상한) | "throttled면 미append" 대안보다 우월: 지속 공격 중 최근 타임스탬프가 계속 갱신돼 **영구 throttle**가 유지되고(미append는 윈도가 비면서 주기적으로 풀려 시도 누수), 동시에 리스트가 무제한 증가하지 않는다(메모리 상한). |
| 키 dict `AUTH_MAX_KEYS=4096` 캡 + 가장 오래된 키 eviction | IP 스푸핑/대량 키로 인한 메모리 고갈 방지. |
| 단일 uvicorn 워커 전제(`run_hook_relay.sh`에 `--workers` 없음) | 인메모리 dict를 `threading.Lock`으로만 보호하면 충분. **다중 워커 도입 시** 이 throttle는 워커별로 쪼개지므로 공유 저장소(Redis 등)가 필요하다 — 주의. |

### 상수시간 비교
web(`_web_password_ok`)·api(`require_admin`) 모두 `secrets.compare_digest`. (api는 과거 `!=`였다가 06-18 교체.)

### 한계 (근본 강화는 별도)
SSH 터널이라 web 클라 IP가 사실상 `127.0.0.1`로 집중 → throttle가 **글로벌 성격**(IP별 구분 약함). 약한 비밀의
*보완책*일 뿐 근본 해결이 아니다. 근본 강화 = **비밀 자체 교체** + **포트 20000 방화벽 소스 제한**(둘 다 운영자/sudo 필요).

---

## 3. 결정 기록 (ADR)

### ADR-1 — `GET /channels`(api 공개 조회) 공개 유지 (2026-06-18)
- **맥락**: 독립 보안 감사가 "api(20000)의 `GET /channels`(`list_channels`)가 `require_admin` 없이 전체 매핑을
  반환"을 HIGH로 지적.
- **결정**: **공개 유지**(운영자 결정). 코드 무변경.
- **근거**: ① 원 설계가 의도한 공개 기능(README 엔드포인트표 `공개(api)`, GUIDE.md "공개 조회—누구나 가능").
  ② 잠가도 **웹 UI(20001 authed 상대경로)·후크(서버측 CSV `lookup_channel`)는 무영향** — 핵심 기능이
  공개 GET에 의존하지 않는다. ③ 매핑(username·channel ID)은 타깃팅 정보이긴 하나 **토큰 같은 비밀은 아니다**.
  ④ 잠그면 GUIDE.md의 "암호 없이 매핑 확인" 문서 동작이 바뀐다.
- **결과**: 수용된 리스크. *보완책*은 포트 20000 방화벽 소스 제한(미적용, sudo 필요). **이 항목은 재논의/잠금 금지.**
- (관련 경미 지적: 평문 암호 `sessionStorage('relay-webpw')` 저장 — 기존 Basic-from-SPA 인증 방식의 구조적 노트,
  현재 XSS 경로 없음(서버 출력 `esc()`); `Authorization`/구성 상태 노출 LOW — 모두 사전존재·경미.)

---

## 4. 라이브 검증 방법론 (재사용 레시피)

> 발송측(Slack/Telegram/Discord) 봇 토큰·채널을 **메시지 발송 없이·토큰 미출력**으로 검증하는 레시피(getMe/getChat·`/users/@me`·auth.test)는 같은 "비밀 미출력" 철학으로 [`messenger-setup-and-verification.md`](messenger-setup-and-verification.md)에 별도 정리.

### 환경
- 브라우저 검증: `/app/miniconda3/envs/playwright/bin/python` + `DISPLAY=:99`(Xvfb) +
  `PLAYWRIGHT_BROWSERS_PATH=/app/playwright-browsers`.
- **실도메인 `https://relay.example.com:20001`**(이 호스트에서 해석·역방향 도달) → **cert까지 검증**(`-k`/ignore 불필요).
  `localhost:20001`은 cert 불일치 → ignore 필요.
- 정적 감사·PIL 렌더: miniconda base `/app/miniconda3/bin/python`. 앱 import: `…/envs/hook_relay/bin/python`.
- 스크린샷 `device_scale_factor=1`. 미인증 시 콘솔의 `/channels` 401은 **정상**(모달 트리거) — 필터.

### 인증/모달
- 가이드 모달은 미인증 상태에서 **`#guideOpen`**(로그인 모달 내 "설치 가이드 보기" 링크)로 연다 —
  헤더 `#guideBtn`은 로그인 스크림에 가려 클릭 불가.
- authed UI 검증: `sessionStorage['relay-webpw']`에 비밀을 주입(env에서 READ, **출력 금지**). 비밀 =
  `WEB_PASSWORD`(빈값이면 `DATA_ADMIN_TOKEN` 폴백). 프로그램 접근은 Basic `curl -u admin:"$PW"`.

### 브루트포스 throttle 검증 (curl)
```bash
# 오답 17회 → 401 누적 후 429 (LIMIT=15 기준)
for i in $(seq 1 17); do curl -s -o /dev/null -w '%{http_code} ' -u admin:wrongpw \
  https://relay.example.com:20001/channels; done; echo
# 정답은 throttle 중에도 즉시 통과(무잠금) + 카운터 리셋 — PW는 env에서 읽고 출력하지 말 것
PW=$(grep '^DATA_ADMIN_TOKEN=' /app/hook_relay/hook_relay.env | cut -d= -f2-)
curl -s -o /dev/null -w 'correct=%{http_code}\n' -u admin:"$PW" https://relay.example.com:20001/channels
unset PW
```
> ⚠️ 터널이라 검증 IP도 대개 `127.0.0.1`(다른 사용자와 같은 키) → **검증 끝에 반드시 정답 1회로 카운터를
> 리셋**(`_auth_clear`)해 실제 사용자가 throttle에 안 걸리게 한다.

증거(2026-06-18): `401×15 → 429`, 정답 `200`(무잠금), 리셋 후 오답 `401`(429 아님), api 오답 Bearer `401×6`
(429 없음=api 미throttle), `/notify` 무매핑 POST `404×3`(도달·미throttle·실발송 회피).

### 기타 함정
- **재기동 직후 curl이 SSL `000`**(TLS 전환 윈도). `--retry-connrefused`는 SSL 오류를 재시도하지 않으니
  짧은 폴링 루프로 200을 기다린다. `sleep` 포그라운드 차단 환경 → curl 폴링으로 대체.
- **app.py UI 수정**: `UI_HTML = r"""…"""` **내부**(파일 전체에서 `"""`는 정확히 2개 유지, 주입 CSS는 작은따옴표).
  수정 후 `cp app.py dist/` + `systemctl --user restart hook_relay-web`(인증/백엔드 변경이면 `hook_relay`도).
  **ruff가 파이썬부를 재포맷**(예: public 튜플 멀티라인화)하므로 다음 편집 전 앵커 재확인.
- **테마 대비 감사**: `<style>` 파싱 → `:root`→`[data-design=X]`→`[data-design=X][data-theme=light]` 캐스케이드
  해석 + 알파 합성 + WCAG 대비 계산. 목표 light 모드 `--faint`≥3.0(목표 3.5)·`--muted`≥4.5, 계층
  `--ink`>`--muted`>`--faint` 보존. (06-18 light 대비 개선: faint 2.29–2.99 → 3.5+.)
  > UI/테마 검증 상세(그라디언트 **마크 가독성은 픽셀 샘플링**으로만 판정, **테마 전환은 반드시 실제 토글 클릭**,
  > HUD 956px 경계 등)는 [`ui-theming-and-verification.md`](ui-theming-and-verification.md) 참고.

---

## 부록 — 핵심 코드 앵커 (함수명으로 grep; 라인은 참고)

| 함수/심볼 | app.py 위치(약) | 역할 |
|---|---|---|
| `_AUTH_FAIL_LIMIT/WINDOW/DELAY` | ~42 | throttle 튜너블(env) |
| `_auth_recent_fails`/`_auth_note_fail`/`_auth_clear` | ~50–78 | IP별 실패 카운터(Lock·prune·캡·eviction) |
| `_web_password_ok` | ~80 | web Basic 상수시간 검증 |
| `_web_gate` | ~92 | web 역할 게이트 + throttle 분기 |
| `require_admin` | ~168 | api Bearer 상수시간 + web_authed 단축 |
| `list_channels`(GET) / POST / DELETE `/channels` | ~263 / ~273 / ~296 | 조회 공개(ADR-1) · 쓰기 보호 |
