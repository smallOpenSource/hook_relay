# 웹 UI 테마 시스템 · 마크 가독성 · 라이브 검증 방법론

> 운영/엔드포인트/env는 [`../README.md`](../README.md), 인증·throttle·보안 검증은
> [`security-and-verification.md`](security-and-verification.md), 사용자 설치는 [`../dist/GUIDE.md`](../dist/GUIDE.md).
> 이 문서는 **웹 UI(20001)의 테마 시스템이 어떻게 동작하는지**, **타이틀/마크 가독성 규칙**,
> 그리고 **이 UI를 어떻게 라이브로 검증하는지(특히 반복해서 당한 함정)** 를 담는다.
> 코드 라인은 drift하므로 **함수명·셀렉터로 grep**해서 찾을 것(아래 앵커는 작성 시점 기준).

---

## 1. 2축 테마 시스템

웹 UI는 `<html data-design="KEY" data-theme="dark|light">` 2축이다.

- **16 디자인** × **2 모드** = 32 조합. 디자인 키: `amber`·`nothing` + 브랜드 14종
  (`spotify`·`apple`·`claude`·`bmw-m`·`ollama`·`figma`·`hp`·`airtable`·`mastercard`·`nvidia`·`tesla`·`discord`·`nike`·`notion`).
- 각 디자인은 CSS 변수 계약(`[data-design="KEY"]{ --bg/--ink/--mark-grad/--act-bg/… }`)으로 구현.
  `amber`는 `:root`(다크 기본)에, 나머지는 자기 블록에 정의.
- **light 모드는 `[data-design="KEY"][data-theme="light"]` 블록**으로 **색 변수만** 오버라이드한다.
  ⚠️ 이 블록들엔 `display`·`flex`·`padding`·`font-size` 같은 **레이아웃 속성이 없다**(있으면 안 됨).
  ⇒ **다크와 라이트의 배치는 동일**해야 정상. 배치가 모드별로 달라 보이면 그건 **색이 아닌 다른 원인**
  (예: 라벨 폭 변화 → 4장 참고)이다.
- 부팅: `<head>` 인라인 스크립트가 `localStorage['relay-design'|'relay-theme']`를 읽어 `<html>` 속성 설정.
  런타임 전환은 `setDesign`/`setTheme`(`data-*` 속성 + localStorage 갱신).

---

## 2. 마크(워드마크 "RELAY") 렌더링 모델

`.mark`는 **그라디언트 텍스트**다:

```
.mark{ background:var(--mark-grad); -webkit-background-clip:text; background-clip:text;
       -webkit-text-fill-color:var(--mark-fill); color:var(--mark-color) }
.mark .glyph{ -webkit-text-fill-color:var(--amber) }   /* ⇆ 글리프는 --amber 색 */
```

- `--mark-grad`가 그라디언트이고 `--mark-fill:transparent`면 → **글자에 그라디언트가 비친다**.
- `--mark-grad:none`이고 `--mark-fill`이 단색이면 → 그 **단색**으로 보인다.
- **글리프(⇆)** 색은 `--amber`(테마 강조색). light에서 `--amber`를 어두운 색으로 바꾸므로 보통 OK.

### 마크 가독성 규칙 (★ 새 테마 필수)

- **light 블록에서 마크를 반드시 어둡게 오버라이드**하라. 안 하면 다크용 밝은 마크
  (`--mark-fill:#ffffff` 등)를 **상속**해 **밝은 배경에 흰 글씨 = 안 보임**.
- 단색 마크 테마: light 블록에 `--mark-fill`·`--mark-color`를 어두운 값으로.
  그라디언트 마크 테마: light 블록에 `--mark-grad`를 어두운 stop들로.
- **다크 모드도** 점검하라. 그라디언트의 *가장 어두운 stop*이 어두운 배경에서 약할 수 있고,
  *밝은 글로우 배경*(예: discord 다크의 블러플 틴트)에서는 같은 계열 stop이 묻힌다.

### 06-19 실제 수정 (예시)
| 테마/모드 | 증상 | 조치 |
|---|---|---|
| bmw-m light | `--mark-fill:#ffffff` 상속 → #f0f0f0에 ~1.1 | `#0a0a0a` |
| claude light | 연한 탄 그라디언트 크림에 저대비 | 진한 러스트 `#a8542f,#974726,#763925` |
| discord light | 밝은 초록 stop 워싱아웃 | 어둡게 `#4049b8,#b82d8f,#178a4e` |
| claude dark | 끝 stop `#a9583e` ~2.7 | `#c2694a`(~4.0) |
| discord dark | 시작 블러플 `#5865f2`가 블러플 글로우에 묻힘 | 밝은 페리윙클 `#8b96ff` |

> dark 그라디언트는 **base 블록** `--mark-grad`가 담당하고 light는 light 블록이 별도 오버라이드하므로,
> **base를 고쳐도 light는 무영향**(반대도 성립).

---

## 3. HUD 레이아웃은 956px 컨테이너 경계에 붙어 있다 (취약점)

- 콘텐츠는 `.wrap{max-width:1000px; padding:48px 22px}` → 폭이 1000px만 넘으면 **컨텐츠 폭은 항상 ~956px**(중앙 정렬).
- HUD = `.hud{display:flex; justify-content:space-between; flex-wrap:wrap}` 안에 `.brand`(좌) + `.meta`(우, 컨트롤들).
- `브랜드폭 + 컨트롤폭 + gap > 956px`이면 **컨트롤이 다음 줄로 wrap**된다.
- amber는 **Syne 50px 마크 + 태그라인 = brand ~478px**(타 테마 ~300px)라 합계가 956에 **딱 붙어** 있다.
  ⇒ 아주 작은 폭 변화에도 wrap이 갈린다. **새 테마는 마크를 과대하게 키우지 말 것.**

### 06-18~19 두 번에 걸친 수정(둘 다 필요했음)
1. **좁은 폭(≤1000px, retina dsf2 ≈881px)**: `@media(max-width:1000px){ .tagline{display:none}
   .mark{font-size:min(var(--mark-size),42px)} }` — 브랜드를 압축. `min()`이라 작은 마크는 안 커진다.
2. **데스크탑(>1000px) — light만 wrap한 진짜 원인**: 테마 토글이 **모노스페이스**(`--f-label`=IBM Plex Mono)라
   **"LIGHT"(70px) > "DARK"(61px) = 9px 차** → light에서 `brand+meta+gap` 합이 **957>956**이 되어 **light만 2줄**,
   dark는 949≤956이라 1줄. *모든* 데스크탑 폭에서 발생(뷰포트 폭과 무관, 컨테이너가 항상 956이므로).
   ⇒ 미디어쿼리로 못 막는다. **수정 = HUD 간격 축소로 headroom 확보**:
   `.hud gap 18→12` · `.brand gap 15→11` · `.meta gap 9→6`(전역, light 합 957→~941, ~15px 여유, 마크 50px 유지).
- **규칙**: 브랜드+컨트롤이 956px 경계에 붙지 않게 여유를 둬라. **라벨 폭이 상태에 따라 바뀌는 요소**
  (토글 light/dark, 카운트 "N routes" 등)를 고려할 것.

---

## 4. 라이브 검증 방법론 (★ 반복해서 당한 함정 포함)

환경은 [`security-and-verification.md` §4](security-and-verification.md)와 동일:
`/app/miniconda3/envs/playwright/bin/python` + `DISPLAY=:99` + `PLAYWRIGHT_BROWSERS_PATH=/app/playwright-browsers`,
실도메인 `https://relay.example.com:20001`, 인증은 `sessionStorage['relay-webpw']` 주입(비밀은 env READ·출력 금지).

### 함정 ①: 테마 전환은 **반드시 실제 토글 클릭**으로 (setAttribute 금지)
`pg.evaluate("…setAttribute('data-theme','light')")`로만 바꾸면 **토글 버튼 텍스트가 안 바뀐다**
(여전히 "dark"). 그러면 3장의 "LIGHT vs DARK" 9px 폭 차가 **사라져** dark/light가 "배치 동일"로
**거짓 측정**된다. 실제로 이 함정 때문에 amber light HUD wrap을 **세 번** 놓쳤다.
→ **반드시 `pg.click('#themeBtn')`(= `setTheme` 호출)로 전환**해 라벨까지 실제 상태로 만든 뒤 측정/캡처.

### 함정 ②: 그라디언트 마크 대비는 **픽셀 샘플링**으로만
`getComputedStyle(.mark)`은 `-webkit-text-fill-color`가 `transparent`라 **실제로 보이는 색을 안 준다**
(그라디언트는 페인트지 색이 아님). → playwright로 `.mark`를 **클립 캡처(dsf2)** → 픽셀에서
배경(코너)·잉크(글자) 대비를 계산. 16테마를 세로로 쌓은 **몽타주**를 만들어 **육안 판정**까지 한다.
유용 지표: `ink45%`(대비≥4.5 픽셀 비율). `maxC`는 **글리프(⇆)가 항상 고대비**라 워드마크 가독성과 무관 →
신뢰하지 말 것. `worstReg`(열별 최저)도 그리드/AA 노이즈로 정상 테마도 ~2.0이라 신뢰 낮음.

### 함정 ③: PIL은 **base python에만** 있다
픽셀 분석용 `PIL`은 `/app/miniconda3/bin/python`에만 있고 **playwright env엔 없다**. →
(a) 캡처는 playwright env, **분석은 base python** 2단계로 나누거나,
(b) PNG 크기만 필요하면 헤더 24바이트를 `struct.unpack('>II', data[16:24])`로 직접 읽는다.

### 함정 ④: 폭/해상도(dsf)
- HUD wrap 경계는 **폭 스윕**으로 찾는다(`hudH≈68`=1줄, `≈119`=2줄). 단 ①을 지켜야 light가 제대로 측정됨.
- 사용자 스크린샷이 **retina(dsf2)**면 픽셀폭 ÷2가 CSS폭(예: 1762px 캡처 = 881 CSS → `@media≤1000` 적용 구간).
- `full_page` 스크린샷은 콘텐츠/뷰포트 높이에 좌우되니, 요소 비교는 **클립(`clip=`)** + 기하 측정을 병행.
- 하네스가 이미지를 표시 축소할 수 있으니 **수치(rect/대비)로 판정**하고 스크린샷은 보조로.

### 함정 ⑤: "서버 고쳐짐 vs 사용자 캐시/구버전"
PWA 서비스워커가 셸(`/`)을 캐시한다. 사용자가 준 이미지가 **수정 이전 캡처**일 수 있으니
파일 **mtime**과 **현재 서버 fresh 캡처**를 비교해 가른다. 구버전이면 **하드 리프레시(Ctrl+Shift+R)** 안내.

---

## 5. 모달 탭 패턴 (재사용)

가이드 모달의 **OS 탭**(`.os-tabs`/`.os-tab`/`.os-panel` + `setOS` + `#guide` 클릭 위임 + `localStorage['relay-os']`)과
**봇 토큰 플랫폼 탭**(`.tok-tabs`/`.tok-tab`/`.tok-panel` + `setPlat` + `relay-plat`)은 같은 패턴.
- 두 탭 시스템은 **클래스·`data-*` 속성을 분리**(`data-os` vs `data-plat`)해 클릭 위임이 서로 안 가로채게 한다.
- 복사 버튼은 기존 `#guide` 클릭 위임(`.copy`)을 **재사용**(새 JS 0).
- `setPlat`엔 `localStorage` 손상값 **whitelist 가드**(`'slack'|'telegram'|'discord'` 아니면 slack)로
  전 패널이 비는 사고를 막는다 — `setOS`도 같은 가드가 바람직.

---

## 부록 — 코드 앵커 (셀렉터/함수명으로 grep; 라인은 참고)

| 심볼 | app.py 위치(약) | 역할 |
|---|---|---|
| `.wrap{max-width:1000px}` | ~1187 | 콘텐츠 컨테이너(폭>1000이면 ~956px) |
| `.hud` / `.brand` / `.meta` | ~1189–1197 | HUD flex; gap 12/11/6(06-19 축소) |
| `.mark` / `.mark .glyph` | ~1191–1193 | 그라디언트 텍스트 마크 + 글리프 |
| `--mark-grad/--mark-fill/--mark-color` | 각 테마 블록 | 마크 색(light 블록서 어둡게 오버라이드) |
| `setTheme` / `#themeBtn` | ~1561 / ~1418 | 테마 토글(텍스트 dark↔light) — **검증 시 실제 클릭** |
| `@media(max-width:1000px)` | ~1368 | 좁은 폭 브랜드 압축(태그라인 숨김 + 마크 min cap) |
| `setOS`/`setPlat`, `.os-tab`/`.tok-tab` | ~1690 부근 | 모달 탭(독립 클래스·data 분리) |
