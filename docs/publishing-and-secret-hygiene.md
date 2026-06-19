# 공개 repo 발행 · 비밀 위생 (GitHub publish)

> 비밀값·개인정보를 품은 **런타임 디렉터리를 공개 GitHub repo로 안전하게 발행**하는 정책과,
> **커밋 전 비밀 누출 게이트** 방법론을 담는다.
> 웹 인증·검증은 [`security-and-verification.md`](security-and-verification.md),
> 메신저 토큰·무발송 검증은 [`messenger-setup-and-verification.md`](messenger-setup-and-verification.md),
> 운영 개요·엔드포인트는 [`../README.md`](../README.md) 참고.
> 경로·패턴은 drift하므로 **부록 앵커를 grep으로 재확인**할 것.
>
> ★ **이 문서 자체가 공개 대상**이다 — 실토큰·실비밀번호·실 chat_id·실도메인을 한 글자도 적지 않는다
> (4장 게이트가 이 문서까지 검사한다). 비밀은 **카테고리로만** 기술한다.

---

## 1. 런타임 파일 ≠ 추적 파일 (핵심 원칙)

`/app/hook_relay`는 **가동 중인 런타임 디렉터리이면서** git repo다. 비밀·개인정보 파일은
**디스크에 그대로 두고 git 추적에서만 제외**한다 — 서비스는 env/CSV/인증서를 원래 경로에서 그대로
읽으므로 **발행이 가동에 영향을 주지 않는다**(파일을 옮기거나 지우지 않는다).

| 파일 | 디스크 | git 추적 |
|---|---|---|
| `hook_relay.env` (토큰·관리자 비번) | 유지 | ✗ 제외 |
| `channels.csv` (실 chat_id) | 유지 | ✗ 제외 |
| `ssl/<도메인>/*.pem` (개인키 포함) | 유지 | ✗ 제외 |
| `channels.csv.example` · `dist/hook_relay.env.example` | — | ✓ 동봉(양식) |

⇒ 클론 측은 양식을 복사해 자기 값으로 채운다: `cp channels.csv.example channels.csv`.

## 2. `.gitignore` 정책 — 3 범주

루트 `.gitignore`(부록)는 의도별로 묶는다:

1. **비밀(절대 금지)**: `hook_relay.env`, `*.env` — 단 **`!*.env.example` 예외**로 양식은 남긴다 — `ssl/`.
2. **개인정보**: **`/channels.csv`** — 실 매핑은 개인 telegram chat_id 등을 담는다.
   ★ **선행 슬래시(`/`)로 루트 한정**: 헤더만 있는 양식 `dist/channels.csv`는 추적을 유지하려는 것.
   슬래시 없는 `channels.csv`는 **모든 깊이를 매칭**해 양식까지 빠뜨린다.
3. **세션 상태·잡음**: `.omc/`, `.remember/`(핸드오프 로그에 비밀 흔적 가능), `.claude/`,
   `.ruff_cache/`, `.pytest_cache/`, `__pycache__/`·`*.py[cod]`, `work/`(스크린샷 등 스크래치), `*.log`.

## 3. 실도메인 placeholder 정책

공개 문서·dist 클라이언트 스크립트의 **실 운영 도메인은 placeholder(`relay.example.com`)로 치환**해
커밋한다. 실도메인 문자열은 추적 파일에 남기지 않는다(공개 색인 방지).

- **안전한 이유**: 클라이언트 스크립트는 서버 URL을 **인자/환경변수**(`NOTIFY_API_URL`)로 받으므로
  예시 도메인 치환이 **동작에 영향이 없다**(하드코딩된 기본값이 아니라 예시 주석/문서일 뿐).
- 루트 `README.md`는 애초에 `<도메인>` 자리표시자를 쓴다 — 같은 관례를 dist·docs로 확장한 것.
- **회귀 주의**: 누군가 실도메인을 예시로 다시 적으면 4장 게이트가 잡는다 → 재치환.

## 4. ★ 커밋 전 비밀 게이트 (재사용 레시피)

새 파일 추가·`dist/` 재동기화·문서 수정 후 **푸시 전에** 스테이징된 인덱스를 직접 스캔한다.
아래 세 검사를 **모두 통과해야** 커밋·푸시한다. (`G="git -C /app/hook_relay"`)

**검사 A — 무시돼야 할 경로가 추적되지 않았는가** (참이면 누출):
```bash
$G ls-files | grep -E '^hook_relay\.env$|^channels\.csv$|^ssl/|^\.(omc|remember|ruff_cache)/|^__pycache__/|^work/' \
  && echo '!! LEAK: ignored path tracked' || echo 'ok'
```

**검사 B — 양식 파일이 추적되는가** (있어야 정상):
```bash
$G ls-files | grep -E 'channels\.csv\.example|hook_relay\.env\.example|^dist/channels\.csv$'
```

**검사 C — 인덱스(스테이징 blob)에 실비밀 리터럴이 있는가** (`--cached`):
```bash
$G grep --cached -lFe '<패턴1>' -e '<패턴2>' …   # 매치=파일명만 출력(-l), 값은 안 찍힘
#  ↑ 매치되면 그 파일을 커밋에서 빼거나 스크럽. 매치 없음(exit 1) = clean.
```
스캔 **패턴은 프로젝트 비밀 그 자체**다 — 운영자가 스캔 시점에 실값으로 채우되
**셸 변수로 읽어** 넘기고 **명령 히스토리·문서·커밋에 남기지 않는다**(무발송·미출력 원칙은
[`messenger-setup-and-verification.md`](messenger-setup-and-verification.md) §4와 동일). 스캔 카테고리:

- **관리자 비밀번호** (`DATA_ADMIN_TOKEN`/`WEB_PASSWORD` 값)
- **봇 자격증명**: telegram **봇 ID**와 **토큰 해시 접두어**, slack `xoxb-` **뒤에 실문자가 붙은** 토큰,
  discord **3분절** 토큰
- **실 chat_id** (개인 telegram ID·discord channel_id — `channels.csv`에 있음)
- **실 운영 도메인**, **개인키 마커**(`BEGIN … PRIVATE KEY`)

> **거짓양성 주의**: `xoxb-`·`<봇ID>:<해시>` 같은 **형식 접두어/말줄임**은 문서 예시라 정상이다.
> 실토큰은 접두어 뒤에 실제 영숫자가 이어진다 — 패턴을 충분히 길게(예: `xoxb-[0-9A-Za-z]{6,}`) 잡아 구분한다.

구조 검사(A·B)는 "무엇이 추적되는가", 내용 검사(C)는 "추적되는 것에 비밀이 박혔는가"를 본다 — **둘 다** 필요하다.

## 5. 인증 · 푸시

- 푸시는 **gh 자격증명 헬퍼(HTTPS)** 로 한다:
  `gh auth status`(로그인·`repo` 스코프 확인) → `gh auth setup-git` → `git push -u origin main`.
  `GIT_TERMINAL_PROMPT=0`을 붙이면 자격증명 부재 시 **행(hang) 대신 즉시 실패**.
- gh 계정에 대상 repo **쓰기 권한(`repo` 스코프)** 이 있어야 한다.
- 로컬 SSH 키가 있어도 **GitHub에 등록 안 됐으면 `Permission denied (publickey)`** — HTTPS+gh가 단순하다.
- ★ **푸시는 자동 실행 금지**(전역 규칙). 사용자가 **명시 요청**했을 때만 푸시한다.
  `/append-wiki` 같은 "보관" 요청은 **로컬 커밋까지만** 하고 푸시는 **확인**받는다.

---

## 부록 — 앵커 (경로·패턴으로 재확인; 라인 번호는 인용 안 함)

| 대상 | 위치 | 비고 |
|---|---|---|
| 무시 규칙 | `/.gitignore` | 3 범주, `/channels.csv` 루트 한정 · `!*.env.example` 예외 |
| 양식 | `channels.csv.example` · `dist/hook_relay.env.example` | 클론 시 `cp …example <실파일>` |
| 도메인 치환 | dist 클라 스크립트 · `dist/GUIDE.md` · `docs/*` | 실도메인 대신 `relay.example.com` |
| 게이트 | (스크립트 없음 — 4장 명령을 푸시 전 수동 실행) | `git grep --cached` · `git ls-files` |
