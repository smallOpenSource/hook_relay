# 알림 의미론 · Claude Code 후크 이벤트 분류 (메인 vs 서브 · 완료 vs 대기)

> 클라이언트 후크(`claude-notify.sh`)가 Claude Code 후크 이벤트를 **어떤 `status`로 분류해 보낼지**,
> 왜 그렇게 정했는지, 그리고 **무발송·무비밀로 검증한 방법론**을 담는다.
> 발송측(봇 토큰·채널·`build_text` 포맷)은 [`messenger-setup-and-verification.md`](messenger-setup-and-verification.md),
> 이벤트→status 요약표는 [`../dist/README.md`](../dist/README.md) "상태 매핑",
> 웹 UI 라이브 검증 함정은 [`ui-theming-and-verification.md`](ui-theming-and-verification.md) §4.
> 코드 라인은 drift하므로 **함수/셀렉터로 grep**해서 찾을 것(부록 앵커 기준).
>
> ★ 이 문서는 공개 대상이다 — 실토큰·실 chat_id·실도메인을 적지 않는다(카테고리로만).

---

## 1. 후크 이벤트 모델 (공식 문서 기준)

출처: `code.claude.com/docs/en/hooks`. 알림 분류에 쓰는 세 이벤트:

| 이벤트 | 언제 | 분류에 쓰는 stdin 필드 |
|---|---|---|
| `Stop` | **메인 에이전트**가 한 턴을 끝내고 사용자에게 제어를 넘길 때 | `hook_event_name`, `stop_hook_active`(bool), **서브 컨텍스트에서만** `agent_id`/`agent_type` |
| `SubagentStop` | **서브에이전트(Task 도구)**가 완료될 때. *"커스텀 서브에이전트의 Stop 후크는 자동으로 SubagentStop으로 변환된다."* | `agent_id`, `agent_type` |
| `Notification` | Claude Code가 알림을 띄울 때 | **`notification_type`** ∈ `permission_prompt`·`idle_prompt`·`elicitation_dialog`·`auth_success`·`elicitation_complete`·`elicitation_response`, `message` |

핵심 비자명점:
- **`Stop`은 메인 세션 턴 종료에서만 발화**한다. 단, *서브에이전트/팀 컨텍스트* 안에서 끝나면 같은 `Stop`이라도 **`agent_id`/`agent_type`이 채워져** 온다 → 이게 "메인 vs 서브" 판별자.
- **`stop_hook_active`** = "Claude Code가 **stop 후크의 결과로 이미 계속 진행 중**"이라는 표시. 즉 **자율 루프(autopilot/ralph 등)가 세션을 잇는 중**이면 `true`.
- `Stop`은 사용자가 질문에 답하길 **기다리는 순간엔 발화하지 않는다**(턴이 완전히 끝날 때만). 사용자 입력 대기는 **`Notification`**(idle/elicitation/permission)로 온다.

## 2. "메인 세션의 실제 상태만 보고" — 판정 트리

**한눈에 — 이벤트·조건 → `status` → 메시지 타이틀** (`Stop` 조건은 위→아래 먼저 맞는 것):

| 후크 이벤트 | 추가 조건 | → `status` | 메시지 타이틀 |
|---|---|---|---|
| `Stop` | `agent_id`/`agent_type` 있음 (서브에이전트·팀원) | — | 🔇 무발송 |
| `Stop` | `stop_hook_active=true` (자율 루프 진행중) | — | 🔇 무발송 |
| `Stop` | 그 외 = 메인 세션 최종 종료 | `task_complete` | **[🆗 작업 완료]** |
| `SubagentStop` | (항상) | — | 🔇 무발송 |
| `Notification` | `elicitation_dialog` / `permission_prompt` | `awaiting_choice` | **[❓ 선택지 대기]** |
| `Notification` | `idle_prompt` | `awaiting_input` | **[⏳ 입력 대기]** |
| `Notification` | 그 외(`auth_success` 등) | — | 🔇 무발송 |
| 기타 이벤트 | — | — | 🔇 무발송 |

발송되는 타이틀은 **[🆗 작업 완료] / [❓ 선택지 대기] / [⏳ 입력 대기]** 3종뿐. 아래는 같은 트리의 주석 버전 — `claude-notify.sh`의 `case "$event"`(부록 앵커):

```
Stop:
  agent_id / agent_type 있음        → 묵음   # 서브에이전트·팀원 (메인 아님)
  stop_hook_active == true          → 묵음   # 자율 루프 진행 중 (아직 작업 중)
  그 외                             → task_complete   # 메인 세션 최종 완료
SubagentStop                        → 묵음   # 서브 완료 (settings 미등록 = 애초에 안 옴, 방어 branch만)
Notification:
  elicitation_dialog | permission_prompt → awaiting_choice  # 사용자 결정 대기
  idle_prompt                            → awaiting_input    # 유휴(입력 대기)
  그 외(auth_success 등)                 → 묵음
```

- **서브/팀원 억제**는 `agent_id` 가드로 한다. `SubagentStop`은 `settings.json`에 **등록하지 않아** 애초에 후크가 안 불린다(스크립트의 `SubagentStop) exit 0`은 방어용 죽은 코드).
- **루프 중 조기 완료 억제**는 `stop_hook_active` 가드로 한다.
- **선택지 대기**는 진짜 사용자 결정(`elicitation_dialog`/`permission_prompt`)일 때만. **유휴(`idle_prompt`)는 별도 `입력 대기`** — 둘을 묶으면 자리를 비웠을 뿐인데 "선택지 대기"로 오표기된다.
- **orchestration 세션 묵음**: 사용자가 직접 띄운 세션이 아니라 **OMC 오케스트레이션(ralph/autopilot 등)이 돌리는 세션**은 발송하지 않는다. 이런 세션은 페이로드·env가 메인과 **동일**(`ep=cli`·`agent_id` 없음·`stop_hook_active=false`)해서 이름·계정·entrypoint로는 못 가른다 — 대신 두 가지 실제 신호로 판별한다: ① 워커 세션 env의 `OMC_TEAM_WORKER`(레거시 `OMX_TEAM_WORKER`), ② `cwd`에서 위로 올라가 찾은 `.omc/state/sessions/<session_id>/`의 **활성 모드 상태파일**(`ralph-state.json`·`autopilot-state.json`·`boulder.json` 등). 둘 중 하나라도 있으면 status 판정 전 묵음. **직접 띄운 메인/대화형 세션엔 둘 다 없어 완료·대기 알림은 그대로 유지**된다.

## 3. status → 라벨 → 메시지

서버 `STATUS_LABEL`(app.py 앵커) 3종: `task_complete`=🆗 작업 완료 / `awaiting_choice`=❓ 선택지 대기 / `awaiting_input`=⏳ 입력 대기.
본문은 `build_text`가 1회 생성(세 플랫폼 공유) — 포맷·회귀주의는 [`messenger-setup-and-verification.md`](messenger-setup-and-verification.md) §2.
패치 스크립트는 `Notification` 매처에 세 종류(`idle_prompt|permission_prompt|elicitation_dialog`)를 등록한다(부록 앵커). **매처에 없는 종류는 후크 자체가 안 불린다.**

## 4. 진단 방법론 (근본 원인 찾기)

증상은 "서브에이전트 완료가 보고됨 / 선택지 대기 오표기 / 작업 중 완료". 근본 원인은 **코드가 아니라 배선 + 이벤트 의미 오해**였다:

1. **라이브 배선 확인**: `~/.claude/settings.json`의 `hooks.Stop`이 **매처/필터 없이** 후크에 연결 + **`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`**(env) → 팀 사용 시 팀원/서브 컨텍스트 Stop까지 무조건 발송.
2. **서버 로그로 과다발송 확증**: `journalctl --user -u hook_relay`에서 활성 작업 중 `POST /notify`가 **1~3분 간격으로 빗발침**.
3. **공식 문서로 이벤트 의미 확정**: agent_id/stop_hook_active/notification_type의 정확한 의미(§1).

⇒ "무엇이 보고되는가"는 **전적으로 클라이언트 후크가 결정**한다(서버 `/notify`는 순수 릴레이 — `(app,username)→channel` 라우팅 후 `build_text` 그대로 전송, 필터 0).

## 5. 검증 방법론 (무발송·무비밀)

세 층으로 검증한다:

### (a) 합성 결정 트리 테스트 — `NOTIFY_DRYRUN`
후크에 `NOTIFY_DRYRUN=1`을 주면 **발송 대신 결정된 status를 stdout으로** 출력한다(부록 앵커). 각 이벤트 JSON을 파이프로 먹여 분기를 단언한다(네트워크·Discord 무접촉):

```bash
HOOK=~/.claude/hooks/claude-notify.sh
run(){ printf '%s' "$1" | NOTIFY_DRYRUN=1 NOTIFY_API_URL=http://x NOTIFY_APP=discord NOTIFY_USER=u bash "$HOOK"; }
run '{"hook_event_name":"Stop","session_id":"s"}'                         # → SEND status=task_complete
run '{"hook_event_name":"Stop","agent_id":"sub-1","agent_type":"x"}'      # → (무출력=묵음)
run '{"hook_event_name":"Stop","stop_hook_active":true}'                  # → (묵음)
run '{"hook_event_name":"Notification","notification_type":"idle_prompt"}'        # → awaiting_input
run '{"hook_event_name":"Notification","notification_type":"elicitation_dialog"}' # → awaiting_choice
```
9개 분기 전수 통과를 확인했다.

### (b) 서버 라벨 — `build_text` 직접 호출
서비스 파이썬(`systemctl --user show hook_relay -p MainPID --value` → `/proc/<pid>/cmdline`)으로 `import app; app.build_text({...})`를 호출해 3 라벨 렌더를 확인한다. **발송·Discord 무접촉**.

### (c) 실이벤트 캡처 — `NOTIFY_DEBUG` / 임시 무조건 캡처
- `NOTIFY_DEBUG=1`이면 원본 JSON을 `~/.claude/logs/notify-debug.jsonl`에 적재(부록 앵커).
- ★**함정**: `settings.json`의 `.env`는 **세션 시작 시점에 로드**되므로, **이미 떠 있는 세션**의 후크엔 새 env가 안 닿는다. 실행 중 세션까지 즉시 캡처하려면 후크에 **무조건 적재 한 줄을 임시로** 넣었다 검증 후 제거한다(이벤트마다 후크가 새로 exec되므로 즉시 반영).

#### 실측 결과 (대표 4건)
| 세션 | 이벤트 | stop_hook_active | agent_id | 결과 |
|---|---|---|---|---|
| 비루프(대화형) | Stop | **false** | 없음 | task_complete 발송 ✓ |
| 자율 루프 | Stop | **true** | 없음 | 묵음 ✓ |
| 자율 루프 | Stop | **true** | 없음 | 묵음 ✓ |
| 비루프 | Notification(idle_prompt) | — | — | 입력 대기 ✓ |

읽는 법:
- **정상 완료(비루프)는 `stop_hook_active:false`** → 발송된다. 가드가 일반 완료까지 죽이지 않음(과다억제 없음).
- **루프 세션 Stop은 `stop_hook_active:true` 반복** → 묵음. 즉 신고의 실제 정체는 *"서브"가 아니라* **루프 도는 메인 세션이 매 반복 쏜 완료**였다(`agent_id` 달린 Stop은 0건).

## 6. 핵심 결정 — 루프 완료 핑 트레이드오프 (Option A)

문서상 루프의 **최종 stop도 `stop_hook_active:true`**다(연속 진행의 결과로 도달). 그러므로 `stop_hook_active=true` 묵음 가드는:
- 루프 중 **거짓 완료를 전부 제거**한다(신고 직접 해결).
- 그러나 **루프가 끝나는 1회 완료 핑도 함께 사라진다**(비루프 작업은 정상 핑).

거짓 완료 제거를 우선해 **Option A(현행 유지)** 로 확정했다. "루프 끝 1회 핑"이 필요하면 향후 **Option B(디바운스)**: 루프 stop들을 모아 세션이 조용해진 뒤 1회만 보낸다 — 단 async 후크 수명·지연 이슈가 있어 보류.

## 7. 재사용 교훈

- **죽은 코드 주의**: 스크립트가 `SubagentStop`을 처리해도 `settings.json`에 매처/이벤트를 **등록하지 않으면 영영 안 불린다**. 분기 추가 ≠ 활성화.
- **이벤트로 메인/서브 구분**: `Stop`+`agent_id` 또는 `SubagentStop`이 "메인 아님"의 신호. 팀(`AGENT_TEAMS`) 사용 시 특히 중요.
- **`bypassPermissions` 환경**에선 `permission_prompt`가 거의 안 뜬다 → 실질적 "선택지 대기"의 대부분은 `elicitation_dialog`다.
- **`settings.json` `.env`는 세션 시작 시 로드** — 실행 중 세션엔 후크 env 변경이 즉시 반영되지 않는다(검증 캡처 설계 시 고려).
- **서버는 순수 릴레이** — 분류 로직은 전부 클라이언트 후크에 둔다(서버 무상태·플랫폼 무관 유지).

---

## 부록 — 코드 앵커 (심볼/grep; 라인은 참고)

| 심볼 | 위치 | 역할 |
|---|---|---|
| `case "$event"` · `agent_id` · `stop_active` · `ntype` | `dist/claude-notify.sh` (~51 / ~31 / ~33 / ~34) | 판정 트리 + 분류 입력 |
| `NOTIFY_DRYRUN` · `NOTIFY_DEBUG` | `dist/claude-notify.sh` (~99 / ~36) | 무발송 테스트 · 원본 캡처 게이트 |
| `matcher: "idle_prompt\|permission_prompt\|elicitation_dialog"` | `dist/patch-claude-config.sh` (~48) | Notification 종류 등록 |
| `STATUS_LABEL` · `build_text` | `app.py` (~35 / ~230) | status→라벨 · 본문 1회 생성 |
| 상태 매핑 표 | `dist/README.md` | 이벤트→조건→status→표시 요약 |
