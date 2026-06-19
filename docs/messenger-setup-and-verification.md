# 메신저(발송측) 봇 설정 · 안전 검증 방법론

> 발송 경로(**Slack / Telegram / Discord**) 봇 토큰·채널 매핑·메시지 포맷, 그리고
> **비밀값을 노출하지 않고(메시지도 안 보내고) 설정을 검증하는 레시피**를 담는다.
> 웹 인증·브루트포스·라이브 UI 검증은 [`security-and-verification.md`](security-and-verification.md),
> 운영 개요·엔드포인트는 [`../README.md`](../README.md), 최종 사용자용 토큰 발급 절차는
> [`../dist/GUIDE.md`](../dist/GUIDE.md)(및 웹 가이드 모달의 플랫폼 탭, [`ui-theming-and-verification.md`](ui-theming-and-verification.md) 참조).
> 코드 라인은 drift하므로 **함수명으로 grep**해서 찾을 것(부록 앵커 기준).

---

## 1. 라우팅 모델 — `app` 필드가 플랫폼 선택자

클라이언트 후크가 `{app, username, status, …}`를 `/notify`로 POST한다(`notify`).

- `app` ∈ `APPS = ("slack","telegram","discord")` = **플랫폼 선택자**.
- `lookup_channel(app, username)` → `channels.csv`의 `(app,username) → channel` 매핑.
- `SENDERS[app](channel, build_text(p))` → 해당 플랫폼 전송 함수 호출.

`channels.csv` 컬럼은 `app,username,channel`이며, **`channel` 값의 의미는 플랫폼마다 다르다**(3장 표).
같은 `username`이라도 `app`이 다르면 별개 행이다(예: `slack,alice,#room` 과 `telegram,alice,123…`).

---

## 2. 메시지 포맷 — `build_text` 한 번, 세 플랫폼이 공유

`build_text(p)`가 본문 문자열을 **한 번** 만들고, `notify`가 그 결과를 `SENDERS[app]`에 그대로 넘긴다
→ **세 플랫폼 본문은 바이트 단위로 동일**하다.

현재 포맷:

```
[<상태>]                     ← STATUS_LABEL: task_complete=작업 완료 / awaiting_input=선택지 대기
<hostname> (<username>)
session: <세션명>            ← /rename 값(클라 후크가 transcript의 "Session renamed to:" system 항목에서 추출)
path: <project_path>
account: <계정 @앞부분>       ← str(claude_account).split("@")[0]
```

### 렌더링 차이(같은 텍스트, 다른 해석) — 중요

| 플랫폼 | 전송 필드 | 마크다운 해석 |
|---|---|---|
| Telegram (`send_telegram`) | `text` (**`parse_mode` 없음**) | 안 함 = **평문 그대로** |
| Slack (`send_slack`) | `text` | mrkdwn 해석 가능 |
| Discord (`send_discord`) | `content` | 마크다운 항상 해석 |

현재 본문에는 `* _ ~ \``  `같은 마크다운 특수문자가 없어 **셋 다 동일하게** 보인다.
**단, 동적 필드(세션명·path)에 그런 문자가 들어가면 Slack/Discord만 서식이 먹고 Telegram은 글자 그대로** 나온다.
완전 동일 렌더를 못박으려면 Slack을 `mrkdwn=false`로 보내면 된다(현재 미적용).

> ★ **회귀 주의**: 이 포맷(상태 라벨 **독립 줄** · `time:` 줄 **없음** · account **`@` 앞부분만**)은
> 사용자가 명시적으로 고른 것이다. 임의로 타임스탬프나 풀 이메일을 복원하지 말 것.

---

## 3. 플랫폼별 토큰·채널 형식 + 발급 함정

### Slack
- 토큰 `xoxb-…`(Bot User OAuth Token). scope `chat:write`(봇 미초대 공개 채널엔 `chat:write.public`).
- 발송 채널에서 `/invite @봇`. `channel` 값 = `#채널명` 또는 채널 ID.
- env `SLACK_BOT_TOKEN`. 전송은 `Authorization: Bearer <토큰>`.

### Telegram (★함정 다수)
- **토큰 = `봇ID:해시` 전체 문자열**(예 `123456789:AAH…`, 콜론 1개 + 해시 ~35자).
  흔한 실수: **봇 ID 숫자만** 넣기 / 복붙 중 **앞뒤 군더더기 숫자**가 끼기. ID만으론 401·404.
- **`channel` 값 = chat_id**: 개인 DM = **양수**(본인 user id), 그룹·채널 = **`-100…` 음수**(부호 포함).
- **chat_id ≠ 토큰** — chat_id는 `channels.csv`의 `channel` 열, 토큰은 env. 둘을 바꿔 넣는 혼동이 잦다.
- **chat_id 얻기**: 봇은 먼저 말을 걸 수 없으니 **사람이 봇에 먼저 메시지** → `getUpdates`의 `message.chat.id`.
  `"result":[]`(빈 배열)이면:
  - **DM**: 봇 채팅에서 **시작(Start)**/메시지를 안 보냄.
  - **그룹**: **프라이버시 모드**(`getMe`의 `can_read_all_group_messages:false`, 기본 ON)라 일반 메시지를 못 봄
    → `/cmd@봇` 같은 **명령**·봇 **@멘션**, 또는 `@BotFather /setprivacy` → **Disable** 후 봇 재추가.
  - 업데이트는 한 번 읽히면 **소비**되고 **24h 만료**. **웹훅**이 걸려 있으면 getUpdates는 항상 빔(`getWebhookInfo`로 확인).
  - 지름길: **`@userinfobot`** 에 메시지 → 내 숫자 ID 즉시.
- env `TELEGRAM_BOT_TOKEN`, base `TELEGRAM_API_BASE`(기본 `https://api.telegram.org`).

### Discord
- 토큰 형식: **점(`.`) 3분절**(대략 총 길이 ~72, 분절 길이 `[~26, 6, 38]`).
  봇 토큰 자체에는 `Bot ` 접두어가 **없다** — 앱이 `Authorization: Bot <토큰>`로 붙인다. **env에 `Bot `를 붙여넣지 말 것.**
- **`channel` 값 = channel_id**(17~19자리 snowflake). Discord 개발자 모드 → 채널 우클릭 **ID 복사**.
- 봇이 **해당 길드에 가입** + **View Channel** + **Send Messages** 권한을 가져야 한다(OAuth2 URL Generator: scope `bot` + 권한).
- env `DISCORD_BOT_TOKEN`, base `DISCORD_API_BASE`(기본 `https://discord.com/api/v10`).

---

## 4. ★ 안전 검증 레시피 (메시지 발송 없이 · 토큰 미출력)

**원칙**: 토큰은 **env에서 읽어 변수에 담고, 명령/출력에 절대 찍지 않는다.** 호출 후 `unset`하고,
응답 JSON에서 **비밀 아닌 필드(id·username·type 등)만** 파싱해 출력한다.
(웹 암호 검증의 `PW=$(grep…); curl -u …; unset PW` 패턴과 동일 철학 — [`security-and-verification.md`](security-and-verification.md) §4.)

공통 골격:

```bash
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' /app/hook_relay/hook_relay.env | cut -d= -f2-)
resp=$(curl -s --max-time 12 "https://api.telegram.org/bot${TOKEN}/getMe")
unset TOKEN
# resp(=getMe JSON)에는 토큰이 들어있지 않음 → 안전 필드만 파싱(base python으로)
/app/miniconda3/bin/python - "$resp" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
r = d.get("result", {})
print("ok=", d.get("ok"), "| id=", r.get("id"), "| username=", r.get("username"))
PY
```

| 플랫폼 | 토큰 유효성(발송 X) | 채널 도달성(발송 X) |
|---|---|---|
| **Telegram** | `GET /bot<TOKEN>/getMe` → `ok:true` + bot username | `GET /bot<TOKEN>/getChat?chat_id=<id>` → `ok:true` + `type`/`id`(없으면 400) |
| **Discord** | `GET /users/@me` (`Authorization: Bot <TOKEN>`) → bot `id`/`username` | `GET /channels/<id>` → `name`/`type`(0=텍스트)/`guild_id`(없으면 50001/10003) |
| **Slack** | `POST /api/auth.test` (`Authorization: Bearer <TOKEN>`) → `ok:true` + team/user | `conversations.info` 또는 실제 초대 여부로 확인 |

> **한계**: getChat / `GET /channels` 200은 **조회·View 권한까지만** 입증한다.
> **실제 전송 권한**(Telegram의 차단 여부, Discord의 **Send Messages** 권한)은 **1회 실발송으로만** 100% 확정된다.

---

## 5. 운영 체크리스트

- **토큰을 추가·변경하면 `systemctl --user restart hook_relay` 필수.** 토큰은 **모듈 로드 시 1회만** 읽힌다
  (`*_BOT_TOKEN = os.environ.get(...)`). 재기동하지 않으면 옛 값/빈 값이라 `… not set`·401이 난다.
  - 발송은 **api(20000)** 가 담당하므로 보통 `hook_relay`만 재기동한다. `app.py` 자체를 고쳤다면 양쪽 재기동 + `cp app.py dist/`.
- 재기동 직후 api 헬스는 `http://127.0.0.1:20000/health`. **기동 타이밍 때문에 즉시 curl이 `000`** 일 수 있으니
  `--retry N --retry-connrefused --retry-delay 1`로 폴링한다(이 환경은 포그라운드 `sleep` 차단).
- 라우팅 빠른 점검(무발송): 매핑 없는 username으로 `POST /notify` → **404 `no channel mapped`**
  (요청 도달·CSV 조회까지 정상임을 입증하면서 실발송은 회피).

---

## 6. 비밀 취급 (하드 제약)

봇 토큰과 `hook_relay.env`(0600)는 **READ만** 한다. **채팅·문서·커밋·명령 출력에 값을 절대 노출하지 않는다.**
노출되면 즉시 재발급(Slack=OAuth & Permissions Rotate · Telegram=BotFather `/revoke` · Discord=Reset Token)
→ env 교체 → `systemctl --user restart hook_relay`.

---

## 부록 — 핵심 코드 앵커 (함수명으로 grep; 라인은 참고)

| 심볼 | app.py 위치(약) | 역할 |
|---|---|---|
| `APPS` / `STATUS_LABEL` | 33 / 35 | 플랫폼 화이트리스트 · 상태 라벨(작업 완료/선택지 대기) |
| `lookup_channel` | 160 | `channels.csv` `(app,username)→channel` 조회 |
| `send_slack` / `send_telegram` / `send_discord` | 179 / 195 / 210 | 플랫폼 전송(Bearer / `bot<token>` URL / `Bot ` 헤더) |
| `SENDERS` | 223 | `app → sender` 매핑 |
| `build_text` | 226 | 본문 1회 생성 → 세 sender 공유 |
| `notify` | 247 | `/notify` 핸들러(라우팅 + 발송) |
| `*_BOT_TOKEN` / `*_API_BASE` | 19–27 | env 토큰 · 플랫폼 API base |
