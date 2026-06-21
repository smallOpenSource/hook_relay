import asyncio
import base64
import csv
import os
import secrets
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

# ---- config (환경변수) ----
CHANNELS_CSV = Path(os.environ.get("CHANNELS_CSV", "channels.csv"))
DATA_ADMIN_TOKEN = os.environ.get("DATA_ADMIN_TOKEN", "")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# 발송 API URL (선택 오버라이드; 기본값은 각 서비스 공식 엔드포인트 — 하드코딩 회피)
SLACK_API_URL = os.environ.get(
    "SLACK_API_URL", "https://slack.com/api/chat.postMessage"
)
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
DISCORD_API_BASE = os.environ.get("DISCORD_API_BASE", "https://discord.com/api/v10")

# 웹(20001) 접근 암호. 미설정 시 DATA_ADMIN_TOKEN 사용. 역할은 실행 래퍼가 주입(api|web).
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "") or DATA_ADMIN_TOKEN
HOOK_RELAY_ROLE = os.environ.get("HOOK_RELAY_ROLE", "api")

APPS = ("slack", "telegram", "discord")
FIELDS = ["app", "username", "channel"]
STATUS_LABEL = {
    "task_complete": "작업 완료",
    "awaiting_choice": "선택지 대기",
    "awaiting_input": "입력 대기",
}

_lock = threading.Lock()
app = FastAPI()


# ---- 로그인 브루트포스 throttle (임계값은 비밀 아님 · env로 오버라이드 가능) ----
_AUTH_FAIL_LIMIT = int(os.environ.get("AUTH_FAIL_LIMIT", "15"))
_AUTH_FAIL_WINDOW = float(os.environ.get("AUTH_FAIL_WINDOW", "300"))
_AUTH_FAIL_DELAY = float(os.environ.get("AUTH_FAIL_DELAY", "1.0"))
_AUTH_MAX_KEYS = 4096
_auth_lock = threading.Lock()
_auth_fails = {}  # client ip -> [monotonic ts of WRONG-password attempts]


def _auth_recent_fails(key, now):
    with _auth_lock:
        ts = _auth_fails.get(key)
        if not ts:
            return 0
        ts[:] = [t for t in ts if now - t < _AUTH_FAIL_WINDOW]
        if not ts:
            _auth_fails.pop(key, None)
            return 0
        return len(ts)


def _auth_note_fail(key, now):
    with _auth_lock:
        if key not in _auth_fails and len(_auth_fails) >= _AUTH_MAX_KEYS:
            victim = min(_auth_fails, key=lambda k: _auth_fails[k][-1])
            _auth_fails.pop(victim, None)
        lst = _auth_fails.setdefault(key, [])
        lst.append(now)
        # 지속 공격 시 리스트 무제한 증가 방지: 최근 항목만 유지(영구 throttle는 유지됨)
        cap = _AUTH_FAIL_LIMIT * 4
        if len(lst) > cap:
            del lst[: len(lst) - cap]


def _auth_clear(key):
    with _auth_lock:
        _auth_fails.pop(key, None)


def _web_password_ok(authorization: str) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(authorization[6:]).decode("utf-8")
    except Exception:
        return False
    _, _, pw = raw.partition(":")
    return bool(WEB_PASSWORD) and secrets.compare_digest(pw, WEB_PASSWORD)


@app.middleware("http")
async def _web_gate(request: Request, call_next):
    # web 역할(20001): UI 셸(GET /)·/health 는 무인증 로드 → 앱 모달이 암호를 물음.
    # 데이터 경로는 암호 필수. 단 WWW-Authenticate 미포함 → 브라우저 기본창 대신 모달이 처리.
    # (서버는 여전히 Basic 수락 → curl -u·프로그램 접근 호환 유지.)
    if HOOK_RELAY_ROLE == "web":
        path = request.url.path
        public = (
            path
            in (
                "/health",
                "/manifest.webmanifest",
                "/sw.js",
                "/favicon.ico",
                "/favicon.svg",
                "/icon-192.png",
                "/icon-512.png",
                "/icon-maskable-512.png",
            )
            or (path == "/" and request.method == "GET")
            or path.startswith("/client/")
        )
        if not public:
            if not WEB_PASSWORD:
                return JSONResponse(
                    {"detail": "WEB_PASSWORD/DATA_ADMIN_TOKEN not configured"},
                    status_code=500,
                )
            authz = request.headers.get("authorization", "")
            key = request.client.host if request.client else "?"
            if _web_password_ok(authz):
                _auth_clear(key)
                request.state.web_authed = True
            elif authz.startswith("Basic "):
                # 잘못된 암호 제출 = 브루트포스 후보 (무자격 401·정답은 미카운트/리셋)
                now = time.monotonic()
                throttled = _auth_recent_fails(key, now) >= _AUTH_FAIL_LIMIT
                _auth_note_fail(key, now)
                if throttled:
                    await asyncio.sleep(_AUTH_FAIL_DELAY)
                    return JSONResponse(
                        {"detail": "too many failed attempts"},
                        status_code=429,
                        headers={"Retry-After": str(int(_AUTH_FAIL_WINDOW))},
                    )
                return JSONResponse({"detail": "auth required"}, status_code=401)
            else:
                return JSONResponse({"detail": "auth required"}, status_code=401)
    return await call_next(request)


# ---- CSV 저장소 ----
def read_rows():
    if not CHANNELS_CSV.exists():
        return []
    with CHANNELS_CSV.open(newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "") for k in FIELDS} for r in csv.DictReader(f)]


def write_rows(rows):
    tmp = CHANNELS_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    tmp.replace(CHANNELS_CSV)


def lookup_channel(app_name, username):
    for r in read_rows():
        if r["app"] == app_name and r["username"] == username:
            return r["channel"]
    return None


# ---- 관리자 인증 ----
def require_admin(authorization, request=None):
    # 웹 Basic 인증을 통과한 요청은 이미 관리자 권한.
    if request is not None and getattr(request.state, "web_authed", False):
        return
    if not DATA_ADMIN_TOKEN:
        raise HTTPException(500, "DATA_ADMIN_TOKEN is not configured on the server")
    if not secrets.compare_digest(authorization or "", "Bearer " + DATA_ADMIN_TOKEN):
        raise HTTPException(401, "invalid or missing admin token")


# ---- 발송기 ----
def send_slack(channel, text):
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    r = requests.post(
        SLACK_API_URL,
        headers={"Authorization": "Bearer " + SLACK_BOT_TOKEN},
        data={"channel": channel, "text": text},
        timeout=10,
    )
    r.raise_for_status()
    b = r.json()
    if not b.get("ok"):
        raise RuntimeError("slack error: " + str(b.get("error")))
    return str(b.get("ts"))


def send_telegram(channel, text):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    r = requests.post(
        TELEGRAM_API_BASE + "/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
        data={"chat_id": channel, "text": text},
        timeout=10,
    )
    r.raise_for_status()
    b = r.json()
    if not b.get("ok"):
        raise RuntimeError("telegram error: " + str(b.get("description")))
    return str(b.get("result", {}).get("message_id"))


def send_discord(channel, text):
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    r = requests.post(
        DISCORD_API_BASE + "/channels/" + channel + "/messages",
        headers={"Authorization": "Bot " + DISCORD_BOT_TOKEN},
        json={"content": text},
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json().get("id"))


SENDERS = {"slack": send_slack, "telegram": send_telegram, "discord": send_discord}


def build_text(p):
    status = STATUS_LABEL.get(p.get("status", ""), p.get("status", ""))
    account = str(p.get("claude_account", "")).split("@")[0]
    return "\n".join(
        [
            "[{}]".format(status),
            "- session:" + str(p.get("session_name", "")),
            "- path: " + str(p.get("project_path", "")),
            "- host:{}({})".format(p.get("hostname", ""), p.get("username", "")),
            "- account:" + account,
        ]
    )


# ---- API ----
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/notify")
async def notify(request: Request):
    p = await request.json()
    app_name = p.get("app")
    username = p.get("username")
    if app_name not in APPS:
        raise HTTPException(400, "app must be one of: " + ", ".join(APPS))
    if not username:
        raise HTTPException(400, "username is required")
    channel = lookup_channel(app_name, username)
    if channel is None:
        raise HTTPException(
            404, "no channel mapped for app={} username={}".format(app_name, username)
        )
    ref = SENDERS[app_name](channel, build_text(p))
    return {"ok": True, "app": app_name, "channel": channel, "ref": ref}


@app.get("/channels")
def list_channels(app: str = "", username: str = ""):
    rows = read_rows()
    if app:
        rows = [r for r in rows if r["app"] == app]
    if username:
        rows = [r for r in rows if r["username"] == username]
    return {"channels": rows}


@app.post("/channels")
async def upsert_channel(request: Request, authorization: str = Header(default="")):
    require_admin(authorization, request)
    p = await request.json()
    app_name = p.get("app")
    username = (p.get("username") or "").strip()
    channel = (p.get("channel") or "").strip()
    if app_name not in APPS:
        raise HTTPException(400, "app must be one of: " + ", ".join(APPS))
    if not username or not channel:
        raise HTTPException(400, "username and channel are required")
    with _lock:
        rows = read_rows()
        for r in rows:
            if r["app"] == app_name and r["username"] == username:
                r["channel"] = channel
                break
        else:
            rows.append({"app": app_name, "username": username, "channel": channel})
        write_rows(rows)
    return {"ok": True, "app": app_name, "username": username, "channel": channel}


@app.delete("/channels")
async def delete_channel(request: Request, authorization: str = Header(default="")):
    require_admin(authorization, request)
    p = await request.json()
    app_name = p.get("app")
    username = (p.get("username") or "").strip()
    if not app_name or not username:
        raise HTTPException(400, "app and username are required")
    with _lock:
        rows = read_rows()
        kept = [
            r for r in rows if not (r["app"] == app_name and r["username"] == username)
        ]
        write_rows(kept)
    return {"ok": True, "removed": len(rows) - len(kept)}


# ---- PWA (manifest / service worker / icons) ----
import base64 as _b64, json as _json

_ICONS = {
    "/icon-192.png": "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAC7klEQVR4nO3csY0UURRE0bcEQhKYpENYJLDBkAQhEAEGBg4IRBldxZ4TwdfoTkmt3zMv314/Hvyrd08fgG0CIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIiAiAiIiICICIiIgIgIiIqCHvf/05ekjRAT0vOmGBFRhtyEBtRhtSEBFFhsSUJe5hgRUZ6shATUaakhApVYaEhARAfWaGCEBVetv6OXb68enz/BT/+f1iK+fPzx9hN+yQAOav1cC2lDbkIBmdDYkoCWFDQloTFtDAhrT9kQmoCVt9ZyAhhTWcwJa0VnPCWhCbT3XdpXxBv3xqaq5nrNA5crrOQE166/nBERIQKUm5ucE1GmlnhNQoaF6TkBttuo5AVWZq+cE1GOxnhNQidF6TkANdus5d2GELBARAREREJHtgNp+ovAGDQekngarAamnxGRA6umxF5B6qowFpJ42SwGpp9BMQOrptBGQemptBEStgYDMT7P2gNRTrvp9IPX8UtULaL0LpJ4JpQGpZ0VjQOoZUheQerZ0BaSeOV0BVT1f8De6AjoNrakL6DQ0pTGg09CO0oBOQyOqrzJu/19w/3u9C/SDPsq1B3Qa6jYQEM02AjJCtTYCOg21mgnoNFRpKaDTUJ+xgE5DZfYCOg01mQzoNFRjNaDTUIf2uzDKDS8QDQREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBEREQEQERERARAREREBEBETkO54YdvJ4KIeRAAAAAElFTkSuQmCC",
    "/icon-512.png": "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAJb0lEQVR4nO3cyXUsxQJFUekv7MAJhnIHs3BAxuAEJsiCP2HRPNRVVWZGc/a2ICZ5T8Qkn99eX54A6Pnf6AMAMIYAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAPD09PT086+/jz4CVxMA4E8aUCMAwN80IEUAgH/RgA4BAH6kARECALxDAwoEACBKAID3eQRsTwCAD2nA3gQA+IwGbEwAgC9owK4EAPiaBmxJAIBv0YD9CADwXRqwGQEAbqABOxEA4DYasA0BAG6mAXsQAIAoAQDu4RGwAQEA7qQBqxMA4H4asDQBAB6iAesSAOBRGrAoAQAOoAErEgDgGBqwHAEADqMBaxEA4EgasBABAA6mAasQAIAoAQCO5xGwBAEATqEB83t+e30ZfYYonwcFf/z2y+gj8CEvAOBELjozEwDgXBowLQEATqcBcxIA4AoaMCEBAC6iAbMRAOA6GjAVAQAupQHzEACAKAEAruYRMAkBAAbQgBkIADCGBgwnAMAwGjCWAAAjacBAAgCM5HehAwkAMIz1H0sAgDGs/3ACAAxg/WcgAABRAgBczfV/EgIAXMr6z0MAgOtY/6kIAHAR6z8bAQCuYP0nJADA6az/nAQAOJf1n5YAACey/jP7afQBunwYTMVfOYO8AICzuOVMTgCAU1j/+QkAcDzrvwQBAA5m/VchAMCRrP9CBAA4jPVfiwAAx7D+yxEA4ADWf0UCABAlAMCjXP8XJQDAQ6z/ugQAuJ/1X5oAAHey/qsTAOAe1n8DAgDczPrvQQCA21j/bQgAcAPrvxMBAIgSAOC7XP83IwDAt1j//QgA8DXrvyUBAL5g/XclAMBnrP/GBAD4kPXfmwAA77P+2xMA4B3Wv0AAAKIEAPiR63+EAAD/Yv07BAD4m/VPEQDgT9a/5vnt9WX0GQAYwAsAIEoAAKIEACBKAACiBAAgSgAAogQAIEoAAKIEACBKAACiBAAgSgAAogQAIEoAcn7+9ffRRwCmIAAt1h/4iwCEWH/gnwSgwvoDPxCABOsP/JcAAEQJwP5c/4F3CcDmrD/wEQHYmfUHPiEA27L+wOcEYE/WH/iSAGzI+gPfIQC7sf7ANwnAVqw/8H0CABAlAPtw/QduIgCbsP7ArQRgB9YfuIMALM/6A/cRgLVZf+BuArAw6w88QgBWZf2BBwnAkqw/8DgBAIgSgPW4/gOHEIDFWH/gKAKwEusPHEgAlmH9gWMJwBqsP3A4AViA9QfOIACzs/7ASQRgatYfOM9Pow/AALrCVP747ZfRR4jyApiXmQZOJQCTsv7A2QRgRtYfuIAATMf6A9cQgLlYf+AyAjAR6w9cSQBmYf2BiwnAFKw/cD0BAIgSgPFc/4EhBGAw6w+MIgAjWX9gIAEYxvoDYwnAMP6ACIwlACNpADCQAAymAcAoAjCeBgBDCMAUNAC4ngAARAnALDwCgIsJwEQ0ALiSAMxFA4DLCMB0NAC4hgDMSAOACwjApDQAOJsAzEsDgFM9v72+jD4Dnznjp6HSAjx5AczPWAMnEYAFaABwBgEAiBKANXgEAIcTgGVoAHAsAViJBgAHEoDFaABwFAFYjwYAhxCAJWkA8DgBWJUGAA8SgIVpAPAIAVibBgB3E4DlaQBwHwEAiBKAHXgEAHcQgE1oAHArAdiHBgA3EYCtaADwfQKwGw0AvkkANqQBwHcIwJ40APiSAGxLA4DPCcDONAD4hABsTgOAjwgAQJQA7M8jAHiXACRoAPBfAlChAcAPBCBEA4B/EoAWDQD+8vz2+jL6DAAM4AUAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFH/B0mNCNvmKSxHAAAAAElFTkSuQmCC",
    "/icon-maskable-512.png": "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAInUlEQVR4nO3dzXHTYBSGUYdCaIJl2qEsGkgxNEEJqYAFDD/BsS3JM+a7zzkVaPU+uguNnl5fnk8A9Hx49AMA8BgCABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQCwko+fvz76EZhDAGAxGsC9CACsRwO4CwGAJWkAxwkArEoDOEgAAKIEABbmCOAIAYC1aQC7CQAsTwPYRwBgAg1gBwGAITSArQQA5tAANhEAGEUDuJ0AwDQawI0EAAbSAG4hAABRAgAzOQK4SgBgLA3gMgGAyTSACwQAhtMA3iMAMJ8GcNbT68vzo5+BxzAKNd++fHr0I/B/cQFAheTzhgBAiAbwJwGAFg3gFwEAiBIAyHEE8IMAQJEGcBIAyNIABAC6NCBOACBNA8oEANJ8HlwmANBl/eMEAKKsPwIARdafkwBAkPXnBwGAFuvPLwIAIdafPwkAQJQ/gsFKjny35fWfN1wAkGD9+ZcAwHzWn7MEAIaz/rxHAGAy688FAgBjWX8uEwCYyfpzlQDAQNafWwgATGP9uZEAwCjWn9sJAMxh/dlEAGAI689WAgATWH92EACAKAGA5Xn9Zx8BgLVZf3YTAFiY9ecIAYBVWX8OEgBYkvXnOAGA9Vh/7kIAYDHWn3vxU3iAKBcAQJQAAEQJAECUAABECQBAlAAARAkAQJQAAEQJAECUAHA3Hz9/ffQjABsIAPdh/WE5AsAdWH9YkQBwlPWHRQkAh1h/WJcAsJ/1h6UJADtZf1idALCH9YcBBAAgSgDYzOs/zCAAbGP9YQwBYAPrD5MIALey/jCMAHAT6w/zCADXWX8YSQC4wvrDVALAJdYfBhMA3mX9YTYB4DzrD+MJAGdYfygQAN6y/hDx9Pry/Ohn4D9i/YO+ffn06EfgMVwAAFECwG9e/yFFAPjJ+kONAHA6WX9IEgCsP0QJQJ31hywBSLP+UCYAXdYf4gSgy+c/ECcAaRoAZQJQpwGQJQBoAEQJAKeTBkCSAABECQA/OQKgRgD4TQMgRQD4iwZAhz+Cccbuj4T1AxbiAuAMOw4FAsB5GgDjCQDv0gCYTQC4RANgMAHgCg2AqQQAIEoAuM4RACMJADfRAJhHALiVBsAwAsAGGgCTCADbaACMIQBspgEwgwCwhwbAAALAThoAqxMA9tMAWJoAAEQJAIc4AmBdAsBRGgCLEgDuQANgRQLAfWgALMdP4QGiXAAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABAlAABRAgAQJQAAUQIAECUAAFECABD1HcJ9u3JldIhsAAAAAElFTkSuQmCC",
}
MANIFEST = {
    "name": "RELAY \u00b7 notification routing",
    "short_name": "RELAY",
    "description": "hook_relay notification routing console",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0c0b0e",
    "theme_color": "#0c0b0e",
    "lang": "ko",
    "orientation": "any",
    "icons": [
        {
            "src": "/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ],
}
SW_JS = "const C='relay-shell-v1';\nconst SHELL=['/','/manifest.webmanifest','/icon-192.png','/icon-512.png'];\nself.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()).catch(()=>self.skipWaiting()));});\nself.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});\nself.addEventListener('fetch',e=>{\n  const req=e.request; if(req.method!=='GET') return;\n  let url; try{ url=new URL(req.url); }catch(_){ return; }\n  if(url.origin===location.origin && (url.pathname.startsWith('/channels')||url.pathname==='/health'||url.pathname.startsWith('/notify'))) return;\n  if(req.mode==='navigate'){ e.respondWith(fetch(req).then(r=>{const cp=r.clone(); caches.open(C).then(c=>c.put('/',cp)); return r;}).catch(()=>caches.match('/'))); return; }\n  if(url.origin===location.origin){ e.respondWith(caches.match(req).then(c=>c||fetch(req).then(r=>{if(r.ok){const cp=r.clone(); caches.open(C).then(ca=>ca.put(req,cp));} return r;}))); }\n});\n"


@app.get("/manifest.webmanifest")
def _manifest():
    return Response(_json.dumps(MANIFEST), media_type="application/manifest+json")


@app.get("/sw.js")
def _sw():
    return Response(
        SW_JS, media_type="text/javascript", headers={"Cache-Control": "no-cache"}
    )


def _mk_icon(_p):
    @app.get(_p)
    def _icon():
        return Response(
            _b64.b64decode(_ICONS[_p]),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return _icon


for _ip in list(_ICONS):
    _mk_icon(_ip)


# ---- favicon (⇋ relay motif: SVG primary + multi-size ICO fallback) ----
FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32" role="img" aria-label="relay"><rect width="32" height="32" rx="7" fill="#0c0b0e"/><g fill="none" stroke="#f2a93b" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 12 H23"/><path d="M20 8.5 L24.5 12 L20 15.5"/><path d="M25 20 H9"/><path d="M12 16.5 L7.5 20 L12 23.5"/></g></svg>'
FAVICON_ICO = "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"


@app.get("/favicon.svg")
def _favicon_svg():
    return Response(
        FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.ico")
def _favicon_ico():
    return Response(
        _b64.b64decode(FAVICON_ICO),
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---- 클라이언트 후크 스크립트 공개 제공 (/client/<name>) — 비밀 없음, 무인증. 웹 가이드의 복붙 다운로드용 ----
_CLIENT_FILES = (
    "claude-notify.sh",
    "claude-notify-mac.sh",
    "claude-notify.ps1",
    "patch-claude-config.sh",
    "patch-claude-config-mac.sh",
    "patch-claude-config.ps1",
)
_BASE_DIR = Path(__file__).resolve().parent


def _client_dir():
    # 런타임(루트) 배치에선 scripts 가 dist/ 안, dist 배포판에선 app.py 와 같은 폴더.
    for d in (_BASE_DIR, _BASE_DIR / "dist"):
        if (d / "claude-notify.sh").exists():
            return d
    return _BASE_DIR


@app.get("/client/{name}")
def _client(name: str):
    if name not in _CLIENT_FILES:  # 화이트리스트 → 경로 우회 불가
        raise HTTPException(404, "unknown client file")
    p = _client_dir() / name
    if not p.is_file():
        raise HTTPException(404, "client file not available")
    return Response(
        p.read_text(encoding="utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/", response_class=HTMLResponse)
def ui():
    return UI_HTML


UI_HTML = r"""<!doctype html>
<html lang="ko" data-design="amber" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RELAY · notification routing</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#0c0b0e" id="themeColor">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="RELAY">
<script>try{var d=localStorage;document.documentElement.setAttribute('data-design',d.getItem('relay-design')||'amber');document.documentElement.setAttribute('data-theme',d.getItem('relay-theme')||'dark')}catch(e){document.documentElement.setAttribute('data-design','amber');document.documentElement.setAttribute('data-theme','dark')}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&family=Doto:wght@500;700;900&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&family=DM+Sans:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700&family=Cormorant+Garamond:ital,wght@0,400;0,500;1,400&family=JetBrains+Mono:wght@400;500&family=Barlow:wght@300;400;700&family=Barlow+Condensed:wght@700&family=Nunito:wght@400;500;600;700&family=Manrope:wght@400;500;600;700&family=Inter+Display:opsz,wght@14..72,400;14..72,500;14..72,600&family=Sofia+Sans:wght@400;500;700&family=Plus+Jakarta+Sans:wght@400;500;600&family=Hanken+Grotesk:wght@400;500;700;800&family=Bebas+Neue&display=swap" rel="stylesheet">
<style>
  /* ====== AMBER design · DARK (default) ====== */
  :root{
    --f-display:'Syne',ui-sans-serif,system-ui,sans-serif;
    --f-body:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
    --f-label:'IBM Plex Mono',ui-monospace,Menlo,monospace;
    --glow-disp:block; --grain-disp:block;
    --route-radius:14px; --card:linear-gradient(180deg, rgba(255,255,255,.026), rgba(255,255,255,.008)); --card-shadow:0 0 0 0 transparent;
    --lbl-transform:none; --lbl-spacing:.04em;
    --mark-grad:linear-gradient(92deg,#fff3df,#f2a93b 52%,#2dd4bf); --mark-fill:transparent; --mark-color:transparent; --mark-size:50px;
    --act-bg:#f2a93b; --act-ink:#1a1408; --act-radius:10px; --act-shadow:0 8px 24px -12px rgba(242,169,59,.95);
    --focus:#f2a93b; --base-fs:15px;
    --bg:#0c0b0e;
    --page-bg:
      radial-gradient(900px 520px at 12% -8%, rgba(242,169,59,.14), transparent 60%),
      radial-gradient(820px 520px at 110% 8%, rgba(45,212,191,.10), transparent 55%),
      radial-gradient(680px 680px at 50% 124%, rgba(242,169,59,.07), transparent 60%),
      linear-gradient(180deg,#0c0b0e,#100d12);
    --grid:rgba(255,255,255,.022); --grain-op:.05; --grain-blend:overlay; --glow-op:.5;
    --glow-a:rgba(242,169,59,.30); --glow-b:rgba(45,212,191,.18);
    --panel:#16131a; --panel-2:#1d1922; --ink:#f1ede6; --muted:#9c958a; --faint:#6b655c;
    --line:rgba(255,255,255,.06); --line-2:rgba(255,255,255,.12); --chip:rgba(255,255,255,.04); --chip-h:rgba(255,255,255,.09);
    --amber:#f2a93b; --amber-soft:rgba(242,169,59,.18);
    --ok:#48d39a; --okrgb:72,211,154; --ok-ink:#9ff0cf; --danger:#f56b73; --danger-soft:rgba(245,107,115,.2);
    --slack:#e8b84b; --telegram:#3aa0ef; --discord:#2dd4bf;
    --slack-glow:rgba(232,184,75,.45); --telegram-glow:rgba(58,160,239,.42); --discord-glow:rgba(45,212,191,.45);
    --on-accent:#1a1408;
    --panel-grad:linear-gradient(180deg, var(--panel), rgba(22,19,26,.55));
    --err-bg:rgba(40,14,16,.92); --err-ink:#ffb0b4; --good-bg:rgba(12,28,22,.92); --good-ink:#8ef0c4;
  }
  /* ====== AMBER · LIGHT ====== */
  [data-design="amber"][data-theme="light"]{
    --bg:#f4eee1;
    --page-bg:
      radial-gradient(900px 520px at 12% -8%, rgba(235,154,30,.13), transparent 60%),
      radial-gradient(820px 520px at 110% 8%, rgba(14,155,134,.09), transparent 55%),
      radial-gradient(680px 680px at 50% 124%, rgba(235,154,30,.06), transparent 60%),
      linear-gradient(180deg,#f7f1e6,#efe6d4);
    --grid:rgba(74,54,18,.05); --grain-op:.04; --grain-blend:multiply; --glow-op:.42;
    --glow-a:rgba(235,154,30,.16); --glow-b:rgba(14,155,134,.12);
    --panel:#fffdf7; --panel-2:#fbf6eb; --ink:#272118; --muted:#6e665a; --faint:#847d70;
    --line:rgba(50,38,12,.10); --line-2:rgba(50,38,12,.16); --chip:rgba(60,45,15,.04); --chip-h:rgba(60,45,15,.085);
    --amber:#eb9a1e; --amber-soft:rgba(235,154,30,.15); --act-bg:#eb9a1e; --act-ink:#2a1d05; --focus:#eb9a1e;
    --ok:#149a68; --okrgb:20,154,104; --ok-ink:#0b7a52; --danger:#d8434f; --danger-soft:rgba(216,67,79,.15);
    --slack:#a9791a; --telegram:#1c7fd0; --discord:#0e9b86; --on-accent:#fff8ea;
    --slack-glow:rgba(169,121,26,.3); --telegram-glow:rgba(28,127,208,.28); --discord-glow:rgba(14,155,134,.3);
    --card:linear-gradient(180deg, rgba(255,255,255,.72), rgba(255,255,255,.42)); --card-shadow:0 2px 8px -3px rgba(50,38,12,.12);
    --panel-grad:linear-gradient(180deg, #fffdf8, #fbf5ea); --mark-grad:linear-gradient(92deg,#4a3514,#d98a12 48%,#0e9b86);
    --err-bg:rgba(255,243,243,.96); --err-ink:#9c2730; --good-bg:rgba(237,250,244,.96); --good-ink:#0b7a52;
  }
  /* ====== NOTHING · DARK (OLED) ====== */
  [data-design="nothing"]{
    --f-display:'Doto',ui-monospace,monospace;
    --f-body:'Space Grotesk',ui-sans-serif,system-ui,sans-serif;
    --f-label:'Space Mono',ui-monospace,Menlo,monospace;
    --glow-disp:none; --grain-disp:none;
    --route-radius:4px; --card:transparent; --card-shadow:none;
    --lbl-transform:uppercase; --lbl-spacing:.14em;
    --mark-grad:none; --mark-fill:var(--ink); --mark-color:var(--ink); --mark-size:58px;
    --act-bg:var(--ink); --act-ink:var(--bg); --act-radius:999px; --act-shadow:none; --focus:var(--ink); --base-fs:15px;
    --bg:#0a0a0a; --page-bg:#0a0a0a;
    --grid:rgba(255,255,255,.05); --grain-op:0; --glow-op:0; --glow-a:transparent; --glow-b:transparent;
    --panel:#111111; --panel-2:#171717; --ink:#f4f4f2; --muted:rgba(244,244,242,.6); --faint:rgba(244,244,242,.4);
    --line:rgba(255,255,255,.13); --line-2:rgba(255,255,255,.22); --chip:transparent; --chip-h:rgba(255,255,255,.08);
    --amber:#f4f4f2; --amber-soft:rgba(255,255,255,.1);
    --ok:#46c07d; --okrgb:70,192,125; --ok-ink:rgba(244,244,242,.78); --danger:#D71921; --danger-soft:rgba(215,25,33,.16);
    --slack:var(--ink); --telegram:var(--ink); --discord:var(--ink);
    --slack-glow:transparent; --telegram-glow:transparent; --discord-glow:transparent; --on-accent:#0a0a0a;
    --panel-grad:transparent; --err-bg:transparent; --err-ink:#D71921; --good-bg:transparent; --good-ink:var(--ink);
  }
  /* ====== NOTHING · LIGHT (warm off-white) ====== */
  [data-design="nothing"][data-theme="light"]{
    --bg:#f2f1ec; --page-bg:#f2f1ec;
    --grid:rgba(0,0,0,.06);
    --panel:#ffffff; --panel-2:#f7f6f1; --ink:#121212; --muted:rgba(18,18,18,.6); --faint:rgba(18,18,18,0.51);
    --line:rgba(0,0,0,.14); --line-2:rgba(0,0,0,.22); --chip-h:rgba(0,0,0,.05);
    --amber:#121212; --amber-soft:rgba(0,0,0,.08); --on-accent:#f2f1ec;
    --ok:#1c7d52; --okrgb:28,125,82; --ok-ink:rgba(18,18,18,.72); --danger:#D71921; --danger-soft:rgba(215,25,33,.1);
  }

  /* === BRAND THEMES (design_system/) — theme picker === */
  .hud-select{font-family:var(--f-label); font-size:12px; color:var(--ink); background:var(--panel-2); border:1px solid var(--line-2); border-radius:999px; padding:6px 11px; cursor:pointer; max-width:148px; text-transform:var(--lbl-transform); letter-spacing:var(--lbl-spacing); transition:border-color .15s, background-color .15s}
  .hud-select:hover{border-color:var(--focus)}
  [data-design='nothing'] .hud-select{border-radius:4px}

  /* ---- Spotify ---- */
[data-design='spotify']{
  --f-display:'Montserrat',ui-sans-serif,system-ui,sans-serif;
  --f-body:'DM Sans',ui-sans-serif,system-ui,sans-serif;
  --f-label:'DM Sans',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:8px; --card:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01)); --card-shadow:rgba(0,0,0,0.3) 0px 8px 8px;
  --lbl-transform:uppercase; --lbl-spacing:.1em;
  --mark-grad:none; --mark-fill:#1ed760; --mark-color:#1ed760; --mark-size:42px;
  --act-bg:#1ed760; --act-ink:#000000; --act-radius:9999px; --act-shadow:0 8px 24px -8px rgba(30,215,96,.5);
  --focus:#1ed760; --base-fs:15px;
  --bg:#121212;
  --page-bg:linear-gradient(180deg,#121212,#121212);
  --grid:rgba(255,255,255,.018); --grain-op:0; --grain-blend:overlay; --glow-op:0;
  --glow-a:rgba(30,215,96,.0); --glow-b:rgba(30,215,96,.0);
  --panel:#181818; --panel-2:#1f1f1f; --ink:#ffffff; --muted:#b3b3b3; --faint:#6a6a6a;
  --line:rgba(255,255,255,.06); --line-2:rgba(77,77,77,1); --chip:#1f1f1f; --chip-h:#252525;
  --amber:#1ed760; --amber-soft:rgba(30,215,96,.18);
  --ok:#1ed760; --okrgb:30,215,96; --ok-ink:#000000; --danger:#f3727f; --danger-soft:rgba(243,114,127,.2);
  --slack:#ffa42b; --telegram:#539df5; --discord:#1ed760;
  --slack-glow:rgba(255,164,43,.4); --telegram-glow:rgba(83,157,245,.4); --discord-glow:rgba(30,215,96,.4);
  --on-accent:#000000;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(24,24,24,.6));
  --err-bg:rgba(40,10,14,.92); --err-ink:#f3727f; --good-bg:rgba(10,36,20,.92); --good-ink:#1ed760;
}
[data-design='spotify'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:linear-gradient(180deg,#f6f6f6,#ffffff);
  --panel:#eeeeee; --panel-2:#e8e8e8; --ink:#121212; --muted:#535353; --faint:#7d7d7d;
  --line:rgba(0,0,0,.08); --line-2:rgba(0,0,0,.18); --chip:rgba(0,0,0,.06); --chip-h:rgba(0,0,0,.12);
  --card:linear-gradient(180deg, rgba(0,0,0,.025), rgba(0,0,0,.01)); --card-shadow:rgba(0,0,0,0.15) 0px 8px 8px;
  --act-bg:#1ed760; --act-ink:#000000; --act-shadow:0 8px 24px -8px rgba(30,215,96,.45);
  --mark-fill:#121212; --mark-color:#121212;
  --amber-soft:rgba(30,215,96,.15);
  --glow-a:rgba(30,215,96,.0); --glow-b:rgba(30,215,96,.0);
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(238,238,238,.6));
  --err-bg:rgba(255,240,242,.95); --err-ink:#c0392b; --good-bg:rgba(232,252,240,.95); --good-ink:#157a3c;
  --danger-soft:rgba(243,114,127,.15);
}

  /* ---- Apple ---- */
[data-design='apple']{
  --f-display:'Inter',system-ui,-apple-system,sans-serif;
  --f-body:'Inter',system-ui,-apple-system,sans-serif;
  --f-label:'Inter',system-ui,-apple-system,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:18px; --card:rgba(42,42,44,1); --card-shadow:0 0 0 0 transparent;
  --lbl-transform:none; --lbl-spacing:-0.02em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:46px;
  --act-bg:#0066cc; --act-ink:#ffffff; --act-radius:9999px; --act-shadow:0 0 0 0 transparent;
  --focus:#0071e3; --base-fs:15px;
  --bg:#1d1d1f;
  --page-bg:#1d1d1f;
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(41,151,255,0); --glow-b:rgba(41,151,255,0);
  --panel:#272729; --panel-2:#2a2a2c; --ink:#f5f5f7; --muted:#cccccc; --faint:#7a7a7a;
  --line:rgba(255,255,255,.06); --line-2:rgba(255,255,255,.12); --chip:rgba(255,255,255,.06); --chip-h:rgba(255,255,255,.12);
  --amber:#2997ff; --amber-soft:rgba(41,151,255,.20);
  --ok:#30d158; --okrgb:48,209,88; --ok-ink:#30d158; --danger:#ff453a; --danger-soft:rgba(255,69,58,.20);
  --slack:#e8b84b; --telegram:#2997ff; --discord:#7c83ff;
  --slack-glow:rgba(232,184,75,.40); --telegram-glow:rgba(41,151,255,.40); --discord-glow:rgba(124,131,255,.40);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(39,39,41,.70));
  --err-bg:rgba(60,12,10,.92); --err-ink:#ff9f9b; --good-bg:rgba(8,36,16,.92); --good-ink:#6edc91;
}
[data-design='apple'][data-theme='light']{
  --glow-disp:none; --grain-disp:none;
  --bg:#f5f5f7;
  --page-bg:#f5f5f7;
  --grid:rgba(0,0,0,.03); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(0,102,204,0); --glow-b:rgba(0,102,204,0);
  --panel:#ffffff; --panel-2:#fafafc; --ink:#1d1d1f; --muted:#333333; --faint:#7a7a7a;
  --line:rgba(0,0,0,.08); --line-2:rgba(0,0,0,.14); --chip:rgba(0,0,0,.05); --chip-h:rgba(0,0,0,.10);
  --mark-fill:#1d1d1f; --mark-color:#1d1d1f;
  --act-bg:#0066cc; --act-ink:#ffffff; --act-radius:9999px; --act-shadow:0 0 0 0 transparent;
  --focus:#0071e3;
  --amber:#0066cc; --amber-soft:rgba(0,102,204,.18);
  --on-accent:#ffffff;
  --ok:#28a745; --okrgb:40,167,69; --ok-ink:#1a7a34; --danger:#d93025; --danger-soft:rgba(217,48,37,.15);
  --slack:#c9950a; --telegram:#0066cc; --discord:#5055c5;
  --slack-glow:rgba(201,149,10,.35); --telegram-glow:rgba(0,102,204,.30); --discord-glow:rgba(80,85,197,.35);
  --card:rgba(255,255,255,1); --card-shadow:0 1px 0 0 rgba(0,0,0,.08);
  --panel-grad:linear-gradient(180deg, #ffffff, rgba(250,250,252,.80));
  --err-bg:rgba(255,240,239,.96); --err-ink:#9c1a12; --good-bg:rgba(234,249,239,.96); --good-ink:#1a5c2a;
}

  /* ---- Claude ---- */
[data-design='claude']{
  --f-display:'Cormorant Garamond',Garamond,'Times New Roman',serif;
  --f-body:'DM Sans',Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --f-label:'DM Sans',Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:12px; --card:linear-gradient(180deg,rgba(250,249,245,.04),rgba(250,249,245,.01)); --card-shadow:0 1px 3px 0 rgba(20,20,19,.18);
  --lbl-transform:none; --lbl-spacing:0;
  --mark-grad:linear-gradient(92deg,#e8a55a,#cc785c 52%,#c2694a); --mark-fill:transparent; --mark-color:transparent; --mark-size:44px;
  --act-bg:#cc785c; --act-ink:#ffffff; --act-radius:8px; --act-shadow:0 4px 16px -6px rgba(204,120,92,.60);
  --focus:#cc785c; --base-fs:15px;
  --bg:#181715;
  --page-bg:linear-gradient(180deg,#1f1e1b,#181715);
  --grid:rgba(250,249,245,.025); --grain-op:0; --grain-blend:overlay; --glow-op:0;
  --glow-a:rgba(204,120,92,.0); --glow-b:rgba(93,184,166,.0);
  --panel:#252320; --panel-2:#2e2b27; --ink:#faf9f5; --muted:#a09d96; --faint:#6c6a64;
  --line:rgba(250,249,245,.07); --line-2:rgba(250,249,245,.13); --chip:rgba(250,249,245,.05); --chip-h:rgba(250,249,245,.10);
  --amber:#cc785c; --amber-soft:rgba(204,120,92,.20);
  --ok:#5db872; --okrgb:93,184,114; --ok-ink:#8ef0a0; --danger:#c64545; --danger-soft:rgba(198,69,69,.20);
  --slack:#e8a55a; --telegram:#5db8a6; --discord:#a09d96;
  --slack-glow:rgba(232,165,90,.40); --telegram-glow:rgba(93,184,166,.38); --discord-glow:rgba(160,157,150,.30);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg,var(--panel),rgba(37,35,32,.60));
  --err-bg:rgba(42,16,16,.92); --err-ink:#f0a8a8; --good-bg:rgba(12,30,18,.92); --good-ink:#8ef0a0;
}

[data-design='claude'][data-theme='light']{
  --bg:#faf9f5;
  --page-bg:#faf9f5;
  --panel:#efe9de; --panel-2:#e8e0d2;
  --ink:#141413; --muted:#6b6963; --faint:#7d7a72;
  --line:rgba(20,20,19,.10); --line-2:rgba(20,20,19,.18); --chip:rgba(20,20,19,.05); --chip-h:rgba(20,20,19,.10);
  --card:linear-gradient(180deg,rgba(255,255,255,.60),rgba(255,255,255,.30)); --card-shadow:0 1px 3px 0 rgba(20,20,19,.08);
  --amber-soft:rgba(204,120,92,.14);
  --panel-grad:linear-gradient(180deg,var(--panel),rgba(239,233,222,.70));
  --glow-disp:none; --grain-disp:none; --glow-op:0;
  --slack:#d4820a; --telegram:#2a8c7a; --discord:#5a5754;
  --slack-glow:rgba(212,130,10,.30); --telegram-glow:rgba(42,140,122,.28); --discord-glow:rgba(90,87,84,.20);
  --ok:#3a9e52; --okrgb:58,158,82; --ok-ink:#1e5c2e; --danger:#b03030; --danger-soft:rgba(176,48,48,.14);
  --err-bg:rgba(255,235,235,.95); --err-ink:#7a1e1e; --good-bg:rgba(230,248,234,.95); --good-ink:#1e5c2e;
  --on-accent:#ffffff;
  --mark-grad:linear-gradient(92deg,#a8542f,#974726 55%,#763925); --mark-fill:transparent; --mark-color:transparent;
}

  /* ---- BMW M ---- */
[data-design='bmw-m']{
  --f-display:'Barlow Condensed',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Barlow',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Barlow',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:0px; --card:none; --card-shadow:0 0 0 1px #3c3c3c;
  --lbl-transform:uppercase; --lbl-spacing:.107em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:48px;
  --act-bg:#ffffff; --act-ink:#000000; --act-radius:0px; --act-shadow:none;
  --focus:#1c69d4; --base-fs:15px;
  --bg:#000000;
  --page-bg:#000000;
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(28,105,212,.0); --glow-b:rgba(226,39,24,.0);
  --panel:#1a1a1a; --panel-2:#262626; --ink:#ffffff; --muted:#7e7e7e; --faint:#3c3c3c;
  --line:rgba(255,255,255,.10); --line-2:rgba(255,255,255,.22); --chip:rgba(255,255,255,.05); --chip-h:rgba(255,255,255,.12);
  --amber:#ffffff; --amber-soft:rgba(255,255,255,.12);
  --ok:#0fa336; --okrgb:15,163,54; --ok-ink:#4ddd80; --danger:#e22718; --danger-soft:rgba(226,39,24,.18);
  --slack:#f4b400; --telegram:#1c69d4; --discord:#0066b1;
  --slack-glow:rgba(244,180,0,.40); --telegram-glow:rgba(28,105,212,.42); --discord-glow:rgba(0,102,177,.40);
  --on-accent:#000000;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(26,26,26,.60));
  --err-bg:rgba(30,6,4,.95); --err-ink:#ff8a82; --good-bg:rgba(4,22,8,.95); --good-ink:#4ddd80;
}
[data-design='bmw-m'][data-theme='light']{
  --bg:#f0f0f0;
  --page-bg:#f0f0f0;
  --panel:#ffffff; --panel-2:#e6e6e6;
  --ink:#000000; --muted:#4a4a4a; --faint:#7e7e7e;
  --mark-fill:#0a0a0a; --mark-color:#0a0a0a;
  --line:rgba(0,0,0,.10); --line-2:rgba(0,0,0,.22); --chip:rgba(0,0,0,.05); --chip-h:rgba(0,0,0,.10);
  --amber:#000000; --amber-soft:rgba(0,0,0,.10);
  --act-bg:#000000; --act-ink:#ffffff; --act-shadow:none;
  --focus:#1c69d4;
  --on-accent:#ffffff;
  --grid:rgba(0,0,0,.06);
  --card:none; --card-shadow:0 0 0 1px #d0d0d0;
  --panel-grad:linear-gradient(180deg, #ffffff, rgba(255,255,255,.70));
  --slack:#b38600; --telegram:#1c69d4; --discord:#0066b1;
  --slack-glow:rgba(179,134,0,.35); --telegram-glow:rgba(28,105,212,.38); --discord-glow:rgba(0,102,177,.35);
  --err-bg:rgba(250,230,228,.97); --err-ink:#a01008; --good-bg:rgba(228,248,234,.97); --good-ink:#065a1c;
  --ok-ink:#0a7a28; --danger-soft:rgba(226,39,24,.12);
}

  /* ---- Ollama ---- */
[data-design='ollama']{
  --f-display:'Nunito',ui-rounded,system-ui,sans-serif;
  --f-body:'Inter',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Inter',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:12px; --card:rgba(255,255,255,.04); --card-shadow:0 0 0 1px rgba(255,255,255,.08);
  --lbl-transform:none; --lbl-spacing:0;
  --mark-grad:none; --mark-fill:#f5f5f5; --mark-color:#f5f5f5;
  --act-bg:#f5f5f5; --act-ink:#0a0a0a; --act-radius:9999px; --act-shadow:none;
  --focus:rgba(59,130,246,.5);
  --bg:#0a0a0a;
  --page-bg:#0a0a0a;
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:transparent; --glow-b:transparent;
  --panel:#171717; --panel-2:#1f1f1f; --ink:#f5f5f5; --muted:#a3a3a3; --faint:#737373;
  --line:rgba(255,255,255,.08); --line-2:rgba(255,255,255,.14); --chip:rgba(255,255,255,.05); --chip-h:rgba(255,255,255,.10);
  --amber:#f5f5f5; --amber-soft:rgba(245,245,245,.12);
  --ok:#27c93f; --okrgb:39,201,63; --ok-ink:#27c93f; --danger:#ff5f56; --danger-soft:rgba(255,95,86,.18);
  --slack:#ffbd2e; --telegram:#a3a3a3; --discord:#f5f5f5;
  --slack-glow:rgba(255,189,46,.35); --telegram-glow:rgba(163,163,163,.30); --discord-glow:rgba(245,245,245,.25);
  --on-accent:#0a0a0a;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(23,23,23,.70));
  --err-bg:rgba(30,10,10,.95); --err-ink:#ff9d99; --good-bg:rgba(8,24,12,.95); --good-ink:#6ee87e;
}
[data-design='ollama'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:#ffffff;
  --panel:#ffffff; --panel-2:#fafafa; --ink:#000000; --muted:#6b6b6b; --faint:#808080;
  --line:rgba(0,0,0,.10); --line-2:rgba(0,0,0,.18); --chip:rgba(0,0,0,.04); --chip-h:rgba(0,0,0,.08);
  --amber:#000000; --amber-soft:rgba(0,0,0,.08);
  --act-bg:#000000; --act-ink:#ffffff; --act-shadow:none;
  --focus:rgba(59,130,246,.5);
  --mark-fill:#000000; --mark-color:#000000;
  --grid:rgba(0,0,0,.05); --glow-op:0; --grain-op:0;
  --glow-a:transparent; --glow-b:transparent;
  --card:rgba(0,0,0,.02); --card-shadow:0 0 0 1px rgba(0,0,0,.09);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(250,250,250,.80));
  --slack:#d97706; --telegram:#525252; --discord:#000000;
  --slack-glow:rgba(217,119,6,.30); --telegram-glow:rgba(82,82,82,.25); --discord-glow:rgba(0,0,0,.20);
  --ok:#16a34a; --okrgb:22,163,74; --ok-ink:#15803d; --danger:#dc2626; --danger-soft:rgba(220,38,38,.12);
  --err-bg:rgba(254,242,242,.95); --err-ink:#b91c1c; --good-bg:rgba(240,253,244,.95); --good-ink:#15803d;
}

  /* ---- Figma ---- */
[data-design='figma']{
  --f-display:'Inter',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Inter',ui-sans-serif,system-ui,sans-serif;
  --f-label:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace;
  --glow-disp:none; --grain-disp:none;
  --route-radius:24px; --card:rgba(255,255,255,0.04); --card-shadow:0 0 0 1px rgba(255,255,255,0.08);
  --lbl-transform:uppercase; --lbl-spacing:.06em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:42px;
  --act-bg:#ff3d8b; --act-ink:#ffffff; --act-radius:9999px; --act-shadow:none;
  --focus:#ff3d8b; --base-fs:15px;
  --bg:#0d0d14;
  --page-bg:#0d0d14;
  --grid:rgba(255,255,255,0.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(255,61,139,0); --glow-b:rgba(31,29,61,0);
  --panel:#1a1a2e; --panel-2:#22223a; --ink:#f0f0f0; --muted:#8888aa; --faint:#6f6f93;
  --line:rgba(255,255,255,0.08); --line-2:rgba(255,255,255,0.14); --chip:rgba(255,255,255,0.05); --chip-h:rgba(255,255,255,0.10);
  --amber:#ff3d8b; --amber-soft:rgba(255,61,139,0.18);
  --ok:#1ea64a; --okrgb:30,166,74; --ok-ink:#6edaa0; --danger:#ff5c6c; --danger-soft:rgba(255,92,108,0.18);
  --slack:#dceeb1; --telegram:#c5b0f4; --discord:#f3c9b6;
  --slack-glow:rgba(220,238,177,0.35); --telegram-glow:rgba(197,176,244,0.35); --discord-glow:rgba(243,201,182,0.35);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(26,26,46,0.7));
  --err-bg:rgba(40,0,20,0.95); --err-ink:#ffb0cc; --good-bg:rgba(5,28,14,0.95); --good-ink:#7adba8;
}
[data-design='figma'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:#ffffff;
  --panel:#f7f7f5; --panel-2:#efefed; --ink:#000000; --muted:#555555; --faint:#838383;
  --line:rgba(0,0,0,0.10); --line-2:rgba(0,0,0,0.18); --chip:rgba(0,0,0,0.05); --chip-h:rgba(0,0,0,0.09);
  --mark-fill:#000000; --mark-color:#000000;
  --act-bg:#ff3d8b; --act-ink:#ffffff; --act-shadow:none;
  --glow-a:rgba(255,61,139,0); --glow-b:rgba(197,176,244,0);
  --grid:rgba(0,0,0,0.04); --grain-op:0;
  --card:rgba(0,0,0,0.03); --card-shadow:0 0 0 1px rgba(0,0,0,0.09);
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(247,247,245,0.8));
  --slack:#3a6b00; --telegram:#5a2db8; --discord:#8b4000;
  --slack-glow:rgba(58,107,0,0.25); --telegram-glow:rgba(90,45,184,0.25); --discord-glow:rgba(139,64,0,0.25);
  --amber-soft:rgba(255,61,139,0.12);
  --ok:#1ea64a; --okrgb:30,166,74; --ok-ink:#0d6e30; --danger:#d10050; --danger-soft:rgba(209,0,80,0.12);
  --err-bg:rgba(255,235,242,0.98); --err-ink:#a00038; --good-bg:rgba(232,252,240,0.98); --good-ink:#0a5a28;
}

  /* ---- HP ---- */
[data-design='hp']{
  --f-display:'Manrope',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Manrope',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Manrope',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:block; --grain-disp:none;
  --route-radius:16px; --card:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.01)); --card-shadow:0 2px 8px rgba(0,0,0,.32);
  --lbl-transform:uppercase; --lbl-spacing:0.07em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:46px;
  --act-bg:#024ad8; --act-ink:#ffffff; --act-radius:4px; --act-shadow:0 4px 16px -6px rgba(2,74,216,.7);
  --focus:#024ad8; --base-fs:15px;
  --bg:#0d0e14;
  --page-bg:radial-gradient(800px 400px at 50% -10%,rgba(2,74,216,.12),transparent 65%),linear-gradient(180deg,#0d0e14,#0b0c12);
  --grid:rgba(255,255,255,.018); --grain-op:0; --grain-blend:normal; --glow-op:.45;
  --glow-a:rgba(2,74,216,.28); --glow-b:rgba(41,110,249,.14);
  --panel:#131520; --panel-2:#1a1c2a; --ink:#f0f1f4; --muted:#9098b0; --faint:#5a6070;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.13); --chip:rgba(255,255,255,.05); --chip-h:rgba(255,255,255,.10);
  --amber:#024ad8; --amber-soft:rgba(2,74,216,.18);
  --ok:#2ec97e; --okrgb:46,201,126; --ok-ink:#7eedb6; --danger:#b3262b; --danger-soft:rgba(179,38,43,.2);
  --slack:#f2a93b; --telegram:#296ef9; --discord:#7fadbe;
  --slack-glow:rgba(242,169,59,.38); --telegram-glow:rgba(41,110,249,.42); --discord-glow:rgba(127,173,190,.38);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg,var(--panel),rgba(19,21,32,.55));
  --err-bg:rgba(40,8,10,.92); --err-ink:#f0a0a4; --good-bg:rgba(8,28,18,.92); --good-ink:#7eedb6;
}
[data-design='hp'][data-theme='light']{
  --glow-disp:none; --grain-disp:none;
  --mark-fill:#1a1a1a; --mark-color:#1a1a1a;
  --act-bg:#024ad8; --act-ink:#ffffff; --act-shadow:0 4px 14px -6px rgba(2,74,216,.5);
  --focus:#024ad8;
  --bg:#ffffff;
  --page-bg:#ffffff;
  --grid:rgba(0,0,0,.015); --grain-op:0; --glow-op:0;
  --glow-a:transparent; --glow-b:transparent;
  --panel:#ffffff; --panel-2:#f7f7f7; --ink:#1a1a1a; --muted:#636363; --faint:#828997;
  --line:rgba(0,0,0,.08); --line-2:rgba(0,0,0,.16); --chip:rgba(0,0,0,.04); --chip-h:rgba(0,0,0,.08);
  --amber:#024ad8; --amber-soft:rgba(2,74,216,.10);
  --ok:#1d8a56; --okrgb:29,138,86; --ok-ink:#0f5c38; --danger:#b3262b; --danger-soft:rgba(179,38,43,.12);
  --slack:#c87d00; --telegram:#024ad8; --discord:#356373;
  --slack-glow:rgba(200,125,0,.28); --telegram-glow:rgba(2,74,216,.28); --discord-glow:rgba(53,99,115,.28);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg,#ffffff,rgba(247,247,247,.6));
  --card:rgba(255,255,255,1); --card-shadow:0 2px 8px rgba(26,26,26,.08);
  --err-bg:rgba(255,240,240,.96); --err-ink:#b3262b; --good-bg:rgba(240,255,248,.96); --good-ink:#0f5c38;
}

  /* ---- Airtable ---- */
[data-design='airtable']{
  --f-display:'Inter Display','Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --f-body:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --f-label:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:12px; --card:rgba(29,31,37,1); --card-shadow:0 1px 0 0 rgba(255,255,255,.06);
  --lbl-transform:none; --lbl-spacing:0;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:46px;
  --act-bg:#181d26; --act-ink:#ffffff; --act-radius:12px; --act-shadow:0 2px 8px -2px rgba(24,29,38,.55);
  --focus:#aa2d00; --base-fs:15px;
  --bg:#0f1116;
  --page-bg:linear-gradient(180deg,#0f1116,#13161c);
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(170,45,0,.20); --glow-b:rgba(245,233,212,.08);
  --panel:#181d26; --panel-2:#1d1f25; --ink:#f0eeeb; --muted:#9297a0; --faint:#787d86;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.13); --chip:rgba(255,255,255,.05); --chip-h:rgba(255,255,255,.10);
  --amber:#aa2d00; --amber-soft:rgba(170,45,0,.18);
  --ok:#39bf45; --okrgb:57,191,69; --ok-ink:#7de885; --danger:#f56b73; --danger-soft:rgba(245,107,115,.18);
  --slack:#fcab79; --telegram:#a8d8c4; --discord:#f4d35e;
  --slack-glow:rgba(252,171,121,.35); --telegram-glow:rgba(168,216,196,.30); --discord-glow:rgba(244,211,94,.30);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(24,29,38,.60));
  --err-bg:rgba(40,10,4,.92); --err-ink:#ffb0a0; --good-bg:rgba(8,26,10,.92); --good-ink:#7de885;
}
[data-design='airtable'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:#ffffff;
  --panel:#ffffff; --panel-2:#f8fafc; --ink:#181d26; --muted:#41454d; --faint:#767b85;
  --line:rgba(0,0,0,.09); --line-2:#dddddd; --chip:rgba(0,0,0,.04); --chip-h:rgba(0,0,0,.08);
  --card:rgba(248,250,252,1); --card-shadow:0 0 0 1px #dddddd;
  --grid:rgba(0,0,0,.04);
  --glow-disp:none; --grain-disp:none; --glow-op:0;
  --panel-grad:linear-gradient(180deg,#ffffff,#f8fafc);
  --mark-fill:#181d26; --mark-color:#181d26;
  --act-bg:#181d26; --act-ink:#ffffff; --act-shadow:0 2px 8px -2px rgba(24,29,38,.30);
  --focus:#aa2d00;
  --amber:#aa2d00; --amber-soft:rgba(170,45,0,.12);
  --ok:#006400; --okrgb:0,100,0; --ok-ink:#004e00; --danger:#c0392b; --danger-soft:rgba(192,57,43,.12);
  --slack:#d97c3e; --telegram:#2e8f70; --discord:#b8941e;
  --slack-glow:rgba(217,124,62,.25); --telegram-glow:rgba(46,143,112,.22); --discord-glow:rgba(184,148,30,.22);
  --err-bg:rgba(255,240,238,.96); --err-ink:#7a1500; --good-bg:rgba(232,255,234,.96); --good-ink:#004e00;
}

  /* ---- Mastercard ---- */
[data-design='mastercard']{
  --f-display:'Sofia Sans',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Sofia Sans',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Sofia Sans',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:40px; --card:none; --card-shadow:0 24px 48px 0 rgba(0,0,0,0.25);
  --lbl-transform:uppercase; --lbl-spacing:.04em;
  --mark-grad:none; --mark-fill:#F3F0EE; --mark-color:#F3F0EE; --mark-size:46px;
  --act-bg:#CF4500; --act-ink:#FFFFFF; --act-radius:20px; --act-shadow:0 8px 24px -8px rgba(207,69,0,0.55);
  --focus:#CF4500; --base-fs:15px;
  --bg:#141413;
  --page-bg:linear-gradient(180deg,#141413,#1a1918);
  --grid:rgba(243,240,238,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(207,69,0,0); --glow-b:rgba(243,115,56,0);
  --panel:#1C1C1B; --panel-2:#242422; --ink:#F3F0EE; --muted:#9A8F86; --faint:#7A726C;
  --line:rgba(243,240,238,.07); --line-2:rgba(243,240,238,.14); --chip:rgba(243,240,238,.05); --chip-h:rgba(243,240,238,.10);
  --amber:#CF4500; --amber-soft:rgba(207,69,0,.18);
  --ok:#48d39a; --okrgb:72,211,154; --ok-ink:#9ff0cf; --danger:#f56b73; --danger-soft:rgba(245,107,115,.2);
  --slack:#F37338; --telegram:#3860BE; --discord:#CF4500;
  --slack-glow:rgba(243,115,56,.42); --telegram-glow:rgba(56,96,190,.40); --discord-glow:rgba(207,69,0,.42);
  --on-accent:#FFFFFF;
  --panel-grad:linear-gradient(180deg,var(--panel),rgba(28,28,27,.55));
  --err-bg:rgba(40,10,4,.92); --err-ink:#ffb0a0; --good-bg:rgba(10,28,20,.92); --good-ink:#8ef0c4;
}
[data-design='mastercard'][data-theme='light']{
  --bg:#F3F0EE;
  --page-bg:#F3F0EE;
  --panel:#FCFBFA; --panel-2:#FFFFFF;
  --ink:#141413; --muted:#696969; --faint:#887e76;
  --line:rgba(20,20,19,.10); --line-2:rgba(20,20,19,.20); --chip:rgba(20,20,19,.05); --chip-h:rgba(20,20,19,.10);
  --mark-fill:#141413; --mark-color:#141413;
  --act-bg:#141413; --act-ink:#F3F0EE; --act-shadow:0 4px 24px 0 rgba(0,0,0,0.08);
  --grid:rgba(20,20,19,.04); --grain-op:0; --glow-op:0;
  --glow-a:rgba(207,69,0,0); --glow-b:rgba(243,115,56,0);
  --card:none; --card-shadow:0 24px 48px 0 rgba(0,0,0,0.08);
  --panel-grad:linear-gradient(180deg,var(--panel),rgba(252,251,250,.55));
  --err-bg:rgba(255,240,235,.95); --err-ink:#9A3A0A; --good-bg:rgba(230,248,240,.95); --good-ink:#1a5c3a;
  --amber-soft:rgba(207,69,0,.12);
  --slack-glow:rgba(243,115,56,.30); --telegram-glow:rgba(56,96,190,.28); --discord-glow:rgba(207,69,0,.30);
}

  /* ---- NVIDIA ---- */
[data-design='nvidia']{
  --f-display:'Inter',ui-sans-serif,system-ui,Arial,sans-serif;
  --f-body:'Inter',ui-sans-serif,system-ui,Arial,sans-serif;
  --f-label:'Inter',ui-sans-serif,system-ui,Arial,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:2px; --card:none; --card-shadow:0 0 0 1px #5e5e5e;
  --lbl-transform:uppercase; --lbl-spacing:.04em;
  --mark-grad:none; --mark-fill:#76b900; --mark-color:#76b900; --mark-size:46px;
  --act-bg:#76b900; --act-ink:#000000; --act-radius:2px; --act-shadow:none;
  --focus:#76b900; --base-fs:15px;
  --bg:#000000;
  --page-bg:linear-gradient(180deg,#000000,#000000);
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(118,185,0,.0); --glow-b:rgba(118,185,0,.0);
  --panel:#1a1a1a; --panel-2:#111111; --ink:#ffffff; --muted:rgba(255,255,255,0.7); --faint:#898989;
  --line:rgba(255,255,255,.12); --line-2:#5e5e5e; --chip:rgba(255,255,255,.06); --chip-h:rgba(255,255,255,.12);
  --amber:#76b900; --amber-soft:rgba(118,185,0,.18);
  --ok:#76b900; --okrgb:118,185,0; --ok-ink:#bff230; --danger:#e52020; --danger-soft:rgba(229,32,32,.18);
  --slack:#bff230; --telegram:#5a8d00; --discord:#76b900;
  --slack-glow:rgba(191,242,48,.35); --telegram-glow:rgba(90,141,0,.35); --discord-glow:rgba(118,185,0,.35);
  --on-accent:#000000;
  --panel-grad:linear-gradient(180deg,#1a1a1a,#111111);
  --err-bg:rgba(40,0,0,.95); --err-ink:#ff9999; --good-bg:rgba(0,20,0,.95); --good-ink:#bff230;
}
[data-design='nvidia'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:linear-gradient(180deg,#ffffff,#f7f7f7);
  --panel:#ffffff; --panel-2:#f7f7f7; --ink:#000000; --muted:#757575; --faint:#898989;
  --line:#cccccc; --line-2:#5e5e5e; --chip:rgba(0,0,0,.05); --chip-h:rgba(0,0,0,.10);
  --amber:#76b900; --amber-soft:rgba(118,185,0,.14);
  --card:none; --card-shadow:0 0 0 1px #cccccc;
  --act-bg:#76b900; --act-ink:#000000; --act-shadow:none;
  --focus:#76b900;
  --mark-fill:#000000; --mark-color:#000000;
  --ok:#3f8500; --okrgb:63,133,0; --ok-ink:#3f8500; --danger:#e52020; --danger-soft:rgba(229,32,32,.12);
  --slack:#5a8d00; --telegram:#3f8500; --discord:#76b900;
  --slack-glow:rgba(90,141,0,.30); --telegram-glow:rgba(63,133,0,.30); --discord-glow:rgba(118,185,0,.30);
  --on-accent:#000000;
  --panel-grad:linear-gradient(180deg,#ffffff,#f7f7f7);
  --glow-op:0; --grain-op:0;
  --err-bg:rgba(255,235,235,.95); --err-ink:#650b0b; --good-bg:rgba(235,255,230,.95); --good-ink:#3f8500;
}

  /* ---- Tesla ---- */
[data-design='tesla']{
  --f-display:'Plus Jakarta Sans',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Manrope',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Manrope',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:4px; --card:rgba(255,255,255,.04); --card-shadow:0 0 0 0 transparent;
  --lbl-transform:none; --lbl-spacing:normal;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:44px;
  --act-bg:#3E6AE1; --act-ink:#ffffff; --act-radius:4px; --act-shadow:0 0 0 0 transparent;
  --focus:#3E6AE1; --base-fs:15px;
  --bg:#171A20;
  --page-bg:#171A20;
  --grid:rgba(255,255,255,.0); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(62,106,225,.0); --glow-b:rgba(62,106,225,.0);
  --panel:#1f2329; --panel-2:#252a32; --ink:#f5f5f5; --muted:#9a9c9f; --faint:#6c6e72;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.13); --chip:rgba(255,255,255,.05); --chip-h:rgba(255,255,255,.10);
  --amber:#3E6AE1; --amber-soft:rgba(62,106,225,.18);
  --ok:#3E6AE1; --okrgb:62,106,225; --ok-ink:#a8c0f5; --danger:#d94f4f; --danger-soft:rgba(217,79,79,.2);
  --slack:#3E6AE1; --telegram:#5c82e8; --discord:#7a9dee;
  --slack-glow:rgba(62,106,225,.40); --telegram-glow:rgba(92,130,232,.38); --discord-glow:rgba(122,157,238,.38);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(31,35,41,.6));
  --err-bg:rgba(30,10,10,.92); --err-ink:#ffaaaa; --good-bg:rgba(10,16,36,.92); --good-ink:#a8c0f5;
}
[data-design='tesla'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:#ffffff;
  --panel:#ffffff; --panel-2:#f4f4f4; --ink:#171A20; --muted:#5c5e62; --faint:#888888;
  --line:rgba(0,0,0,.09); --line-2:rgba(0,0,0,.15); --chip:rgba(0,0,0,.04); --chip-h:rgba(0,0,0,.08);
  --mark-fill:#171A20; --mark-color:#171A20;
  --act-bg:#3E6AE1; --act-ink:#ffffff; --act-shadow:0 0 0 0 transparent;
  --amber:#3E6AE1; --amber-soft:rgba(62,106,225,.14);
  --ok:#3E6AE1; --okrgb:62,106,225; --ok-ink:#2a50c4; --danger:#c0392b; --danger-soft:rgba(192,57,43,.15);
  --glow-disp:none; --grain-disp:none; --glow-op:0;
  --glow-a:rgba(62,106,225,.0); --glow-b:rgba(62,106,225,.0);
  --grid:rgba(0,0,0,.0); --grain-op:0;
  --card:rgba(0,0,0,.02); --card-shadow:0 0 0 0 transparent;
  --panel-grad:linear-gradient(180deg, #ffffff, #f4f4f4);
  --slack:#3E6AE1; --telegram:#2a50c4; --discord:#1e3dae;
  --slack-glow:rgba(62,106,225,.30); --telegram-glow:rgba(42,80,196,.28); --discord-glow:rgba(30,61,174,.28);
  --err-bg:rgba(255,240,240,.96); --err-ink:#c0392b; --good-bg:rgba(235,241,255,.96); --good-ink:#1e3dae;
}

  /* ---- Discord ---- */
[data-design='discord']{
  --f-display:'Hanken Grotesk',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Plus Jakarta Sans',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Plus Jakarta Sans',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:block; --grain-disp:none;
  --route-radius:16px; --card:linear-gradient(180deg, rgba(94,101,242,.07), rgba(30,35,83,.18)); --card-shadow:0 3px 68px rgba(69,42,124,.18);
  --lbl-transform:uppercase; --lbl-spacing:.08em;
  --mark-grad:linear-gradient(92deg,#8b96ff,#ec48bd 62%,#35ed7e); --mark-fill:transparent; --mark-color:transparent; --mark-size:48px;
  --act-bg:#5865f2; --act-ink:#ffffff; --act-radius:12px; --act-shadow:0 8px 28px -8px rgba(88,101,242,.72);
  --focus:#5865f2; --base-fs:15px;
  --bg:#0a0d3a;
  --page-bg:radial-gradient(900px 560px at 15% -10%, rgba(88,101,242,.28), transparent 55%), radial-gradient(700px 400px at 85% 30%, rgba(236,72,189,.18), transparent 55%), linear-gradient(180deg,#0a0d3a,#0e1145);
  --grid:rgba(255,255,255,.018); --grain-op:0; --grain-blend:overlay; --glow-op:.55;
  --glow-a:rgba(88,101,242,.38); --glow-b:rgba(236,72,189,.22);
  --panel:#1e2353; --panel-2:#23272a; --ink:#ffffff; --muted:#a9afd4; --faint:#6b72a8;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.14); --chip:rgba(88,101,242,.12); --chip-h:rgba(88,101,242,.22);
  --amber:#5865f2; --amber-soft:rgba(88,101,242,.22);
  --ok:#35ed7e; --okrgb:53,237,126; --ok-ink:#0a2b18; --danger:#ec48bd; --danger-soft:rgba(236,72,189,.22);
  --slack:#e8b84b; --telegram:#00b0f4; --discord:#5865f2;
  --slack-glow:rgba(232,184,75,.42); --telegram-glow:rgba(0,176,244,.40); --discord-glow:rgba(88,101,242,.52);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(30,35,83,.60));
  --err-bg:rgba(40,8,36,.92); --err-ink:#f9a8e8; --good-bg:rgba(8,36,20,.92); --good-ink:#7dfab6;
}
[data-design='discord'][data-theme='light']{
  --bg:#f0f1fe;
  --page-bg:radial-gradient(800px 480px at 10% -8%, rgba(88,101,242,.12), transparent 55%), radial-gradient(600px 360px at 88% 20%, rgba(236,72,189,.08), transparent 52%), linear-gradient(180deg,#eef0fd,#f5f5ff);
  --panel:#ffffff; --panel-2:#e8eafd;
  --ink:#0a0d3a; --muted:#3d4270; --faint:#7278b0;
  --line:rgba(10,13,58,.09); --line-2:rgba(10,13,58,.16); --chip:rgba(88,101,242,.09); --chip-h:rgba(88,101,242,.16);
  --amber-soft:rgba(88,101,242,.16);
  --glow-a:rgba(88,101,242,.20); --glow-b:rgba(236,72,189,.12); --glow-op:.40;
  --grid:rgba(10,13,58,.025);
  --card:linear-gradient(180deg, rgba(255,255,255,.95), rgba(232,234,253,.60)); --card-shadow:0 3px 32px rgba(88,101,242,.10);
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(232,234,253,.70));
  --ok-ink:#07391a; --err-bg:rgba(255,240,252,.96); --err-ink:#a0177a; --good-bg:rgba(236,255,244,.96); --good-ink:#0b5c2a;
  --act-shadow:0 6px 20px -6px rgba(88,101,242,.55);
  --mark-grad:linear-gradient(92deg,#4049b8,#b82d8f 55%,#178a4e);
  --on-accent:#ffffff;
}

  /* ---- Nike ---- */
[data-design='nike']{
  --f-display:'Bebas Neue',ui-sans-serif,system-ui,sans-serif;
  --f-body:'Inter',ui-sans-serif,system-ui,sans-serif;
  --f-label:'Inter',ui-sans-serif,system-ui,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:0px; --card:none; --card-shadow:none;
  --lbl-transform:uppercase; --lbl-spacing:.02em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:40px;
  --act-bg:#ffffff; --act-ink:#111111; --act-radius:9999px; --act-shadow:none;
  --focus:#ffffff; --base-fs:15px;
  --bg:#111111;
  --page-bg:#111111;
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(255,255,255,0); --glow-b:rgba(255,255,255,0);
  --panel:#1a1a1a; --panel-2:#222222; --ink:#ffffff; --muted:#9e9ea0; --faint:#4b4b4d;
  --line:rgba(255,255,255,.10); --line-2:rgba(255,255,255,.18); --chip:rgba(255,255,255,.06); --chip-h:rgba(255,255,255,.12);
  --amber:#ffffff; --amber-soft:rgba(255,255,255,.12);
  --ok:#1eaa52; --okrgb:30,170,82; --ok-ink:#8ef0c4; --danger:#d30005; --danger-soft:rgba(211,0,5,.18);
  --slack:#e8b84b; --telegram:#1151ff; --discord:#0a7281;
  --slack-glow:rgba(232,184,75,.35); --telegram-glow:rgba(17,81,255,.35); --discord-glow:rgba(10,114,129,.35);
  --on-accent:#111111;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(26,26,26,.82));
  --err-bg:rgba(60,0,2,.92); --err-ink:#ffb0b4; --good-bg:rgba(0,30,16,.92); --good-ink:#8ef0c4;
}
[data-design='nike'][data-theme='light']{
  --bg:#ffffff;
  --page-bg:#ffffff;
  --panel:#f5f5f5; --panel-2:#ebebeb; --ink:#111111; --muted:#707072; --faint:#828283;
  --line:rgba(0,0,0,.10); --line-2:rgba(0,0,0,.18); --chip:rgba(0,0,0,.05); --chip-h:rgba(0,0,0,.10);
  --mark-fill:#111111; --mark-color:#111111;
  --amber:#111111; --amber-soft:rgba(17,17,17,.10);
  --act-bg:#111111; --act-ink:#ffffff; --act-shadow:none;
  --focus:#111111;
  --on-accent:#ffffff;
  --grid:rgba(0,0,0,.04);
  --ok:#007d48; --okrgb:0,125,72; --ok-ink:#007d48;
  --danger:#d30005; --danger-soft:rgba(211,0,5,.12);
  --slack:#c89a00; --telegram:#0034e3; --discord:#0a7281;
  --slack-glow:rgba(200,154,0,.30); --telegram-glow:rgba(0,52,227,.30); --discord-glow:rgba(10,114,129,.30);
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(245,245,245,.82));
  --err-bg:rgba(255,235,235,.95); --err-ink:#780700; --good-bg:rgba(230,255,242,.95); --good-ink:#007d48;
}

  /* ---- Notion ---- */
[data-design='notion']{
  --f-display:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  --f-body:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  --f-label:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  --glow-disp:none; --grain-disp:none;
  --route-radius:12px; --card:linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02)); --card-shadow:0 0 0 1px rgba(255,255,255,.08), 0 1px 3px rgba(0,0,0,.24), 0 4px 12px rgba(0,0,0,.18);
  --lbl-transform:none; --lbl-spacing:0em;
  --mark-grad:none; --mark-fill:#ffffff; --mark-color:#ffffff; --mark-size:44px;
  --act-bg:#0075de; --act-ink:#ffffff; --act-radius:9999px; --act-shadow:0 2px 8px rgba(0,117,222,.35);
  --focus:#0075de; --base-fs:15px;
  --bg:#181d2e;
  --page-bg:linear-gradient(180deg,#202a4d 0%,#181d2e 42%,#12151f 100%);
  --grid:rgba(255,255,255,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(0,117,222,.0); --glow-b:rgba(33,49,131,.0);
  --panel:#202744; --panel-2:#28305a; --ink:#f2f1f4; --muted:#9aa0c0; --faint:#5f6790;
  --line:rgba(255,255,255,.08); --line-2:rgba(255,255,255,.14); --chip:rgba(255,255,255,.06); --chip-h:rgba(255,255,255,.11);
  --amber:#0075de; --amber-soft:rgba(0,117,222,.20);
  --ok:#1aae39; --okrgb:26,174,57; --ok-ink:#7edd93; --danger:#f56b73; --danger-soft:rgba(245,107,115,.18);
  --slack:#dd5b00; --telegram:#62aef0; --discord:#2a9d99;
  --slack-glow:rgba(221,91,0,.40); --telegram-glow:rgba(98,174,240,.38); --discord-glow:rgba(42,157,153,.40);
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg, var(--panel), rgba(32,39,68,.60));
  --err-bg:rgba(30,10,12,.92); --err-ink:#ffb0b4; --good-bg:rgba(8,24,14,.92); --good-ink:#7edd93;
}
[data-design='notion'][data-theme='light']{
  --bg:#f6f5f4;
  --page-bg:#f6f5f4;
  --grid:rgba(0,0,0,.04); --grain-op:0; --grain-blend:normal; --glow-op:0;
  --glow-a:rgba(0,117,222,.0); --glow-b:rgba(33,49,131,.0);
  --panel:#ffffff; --panel-2:#f6f5f4; --ink:#000000; --muted:#615d59; --faint:#86827d;
  --line:rgba(0,0,0,.10); --line-2:rgba(0,0,0,.16); --chip:rgba(0,0,0,.05); --chip-h:rgba(0,0,0,.09);
  --amber:#0075de; --amber-soft:rgba(0,117,222,.14);
  --card:linear-gradient(180deg,rgba(255,255,255,1),rgba(255,255,255,1)); --card-shadow:0 0 0 1px rgba(0,0,0,.08), 0 0.175px 1.04px rgba(0,0,0,.01), 0 0.8px 2.93px rgba(0,0,0,.02), 0 2.03px 7.85px rgba(0,0,0,.027), 0 4px 18px rgba(0,0,0,.04);
  --act-bg:#0075de; --act-ink:#ffffff; --act-radius:9999px; --act-shadow:0 2px 10px rgba(0,117,222,.28);
  --mark-fill:#000000; --mark-color:#000000;
  --on-accent:#ffffff;
  --panel-grad:linear-gradient(180deg,#ffffff,rgba(246,245,244,.70));
  --ok:#1aae39; --okrgb:26,174,57; --ok-ink:#0d7d27; --danger:#cc2e35; --danger-soft:rgba(204,46,53,.12);
  --slack:#dd5b00; --telegram:#62aef0; --discord:#2a9d99;
  --slack-glow:rgba(221,91,0,.30); --telegram-glow:rgba(98,174,240,.28); --discord-glow:rgba(42,157,153,.30);
  --err-bg:rgba(255,240,240,.96); --err-ink:#9b1c22; --good-bg:rgba(235,255,240,.96); --good-ink:#0d5c1e;
}

  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{
    margin:0; min-height:100vh; color:var(--ink); font-family:var(--f-body);
    font-size:var(--base-fs); line-height:1.55; -webkit-font-smoothing:antialiased;
    background:var(--page-bg); background-attachment:fixed;
    transition:background .3s ease, color .3s ease;
  }
  body::before{
    content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
    background-image:linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size:48px 48px;
    -webkit-mask-image:radial-gradient(circle at 50% 26%, #000 0%, transparent 78%);
    mask-image:radial-gradient(circle at 50% 26%, #000 0%, transparent 78%);
  }
  [data-design="nothing"] body::before{-webkit-mask-image:none; mask-image:none}
  .grain{position:fixed; inset:0; z-index:1; pointer-events:none; display:var(--grain-disp); opacity:var(--grain-op); mix-blend-mode:var(--grain-blend);
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
  .glow{position:fixed; z-index:0; pointer-events:none; display:var(--glow-disp); filter:blur(64px); opacity:var(--glow-op); border-radius:50%}
  .glow.a{width:400px;height:400px;left:-130px;top:-70px;background:var(--glow-a);animation:drift 26s ease-in-out infinite}
  .glow.b{width:440px;height:440px;right:-160px;bottom:-140px;background:var(--glow-b);animation:drift 32s ease-in-out infinite reverse}
  @keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(40px,30px)}}

  .wrap{position:relative; z-index:2; max-width:1000px; margin:0 auto; padding:48px 22px 100px}

  .hud{display:flex; align-items:flex-end; justify-content:space-between; gap:12px; flex-wrap:wrap; padding-bottom:22px; border-bottom:1px solid var(--line-2)}
  .brand{display:flex; align-items:baseline; gap:11px; flex-wrap:wrap}
  .mark{font-family:var(--f-display); font-weight:800; font-size:var(--mark-size); letter-spacing:-.02em; line-height:.9;
    background:var(--mark-grad); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:var(--mark-fill); color:var(--mark-color)}
  .mark .glyph{-webkit-text-fill-color:var(--amber); margin-right:9px}
  [data-design="nothing"] .mark{-webkit-background-clip:border-box; background-clip:border-box}
  [data-design="nothing"] .mark .glyph{display:none}
  .tagline{font-family:var(--f-label); color:var(--muted); font-size:13px; letter-spacing:.2em; text-transform:uppercase}
  .meta{display:flex; align-items:center; gap:6px; font-family:var(--f-label); font-size:12px; color:var(--muted)}
  .pill{display:inline-flex; align-items:center; gap:7px; padding:6px 12px; border:1px solid var(--line-2); border-radius:999px; background:var(--chip); white-space:nowrap; letter-spacing:.06em}
  .pill.btn-pill{font-family:var(--f-label); color:var(--ink); cursor:pointer; text-transform:uppercase; transition:background .15s, border-color .15s}
  .pill.btn-pill:hover{background:var(--chip-h); border-color:var(--focus)}
  .pill.auth{color:var(--ok-ink); border-color:rgba(var(--okrgb),.34); background:rgba(var(--okrgb),.08); text-transform:uppercase}
  .pill .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
  .count{font-family:var(--f-label); font-variant-numeric:tabular-nums; color:var(--ink); padding:6px 12px; border:1px solid var(--line-2); border-radius:999px; background:var(--chip); text-transform:uppercase; letter-spacing:.06em}

  .legend{display:flex; gap:18px; flex-wrap:wrap; margin:20px 0 4px; font-family:var(--f-label); font-size:12px; color:var(--faint); letter-spacing:.06em; text-transform:uppercase}
  .legend span{display:inline-flex; align-items:center; gap:7px}
  .legend i{width:9px;height:9px;border-radius:50%;display:inline-block}
  .k-slack{background:var(--slack)} .k-telegram{background:var(--telegram)} .k-discord{background:var(--discord)}
  [data-design="amber"] .k-slack{box-shadow:0 0 10px var(--slack-glow)} [data-design="amber"] .k-telegram{box-shadow:0 0 10px var(--telegram-glow)} [data-design="amber"] .k-discord{box-shadow:0 0 10px var(--discord-glow)}
  [data-design="nothing"] .k-slack{border-radius:0} [data-design="nothing"] .k-telegram{border-radius:0; background:repeating-linear-gradient(90deg,var(--ink) 0 2px,transparent 2px 4px)} [data-design="nothing"] .k-discord{border-radius:0; background:radial-gradient(circle,var(--ink) 1px,transparent 1.4px) 0 0/3px 3px}

  .controls{display:flex; gap:10px; flex-wrap:wrap; margin:20px 0}
  select,input{font-family:var(--f-body); font-size:14.5px; color:var(--ink); background:var(--panel); border:1px solid var(--line-2); border-radius:10px; padding:11px 13px; outline:none; transition:border-color .15s, box-shadow .15s, background .15s}
  [data-design="nothing"] select,[data-design="nothing"] input{border-radius:4px}
  select:focus,input:focus{border-color:var(--focus); box-shadow:0 0 0 3px var(--amber-soft); background:var(--panel-2)}
  input::placeholder{color:var(--faint)}
  .controls #fUser{min-width:190px; flex:1; max-width:380px}
  .btn{font-family:var(--f-label); font-size:13.5px; cursor:pointer; border-radius:10px; padding:11px 17px; border:1px solid var(--line-2); background:var(--chip); color:var(--ink); text-transform:uppercase; letter-spacing:.06em; transition:transform .12s, border-color .15s, background .15s, filter .15s}
  [data-design="nothing"] .btn{border-radius:999px}
  .btn:hover{background:var(--chip-h); transform:translateY(-1px)}
  .btn.primary{background:var(--act-bg); border-color:transparent; color:var(--act-ink); border-radius:var(--act-radius); font-weight:700; box-shadow:var(--act-shadow)}
  .btn.primary:hover{filter:brightness(1.06)}
  :focus-visible{outline:2px solid var(--focus); outline-offset:2px}

  .msg{position:fixed; left:50%; bottom:28px; transform:translate(-50%,16px); z-index:30; font-family:var(--f-label);
    padding:13px 20px; border-radius:12px; font-size:13.5px; letter-spacing:.04em; border:1px solid transparent;
    opacity:0; pointer-events:none; box-shadow:0 18px 48px -14px rgba(0,0,0,.55);
    -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px); transition:.3s cubic-bezier(.2,.7,.2,1)}
  .msg.show{opacity:1; transform:translate(-50%,0)}
  .msg.err{background:var(--err-bg); border-color:color-mix(in srgb, var(--danger) 55%, transparent); color:var(--err-ink)}
  .msg.good{background:var(--good-bg); border-color:color-mix(in srgb, var(--ok) 50%, transparent); color:var(--good-ink)}
  [data-design="nothing"] .msg{position:static; left:auto; bottom:auto; transform:none; margin:16px 0 0; padding:10px 0 0; border:0; border-top:1px solid var(--line-2); border-radius:0; box-shadow:none; -webkit-backdrop-filter:none; backdrop-filter:none; max-width:none; max-height:0; overflow:hidden; transition:opacity .2s}
  [data-design="nothing"] .msg.show{transform:none; max-height:60px}
  [data-design="nothing"] .msg.show::before{content:"[ "} [data-design="nothing"] .msg.show::after{content:" ]"}

  .board{display:flex; flex-direction:column; gap:11px; margin-top:10px}
  [data-design="nothing"] .board{gap:0}
  .route{--accent:var(--muted); --glow:rgba(0,0,0,.1);
    display:grid; grid-template-columns:146px 1fr auto; align-items:center; gap:16px;
    padding:16px 19px; border:1px solid var(--line); border-radius:var(--route-radius); background:var(--card); box-shadow:var(--card-shadow);
    position:relative; overflow:hidden; transition:transform .18s, border-color .18s, box-shadow .25s}
  [data-design="amber"] .route{animation:rise .5s cubic-bezier(.2,.7,.2,1) both; animation-delay:calc(var(--i,0)*55ms)}
  [data-design="nothing"] .route{border:0; border-bottom:1px solid var(--line); border-radius:0; padding:18px 8px 18px 22px}
  [data-design="nothing"] .board .route:first-child{border-top:1px solid var(--line)}
  .route::before{content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--accent); box-shadow:0 0 16px var(--glow)}
  [data-design="nothing"] .route::before{box-shadow:none; width:4px; background:var(--ink)}
  [data-design="nothing"] .route.s-telegram::before{background:repeating-linear-gradient(180deg,var(--ink) 0 3px,transparent 3px 7px)}
  [data-design="nothing"] .route.s-discord::before{background:radial-gradient(circle,var(--ink) 1.3px,transparent 1.7px) 0 0/4px 6px}
  .route:hover{transform:translateX(5px); border-color:var(--line-2); box-shadow:0 14px 44px -20px var(--glow)}
  [data-design="nothing"] .route:hover{transform:none; background:var(--chip-h); box-shadow:none}
  .route.editing{transform:none; border-color:color-mix(in srgb, var(--accent) 55%, var(--line-2)); box-shadow:0 0 0 1px color-mix(in srgb, var(--accent) 30%, transparent), 0 14px 40px -22px var(--glow)}
  [data-design="nothing"] .route.editing{background:var(--chip-h); box-shadow:none}
  .route.s-slack{--accent:var(--slack); --glow:var(--slack-glow)} .route.s-telegram{--accent:var(--telegram); --glow:var(--telegram-glow)} .route.s-discord{--accent:var(--discord); --glow:var(--discord-glow)}
  .node{display:flex; align-items:center; gap:11px}
  .pin{width:10px;height:10px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--glow),0 0 0 4px color-mix(in srgb,var(--accent) 10%, transparent)}
  [data-design="nothing"] .pin{border-radius:0; width:8px; height:8px; background:var(--ink); box-shadow:none}
  .badge{font-family:var(--f-label); font-size:12.5px; font-weight:600; letter-spacing:.04em; text-transform:lowercase; color:var(--accent); padding:3px 11px; border-radius:7px; border:1px solid color-mix(in srgb, var(--accent) 42%, transparent); background:color-mix(in srgb, var(--accent) 15%, transparent)}
  [data-design="nothing"] .badge{color:var(--ink); border:1px solid var(--line-2); background:transparent; border-radius:0; text-transform:uppercase; letter-spacing:.12em; font-weight:400}
  .path{display:flex; align-items:center; gap:13px; min-width:0}
  .user{font-weight:600; color:var(--ink); white-space:nowrap}
  .wire{flex:1 1 auto; min-width:40px; height:2px; border-radius:2px; position:relative; background:linear-gradient(90deg, transparent, var(--accent), transparent); background-size:200% 100%; opacity:.55; animation:flow 2.6s linear infinite}
  .route:hover .wire{opacity:.9}
  .wire::after{content:"\25B8"; position:absolute; right:-5px; top:50%; transform:translateY(-52%); color:var(--accent); font-size:12px}
  [data-design="nothing"] .wire{animation:none; background:var(--line-2); opacity:1} [data-design="nothing"] .wire::after{color:var(--muted)}
  .route.editing .wire{display:none}
  .chan{color:var(--muted); word-break:break-all; max-width:46%}
  .chan-edit{flex:1 1 auto; min-width:130px; font-family:var(--f-body); font-size:14.5px; color:var(--ink); background:var(--panel-2); border:1px solid var(--accent); border-radius:8px; padding:7px 11px; outline:none; box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent)}
  [data-design="nothing"] .chan-edit{border-radius:4px; border-color:var(--ink); box-shadow:none}
  .ops{display:flex; gap:8px}
  .op{font-family:var(--f-label); font-size:13px; cursor:pointer; padding:7px 13px; border-radius:8px; border:1px solid var(--line-2); background:transparent; color:var(--muted); transition:.14s; white-space:nowrap; text-transform:uppercase; letter-spacing:.05em}
  [data-design="nothing"] .op{border-radius:999px}
  .op.edit:hover{color:var(--ink); border-color:var(--accent); background:color-mix(in srgb,var(--accent) 18%,transparent)}
  .op.del:hover{color:#fff; border-color:var(--danger); background:var(--danger-soft)}
  [data-design="nothing"] .op.del:hover{color:var(--danger); background:transparent; border-color:var(--danger)}
  .op.save{color:var(--on-accent); background:var(--accent); border-color:transparent; font-weight:700}
  [data-design="nothing"] .op.save{background:var(--ink); color:var(--bg)}
  .op.save:hover{filter:brightness(1.08)}
  .op.cancel:hover{color:var(--ink); border-color:var(--line-2); background:var(--chip-h)}
  @keyframes flow{0%{background-position:200% 0}100%{background-position:-200% 0}}
  @keyframes rise{from{opacity:0; transform:translateY(14px)}to{opacity:1; transform:none}}

  .empty{padding:58px 20px; text-align:center; color:var(--muted); border:1px dashed var(--line-2); border-radius:14px}
  [data-design="nothing"] .empty{border-radius:0; border-style:solid; border-width:1px 0}
  .empty-mark{display:block; font-size:36px; color:var(--faint); margin-bottom:12px; font-family:var(--f-display)}

  .patch{margin-top:28px; border:1px solid var(--line-2); border-radius:16px; padding:22px; background:var(--panel-grad); box-shadow:var(--card-shadow)}
  [data-design="nothing"] .patch{border-radius:0; border-width:1px 0 0; padding:22px 0 0; background:transparent}
  .patch h2{font-family:var(--f-display); font-weight:700; font-size:17px; letter-spacing:.02em; margin:0 0 4px; color:var(--ink); display:flex; align-items:center; gap:9px}
  [data-design="nothing"] .patch h2{font-family:var(--f-label); font-size:13px; letter-spacing:.16em; text-transform:uppercase}
  .patch h2 .plus{color:var(--amber); font-size:21px}
  [data-design="nothing"] .patch h2 .plus{color:var(--danger)}
  .patch .hint{font-family:var(--f-label); color:var(--faint); font-size:12.5px; margin:0 0 18px; letter-spacing:.04em}
  .grid{display:grid; grid-template-columns:160px 1fr 1.4fr auto; gap:13px; align-items:end}
  .field label{display:block; font-family:var(--f-label); font-size:11.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--faint); margin-bottom:7px}
  .field select,.field input{width:100%}

  footer{margin-top:32px; text-align:center; font-family:var(--f-label); color:var(--faint); font-size:11.5px; letter-spacing:.1em; text-transform:uppercase}

  .modal-overlay{position:fixed; inset:0; z-index:100; display:none; align-items:center; justify-content:center; padding:24px; background:rgba(0,0,0,.62); -webkit-backdrop-filter:blur(5px); backdrop-filter:blur(5px)}
  .modal-overlay.show{display:flex}
  [data-design="nothing"] .modal-overlay{background:rgba(0,0,0,.84); -webkit-backdrop-filter:none; backdrop-filter:none}
  [data-design="nothing"][data-theme="light"] .modal-overlay{background:rgba(28,28,28,.5)}
  .modal{width:min(380px,100%); background:var(--panel); border:1px solid var(--line-2); border-radius:16px; padding:26px 24px; box-shadow:0 30px 80px -20px rgba(0,0,0,.7)}
  [data-design="nothing"] .modal{border-radius:4px; box-shadow:none; background:var(--bg)}
  .modal .lock{font-family:var(--f-label); color:var(--faint); font-size:11px; letter-spacing:.16em; text-transform:uppercase; margin-bottom:16px; display:flex; align-items:center; gap:8px}
  .modal h3{font-family:var(--f-display); font-weight:800; font-size:28px; margin:0 0 4px; color:var(--ink); letter-spacing:-.01em}
  .modal p{font-family:var(--f-label); color:var(--muted); font-size:12px; margin:0 0 18px; letter-spacing:.04em; text-transform:uppercase}
  .modal input{width:100%; margin-bottom:12px}
  .modal-err{color:var(--danger); font-family:var(--f-label); font-size:12px; min-height:15px; margin:0 0 12px; letter-spacing:.03em}
  .modal-row{display:flex; gap:10px} .modal-row .btn{flex:1; text-align:center}
  button.pill.auth{font-family:var(--f-label); font-size:12px; cursor:pointer}

  .guide-overlay{z-index:120; align-items:flex-start; overflow:auto; padding:5vh 18px}
  .guide-card{width:min(760px,100%); margin:auto; background:var(--panel); border:1px solid var(--line-2); border-radius:16px; padding:24px; box-shadow:0 30px 80px -20px rgba(0,0,0,.7)}
  [data-design="nothing"] .guide-card{border-radius:4px; box-shadow:none; background:var(--bg)}
  .guide-head{display:flex; justify-content:space-between; align-items:flex-start; gap:14px}
  .guide-head h3{font-family:var(--f-display); font-weight:800; font-size:25px; margin:3px 0 0; color:var(--ink); letter-spacing:-.01em}
  .guide-intro{font-family:var(--f-body); color:var(--muted); font-size:13.5px; line-height:1.6; margin:12px 0 16px}
  .guide-intro b{color:var(--ink)}
  .guide-field{display:flex; align-items:center; gap:12px; margin:0 0 16px}
  .guide-field span{font-family:var(--f-label); font-size:11.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--faint); white-space:nowrap}
  .guide-field input{flex:1; max-width:240px}
  .os-tabs{display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap}
  .os-tab{font-family:var(--f-label); font-size:12.5px; cursor:pointer; border-radius:999px; padding:8px 16px; border:1px solid var(--line-2); background:var(--chip); color:var(--muted); text-transform:uppercase; letter-spacing:.06em; transition:background .15s,color .15s,border-color .15s}
  [data-design="nothing"] .os-tab{border-radius:4px}
  .os-tab:hover{background:var(--chip-h)}
  .os-tab.active{background:var(--act-bg); color:var(--act-ink); border-color:transparent; font-weight:700}
  .os-panel{display:none}
  .os-panel.active{display:block}
  .step{font-family:var(--f-label); font-size:11.5px; letter-spacing:.05em; color:var(--faint); line-height:1.5; margin:2px 0 8px}
  .code{position:relative; margin:0}
  .code pre{font-family:var(--f-label); font-size:12.5px; line-height:1.65; color:var(--ink); background:var(--panel-2); border:1px solid var(--line-2); border-radius:10px; padding:14px 15px; padding-right:72px; overflow-x:auto; white-space:pre; margin:0}
  [data-design="nothing"] .code pre{border-radius:4px}
  .copy{position:absolute; top:9px; right:9px; font-family:var(--f-label); font-size:11px; cursor:pointer; border-radius:8px; padding:6px 11px; border:1px solid var(--line-2); background:var(--chip); color:var(--ink); text-transform:uppercase; letter-spacing:.05em; transition:background .15s,color .15s}
  [data-design="nothing"] .copy{border-radius:4px}
  .copy:hover{background:var(--chip-h)}
  .copy.done{background:var(--act-bg); color:var(--act-ink); border-color:transparent}
  .guide-foot{font-family:var(--f-label); color:var(--faint); font-size:12px; line-height:1.6; letter-spacing:.02em; margin:16px 0 0; padding-top:14px; border-top:1px solid var(--line-2)}
  .guide-foot b{color:var(--muted)}
  .guide-link{display:block; margin-top:16px; text-align:center; font-family:var(--f-label); font-size:12px; letter-spacing:.04em; color:var(--muted); cursor:pointer; background:none; border:none; text-decoration:underline; width:100%}
  .guide-link:hover{color:var(--ink)}
  .tok{margin:18px 0 0; border-top:1px solid var(--line-2); padding-top:16px}
  .tok>summary{font-family:var(--f-label); font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); cursor:pointer; list-style:none; outline:none}
  .tok>summary::-webkit-details-marker{display:none}
  .tok>summary::before{content:'\25B8  '; color:var(--faint)}
  .tok[open]>summary::before{content:'\25BE  '}
  .tok>summary:hover{color:var(--ink)}
  .tok-intro{font-family:var(--f-body); color:var(--muted); font-size:12.5px; line-height:1.6; margin:12px 0 14px}
  .tok-intro b{color:var(--ink)}
  .tok-tabs{display:flex; gap:8px; margin:14px 0; flex-wrap:wrap}
  .tok-tab{font-family:var(--f-label); font-size:12.5px; cursor:pointer; border-radius:999px; padding:7px 15px; border:1px solid var(--line-2); background:var(--chip); color:var(--muted); text-transform:uppercase; letter-spacing:.06em; transition:background .15s,color .15s,border-color .15s}
  [data-design="nothing"] .tok-tab{border-radius:4px}
  .tok-tab:hover{background:var(--chip-h)}
  .tok-tab.active{background:var(--act-bg); color:var(--act-ink); border-color:transparent; font-weight:700}
  .tok-panel{display:none}
  .tok-panel.active{display:block}
  .tok-h{font-family:var(--f-label); font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--ink); margin:0 0 7px}
  .tok-h span{color:var(--muted); text-transform:none; letter-spacing:0; font-size:11px; margin-left:7px}
  .tok-steps{margin:0 0 9px; padding-left:18px; font-family:var(--f-body); font-size:12.5px; color:var(--muted); line-height:1.65}
  .tok-steps li{margin:0 0 4px}
  .tok-steps b{color:var(--ink)}
  .tok-sub{margin:5px 0 2px; padding-left:16px; list-style:disc; line-height:1.6}
  .tok-sub li{margin:0 0 3px}
  .tok code{font-family:var(--f-label); font-size:11px; background:var(--chip); padding:1px 5px; border-radius:5px; color:var(--ink)}
  .tok a{color:var(--muted); text-decoration:underline}
  .tok a:hover{color:var(--ink)}
  .tok-sec{font-family:var(--f-label); font-size:11px; color:var(--faint); line-height:1.6; letter-spacing:.02em; margin:8px 0 0}
  @media(max-width:680px){ .guide-field{flex-direction:column; align-items:flex-start; gap:6px} .guide-field input{max-width:100%; width:100%} .guide-card{padding:20px 16px} }

  @media(max-width:1000px){
    .tagline{display:none}
    .mark{font-size:min(var(--mark-size),42px)}
  }
  @media(max-width:820px){
    .grid{grid-template-columns:1fr 1fr; align-items:end}
    .grid > .field:nth-of-type(3){grid-column:1 / -1}
    .grid > button{grid-column:1 / -1}
  }
  @media(max-width:680px){
    .wrap{padding:34px 16px 88px}
    .meta{gap:7px; font-size:11px}
    .pill,.count{padding:5px 9px}
    .route{grid-template-columns:1fr auto; grid-template-areas:"node ops" "path path"; row-gap:13px; gap:12px}
    .node{grid-area:node} .ops{grid-area:ops; justify-content:flex-end} .path{grid-area:path; flex-wrap:wrap}
    .chan{max-width:64%} .grid{grid-template-columns:1fr} .mark{font-size:38px} .msg{max-width:calc(100vw - 32px)}
  }
  @media(prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important; scroll-behavior:auto!important}}
</style>
</head>
<body>
<div class="grain"></div>
<div class="glow a"></div>
<div class="glow b"></div>

<main class="wrap">
  <header class="hud">
    <div class="brand">
      <div class="mark"><span class="glyph">&#8651;</span>RELAY</div>
      <div class="tagline">signal routing</div>
    </div>
    <div class="meta">
      <select class="hud-select" id="designSel" aria-label="디자인 테마 선택">
        <option value="amber">Amber</option>
        <option value="nothing">Nothing</option>
        <option value="spotify">Spotify</option>
        <option value="apple">Apple</option>
        <option value="claude">Claude</option>
        <option value="bmw-m">BMW M</option>
        <option value="ollama">Ollama</option>
        <option value="figma">Figma</option>
        <option value="hp">HP</option>
        <option value="airtable">Airtable</option>
        <option value="mastercard">Mastercard</option>
        <option value="nvidia">NVIDIA</option>
        <option value="tesla">Tesla</option>
        <option value="discord">Discord</option>
        <option value="nike">Nike</option>
        <option value="notion">Notion</option>
      </select>
      <button class="pill btn-pill" id="themeBtn" type="button" aria-label="다크/라이트 모드 전환">dark</button>
      <button class="pill btn-pill" id="guideBtn" type="button" aria-label="클라이언트 설치 가이드">guide</button>
      <button class="pill auth" id="logoutBtn" type="button" title="클릭하면 로그아웃"><span class="dot"></span> authed</button>
      <span class="count" id="count">0 routes</span>
    </div>
  </header>

  <div class="legend">
    <span><i class="k-slack"></i> slack</span>
    <span><i class="k-telegram"></i> telegram</span>
    <span><i class="k-discord"></i> discord</span>
    <span style="color:var(--faint)">app &#9656; @user &#9472;&#9472;&#9656; channel</span>
  </div>

  <div class="controls">
    <select id="fApp" aria-label="앱 필터">
      <option value="">앱 전체</option><option value="slack">slack</option><option value="telegram">telegram</option><option value="discord">discord</option>
    </select>
    <input id="fUser" placeholder="username 검색…" aria-label="username 검색">
    <button class="btn" id="refresh">새로고침</button>
  </div>

  <div id="msg" class="msg" role="status" aria-live="polite"></div>

  <section class="board" id="board"></section>

  <section class="patch" id="patch">
    <h2><span class="plus">+</span> 새 라우트 연결</h2>
    <p class="hint">기존 라우트 수정은 각 행의 “수정” 버튼으로 그 자리에서 합니다.</p>
    <div class="grid">
      <div class="field"><label for="nApp">app</label>
        <select id="nApp"><option value="slack">slack</option><option value="telegram">telegram</option><option value="discord">discord</option></select>
      </div>
      <div class="field"><label for="nUser">username</label><input id="nUser" placeholder="minsu"></div>
      <div class="field"><label for="nChan">channel</label><input id="nChan" placeholder="#room · chat_id · channel_id"></div>
      <button class="btn primary" id="add">연결 &#9656;</button>
    </div>
  </section>

  <footer>hook_relay · Claude Code &#8594; slack / telegram / discord</footer>
</main>

<div class="modal-overlay" id="login" role="dialog" aria-modal="true" aria-label="관리자 암호">
  <form class="modal" id="loginForm">
    <div class="lock"><span style="width:8px;height:8px;background:var(--danger);display:inline-block"></span> web access · 20001</div>
    <h3>RELAY</h3>
    <p>관리자 암호를 입력하세요</p>
    <input id="loginPw" type="password" placeholder="암호" autocomplete="current-password" aria-label="암호">
    <div class="modal-err" id="loginErr" role="alert"></div>
    <div class="modal-row"><button class="btn primary" type="submit" id="loginGo">확인</button></div>
    <button type="button" class="guide-link" id="guideOpen">처음이신가요? 클라이언트 설치 가이드 보기 &#9656;</button>
  </form>
</div>

<div class="modal-overlay guide-overlay" id="guide" role="dialog" aria-modal="true" aria-label="클라이언트 설치 가이드">
  <div class="guide-card">
    <div class="guide-head">
      <div>
        <div class="lock"><span style="width:8px;height:8px;background:var(--danger);display:inline-block"></span> client setup &#183; hook</div>
        <h3>설치 가이드</h3>
      </div>
      <button class="btn" id="guideClose" type="button">닫기</button>
    </div>
    <p class="guide-intro">내 Claude Code 작업 알림(작업완료·입력대기)을 Slack/Telegram/Discord로 받습니다. OS를 고르고 명령을 복사해 실행하세요. 알림이 가려면 아래 보드(또는 운영자)에 <b>내 username &#9656; 채널</b> 매핑이 있어야 합니다.</p>
    <label class="guide-field"><span>내 username</span><input id="guideUser" placeholder="minsu" autocomplete="off" spellcheck="false" aria-label="내 username"></label>
    <div class="os-tabs" role="tablist" aria-label="운영체제 선택">
      <button class="os-tab" type="button" data-os="win" role="tab">Windows</button>
      <button class="os-tab" type="button" data-os="mac" role="tab">macOS</button>
      <button class="os-tab" type="button" data-os="linux" role="tab">Linux</button>
    </div>
    <div class="os-panel" data-os="win"><p class="step">PowerShell에서 실행 (Windows 기본 내장)</p><div class="code"><button class="copy" type="button">복사</button><pre data-cmd="win"></pre></div></div>
    <div class="os-panel" data-os="mac"><p class="step">터미널에서 실행 · jq 필요</p><div class="code"><button class="copy" type="button">복사</button><pre data-cmd="mac"></pre></div></div>
    <div class="os-panel" data-os="linux"><p class="step">터미널에서 실행 · jq 필요</p><div class="code"><button class="copy" type="button">복사</button><pre data-cmd="linux"></pre></div></div>
    <p class="guide-foot">설치 후 Claude Code(CLI)에서 작업을 끝내면 알림이 옵니다. 채널을 처음 연결하는 <b>운영자</b>는 아래 <b>봇 토큰 발급</b>을 펼쳐 보세요. 문제해결·환경변수 등 추가 레퍼런스는 배포본 <b>dist/GUIDE.md</b>에 있습니다.</p>
    <details class="tok">
      <summary>봇 토큰 발급 (운영자 전용) — Slack · Telegram · Discord</summary>
      <p class="tok-intro">봇 토큰은 <b>사용자가 아니라 중계 서버</b>가 보관합니다. 플랫폼(앱)별로 1개만 발급해 운영자가 서버 <b>hook_relay.env</b>(0600)에 넣습니다. 후크만 설치하는 일반 사용자는 토큰이 필요 없고 username 매핑만 하면 됩니다. 발급·변경 후 <b>systemctl --user restart hook_relay</b>. 아래 명령의 <code>xoxb-…</code>·<code>&lt;토큰&gt;</code>·<code>&lt;APP_ID&gt;</code> 등은 <b>예시 자리표시자</b>이니 발급받은 실제 값으로 바꿔 실행하세요.</p>
      <div class="tok-tabs" role="tablist" aria-label="플랫폼 선택">
        <button class="tok-tab active" type="button" data-plat="slack" role="tab">Slack</button>
        <button class="tok-tab" type="button" data-plat="telegram" role="tab">Telegram</button>
        <button class="tok-tab" type="button" data-plat="discord" role="tab">Discord</button>
      </div>
      <div class="tok-panel active" data-plat="slack">
        <h4 class="tok-h">Slack <span>SLACK_BOT_TOKEN · xoxb-…</span></h4>
        <ol class="tok-steps">
          <li><a href="https://api.slack.com/apps" target="_blank" rel="noopener noreferrer">api.slack.com/apps</a> → <b>Create New App</b> (From scratch) → 워크스페이스 선택</li>
          <li>좌측 <b>OAuth &amp; Permissions</b> → Bot Token Scopes 에 <code>chat:write</code> 추가 (봇 미초대 공개 채널엔 <code>chat:write.public</code>)</li>
          <li>상단 <b>Install to Workspace</b> → 승인</li>
          <li>같은 페이지의 <b>Bot User OAuth Token</b>(<code>xoxb-…</code>) 복사 — 토큰은 이 페이지에서만 표시(별도 복사 버튼 없음). 바로가기: 앱 → OAuth &amp; Permissions (<code>/apps/&lt;APP_ID&gt;/oauth</code>)</li>
          <li>발송 채널에서 <code>/invite @봇이름</code> · 매핑 channel = <code>#채널명</code> 또는 채널 ID</li>
        </ol>
        <div class="code"><button class="copy" type="button">복사</button><pre>curl -X POST https://slack.com/api/auth.test -H "Authorization: Bearer xoxb-…"</pre></div>
      </div>
      <div class="tok-panel" data-plat="telegram">
        <h4 class="tok-h">Telegram <span>TELEGRAM_BOT_TOKEN · 123456:ABC…</span></h4>
        <ol class="tok-steps">
          <li>텔레그램 <b>@BotFather</b> → <code>/newbot</code> → 이름·username → 토큰 발급</li>
          <li><b>보낼 대상에 따라 채팅을 먼저 활성화</b> — 봇은 먼저 말을 걸 수 없어, 사람이 먼저 보내야 <code>getUpdates</code>에 잡힙니다:
            <ul class="tok-sub">
              <li><b>개인 DM</b>: 봇(<code>@username</code>) 검색 → 열고 <b>시작(Start)</b> 또는 아무 메시지 전송 → <code>chat.id</code>는 <b>양수</b>(본인 ID)</li>
              <li><b>그룹</b>: 봇 추가 후 <b>프라이버시 모드</b>(기본 ON)라 일반 메시지는 안 잡힘 → <code>/start@봇username</code> 같은 <b>명령</b>이나 봇 <b>@멘션</b>을 보낼 것 (또는 @BotFather <code>/setprivacy</code> → 봇 선택 → <b>Disable</b> 후 봇 재추가) → <code>chat.id</code> = <code>-100…</code></li>
              <li><b>채널</b>: 봇을 <b>관리자</b>로 추가 → 채널에 글 1건 게시 → 응답의 <code>channel_post.chat.id</code>(<code>-100…</code>)</li>
            </ul>
          </li>
          <li>위 동작 <b>직후 곧바로</b> <code>getUpdates</code> 호출 → <code>chat.id</code> 확인. 매핑 channel = 그 값(<b>음수면 부호까지</b> 그대로). 업데이트는 한 번 읽히면 사라지고 <b>24시간</b> 뒤 만료되니 즉시 확인.</li>
          <li><b>그래도 <code>"result":[]</code>(빈 배열)이면</b> — ① DM은 <b>시작</b>을 눌렀는지 ② 그룹은 명령/멘션·프라이버시 ③ 웹훅이 걸려 있으면 안 잡힘(<code>getWebhookInfo</code>로 확인). 가장 간편한 대안: <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer"><b>@userinfobot</b></a>에 메시지를 보내 내 숫자 ID를 즉시 확인</li>
        </ol>
        <div class="code"><button class="copy" type="button">복사</button><pre>curl "https://api.telegram.org/bot&lt;토큰&gt;/getMe"
curl "https://api.telegram.org/bot&lt;토큰&gt;/getUpdates"
curl "https://api.telegram.org/bot&lt;토큰&gt;/getWebhookInfo"</pre></div>
      </div>
      <div class="tok-panel" data-plat="discord">
        <h4 class="tok-h">Discord <span>DISCORD_BOT_TOKEN</span></h4>
        <ol class="tok-steps">
          <li><a href="https://discord.com/developers/applications" target="_blank" rel="noopener noreferrer">discord.com/developers/applications</a> → <b>New Application</b> → 좌측 <b>Bot</b> → <b>Reset Token</b> → 복사</li>
          <li><b>OAuth2 → URL Generator</b>: scope <code>bot</code> + 권한 <code>View Channel</code>·<code>Send Messages</code> → 생성된 URL로 서버에 봇 초대</li>
          <li>설정 → 고급 → <b>개발자 모드</b> 켠 뒤 채널 우클릭 <b>ID 복사</b> → 매핑 channel = <b>channel_id</b></li>
        </ol>
      </div>
      <p class="tok-sec">⚠ 토큰은 서버 <b>hook_relay.env</b>(0600)에만 — 코드·문서·채팅에 붙여넣거나 커밋하지 마세요. 유출 시 즉시 재발급: Slack=OAuth &amp; Permissions Rotate · Telegram=BotFather <code>/revoke</code> · Discord=Reset Token.</p>
      <p class="tok-sec">공식 문서: <a href="https://docs.slack.dev/quickstart/" target="_blank" rel="noopener noreferrer">Slack Quickstart</a> · <a href="https://docs.slack.dev/reference/methods/auth.test/" target="_blank" rel="noopener noreferrer">auth.test</a> · <a href="https://core.telegram.org/bots/api" target="_blank" rel="noopener noreferrer">Telegram Bot API</a></p>
    </details>
  </div>
</div>

<script>
  const $ = s => document.querySelector(s);
  const K = (a,u) => a + '\x1f' + u;
  let lastRows = [], editing = null;
  let webPw = ''; try{ webPw = sessionStorage.getItem('relay-webpw') || ''; }catch(e){}
  function b64(s){ try{ return btoa(unescape(encodeURIComponent(s))); }catch(e){ return btoa(s); } }
  function authH(extra){ const h = extra || {}; if(webPw) h['Authorization'] = 'Basic ' + b64('admin:'+webPw); return h; }
  function openLogin(){ $('#login').classList.add('show'); $('#loginErr').textContent=''; const i=$('#loginPw'); if(i){ i.value=''; setTimeout(()=>i.focus(),60); } }
  function closeLogin(){ $('#login').classList.remove('show'); }
  async function submitLogin(){
    const pw=$('#loginPw').value; if(!pw){ $('#loginErr').textContent='암호를 입력하세요.'; return; }
    let ok=false;
    try{ const r=await fetch('/channels',{headers:{'Authorization':'Basic '+b64('admin:'+pw)}}); ok=r.ok; }
    catch(e){ $('#loginErr').textContent='서버에 연결하지 못했습니다.'; return; }
    if(!ok){ $('#loginErr').textContent='암호가 올바르지 않습니다.'; return; }
    webPw=pw; try{ sessionStorage.setItem('relay-webpw',pw); }catch(e){}
    closeLogin(); load();
  }
  function logout(){ webPw=''; try{ sessionStorage.removeItem('relay-webpw'); }catch(e){} editing=null; lastRows=[]; $('#board').innerHTML=''; $('#count').textContent='0 routes'; openLogin(); }
  const DKEY='relay-design', TKEY='relay-theme';

  function setDesign(d){
    document.documentElement.setAttribute('data-design', d);
    try{ localStorage.setItem(DKEY, d); }catch(e){}
    const s=$('#designSel'); if(s && s.value!==d) s.value = d; syncTC();
  }
  function setTheme(t){
    document.documentElement.setAttribute('data-theme', t);
    try{ localStorage.setItem(TKEY, t); }catch(e){}
    const b=$('#themeBtn'); if(b) b.textContent = t; syncTC();
  }

  function show(text, kind){
    const m=$('#msg'); m.textContent=text; m.className='msg show '+(kind||'err');
    if(kind==='good') setTimeout(()=>m.classList.remove('show'), 2600);
  }
  function esc(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  async function load(){
    const a=$('#fApp').value, u=$('#fUser').value.trim();
    const qs=new URLSearchParams(); if(a) qs.set('app',a); if(u) qs.set('username',u);
    let res;
    try{ res=await fetch('/channels?'+qs.toString(), {headers:authH()}); }
    catch(e){ show('서버에 연결하지 못했습니다.'); return; }
    if(res.status===401){ openLogin(); return; }
    let data; try{ data=await res.json(); }catch(e){ data={}; }
    render(data.channels||[]);
  }

  function render(rows){
    lastRows = rows;
    $('#count').textContent = rows.length + ' route' + (rows.length===1?'':'s');
    if(editing && !rows.some(r=>K(r.app,r.username)===editing)) editing=null;
    const board=$('#board');
    if(rows.length===0){ board.innerHTML='<div class="empty"><span class="empty-mark">&#8651;</span>아직 라우트가 없습니다.<br>아래 패치 패널에서 첫 신호 경로를 연결하세요.</div>'; return; }
    board.innerHTML = rows.map((r,i)=>{
      const e = editing===K(r.app,r.username);
      const chan = e ? '<input class="chan-edit" id="chanEdit" value="'+esc(r.channel)+'" aria-label="channel 값 수정" spellcheck="false" autocomplete="off">' : '<span class="chan">'+esc(r.channel)+'</span>';
      const ops = e
        ? '<button class="op save" data-act="save" data-app="'+esc(r.app)+'" data-user="'+esc(r.username)+'">저장</button><button class="op cancel" data-act="cancel">취소</button>'
        : '<button class="op edit" data-act="edit" data-app="'+esc(r.app)+'" data-user="'+esc(r.username)+'">수정</button><button class="op del" data-act="del" data-app="'+esc(r.app)+'" data-user="'+esc(r.username)+'">삭제</button>';
      return '<article class="route s-'+esc(r.app)+(e?' editing':'')+'" style="--i:'+i+'">'
        +'<div class="node"><span class="pin"></span><span class="badge">'+esc(r.app)+'</span></div>'
        +'<div class="path"><span class="user">@'+esc(r.username)+'</span><span class="wire" aria-hidden="true"></span>'+chan+'</div>'
        +'<div class="ops">'+ops+'</div></article>';
    }).join('');
    if(editing){ const inp=$('#chanEdit'); if(inp){ inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } }
  }

  function beginEdit(a,u){ editing=K(a,u); render(lastRows); }
  function cancelEdit(){ editing=null; render(lastRows); }
  async function saveEdit(a,u){
    const inp=$('#chanEdit'); const channel = inp ? inp.value.trim() : '';
    if(!channel){ show('channel을 입력하세요.'); return; }
    try{
      const res=await fetch('/channels',{method:'POST',headers:authH({'Content-Type':'application/json'}),body:JSON.stringify({app:a, username:u, channel:channel})});
      if(res.status===401){ openLogin(); return; }
      if(!res.ok){ show(await detail(res)); return; }
      editing=null; show('수정했습니다.','good'); load();
    }catch(e){ show('요청에 실패했습니다.'); }
  }
  async function add(){
    const body={ app:$('#nApp').value, username:$('#nUser').value.trim(), channel:$('#nChan').value.trim() };
    if(!body.username||!body.channel){ show('username과 channel을 입력하세요.'); return; }
    try{
      const res=await fetch('/channels',{method:'POST',headers:authH({'Content-Type':'application/json'}),body:JSON.stringify(body)});
      if(res.status===401){ openLogin(); return; }
      if(!res.ok){ show(await detail(res)); return; }
      $('#nUser').value=''; $('#nChan').value=''; show('연결했습니다.','good'); load();
    }catch(e){ show('요청에 실패했습니다.'); }
  }
  async function del(a,u){
    if(!confirm(u+' ('+a+') 라우트를 끊을까요?')) return;
    try{
      const res=await fetch('/channels',{method:'DELETE',headers:authH({'Content-Type':'application/json'}),body:JSON.stringify({app:a,username:u})});
      if(res.status===401){ openLogin(); return; }
      if(!res.ok){ show(await detail(res)); return; }
      if(editing===K(a,u)) editing=null; show('끊었습니다.','good'); load();
    }catch(e){ show('요청에 실패했습니다.'); }
  }
  async function detail(res){ try{ const j=await res.json(); return (j.detail||('오류 '+res.status)); }catch(e){ return '오류 '+res.status; } }

  $('#designSel').addEventListener('change', (e)=> setDesign(e.target.value));
  $('#themeBtn').addEventListener('click', ()=> setTheme(document.documentElement.getAttribute('data-theme')==='light'?'dark':'light'));
  $('#board').addEventListener('click', e=>{
    const b=e.target.closest('button[data-act]'); if(!b) return;
    const act=b.dataset.act;
    if(act==='edit') beginEdit(b.dataset.app, b.dataset.user);
    else if(act==='cancel') cancelEdit();
    else if(act==='save') saveEdit(b.dataset.app, b.dataset.user);
    else if(act==='del') del(b.dataset.app, b.dataset.user);
  });
  $('#board').addEventListener('keydown', e=>{
    if(!editing || !e.target.classList || !e.target.classList.contains('chan-edit')) return;
    if(e.key==='Enter'){ e.preventDefault(); const p=editing.split('\x1f'); saveEdit(p[0], p[1]); }
    else if(e.key==='Escape'){ e.preventDefault(); cancelEdit(); }
  });
  $('#loginForm').addEventListener('submit', e=>{ e.preventDefault(); submitLogin(); });
  $('#logoutBtn').addEventListener('click', logout);
  $('#add').addEventListener('click', add);
  $('#refresh').addEventListener('click', load);
  $('#fApp').addEventListener('change', load);
  $('#fUser').addEventListener('keydown', e=>{ if(e.key==='Enter') load(); });

  // ---- 설치 가이드 (클라이언트 복붙) ----
  const OSKEY='relay-os';
  const PLATKEY='relay-plat';
  const GUIDE_CMDS = {
    win: `New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\\.claude\\hooks" | Out-Null
Invoke-WebRequest __DL__/client/claude-notify.ps1 -OutFile "$env:USERPROFILE\\.claude\\hooks\\claude-notify.ps1"
Invoke-WebRequest __DL__/client/patch-claude-config.ps1 -OutFile "$env:TEMP\\hr-patch.ps1"
$env:NOTIFY_API_URL='__API__'; $env:NOTIFY_APP='slack'; $env:NOTIFY_USER='__USER__'
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\\hr-patch.ps1"`,
    mac: `brew list jq >/dev/null 2>&1 || brew install jq
mkdir -p ~/.claude/hooks
curl -fsSL __DL__/client/claude-notify-mac.sh -o ~/.claude/hooks/claude-notify-mac.sh
chmod +x ~/.claude/hooks/claude-notify-mac.sh
curl -fsSL __DL__/client/patch-claude-config-mac.sh -o /tmp/hr-patch.sh
NOTIFY_API_URL='__API__' NOTIFY_APP='slack' NOTIFY_USER='__USER__' bash /tmp/hr-patch.sh`,
    linux: `command -v jq >/dev/null || sudo dnf install -y jq   # 또는: sudo apt-get install -y jq
mkdir -p ~/.claude/hooks
curl -fsSL __DL__/client/claude-notify.sh -o ~/.claude/hooks/claude-notify.sh
chmod +x ~/.claude/hooks/claude-notify.sh
curl -fsSL __DL__/client/patch-claude-config.sh -o /tmp/hr-patch.sh
NOTIFY_API_URL='__API__' NOTIFY_APP='slack' NOTIFY_USER='__USER__' bash /tmp/hr-patch.sh`
  };
  function gApi(){ return 'http://' + location.hostname + ':20000'; }
  function gDl(){ return location.origin; }
  function renderGuide(){
    const u=($('#guideUser') && $('#guideUser').value.trim()) || '<나>';
    document.querySelectorAll('#guide pre[data-cmd]').forEach(function(pre){
      const tpl=GUIDE_CMDS[pre.getAttribute('data-cmd')]||'';
      pre.textContent = tpl.split('__DL__').join(gDl()).split('__API__').join(gApi()).split('__USER__').join(u);
    });
  }
  function setOS(os){
    document.querySelectorAll('#guide .os-tab').forEach(function(t){ t.classList.toggle('active', t.dataset.os===os); });
    document.querySelectorAll('#guide .os-panel').forEach(function(p){ p.classList.toggle('active', p.dataset.os===os); });
    try{ localStorage.setItem(OSKEY, os); }catch(e){}
  }
  function setPlat(pl){
    if(pl!=='slack' && pl!=='telegram' && pl!=='discord') pl='slack';
    document.querySelectorAll('#guide .tok-tab').forEach(function(t){ t.classList.toggle('active', t.dataset.plat===pl); });
    document.querySelectorAll('#guide .tok-panel').forEach(function(p){ p.classList.toggle('active', p.dataset.plat===pl); });
    try{ localStorage.setItem(PLATKEY, pl); }catch(e){}
  }
  function detectOS(){ const p=(navigator.platform||'')+' '+(navigator.userAgent||''); if(/Win/i.test(p))return 'win'; if(/Mac/i.test(p))return 'mac'; return 'linux'; }
  function openGuide(){ renderGuide(); $('#guide').classList.add('show'); }
  function closeGuide(){ $('#guide').classList.remove('show'); }
  async function copyText(txt, btn){
    try{ await navigator.clipboard.writeText(txt); }
    catch(e){ const t=document.createElement('textarea'); t.value=txt; t.style.position='fixed'; t.style.opacity='0'; document.body.appendChild(t); t.focus(); t.select(); try{ document.execCommand('copy'); }catch(_){ } t.remove(); }
    if(btn){ const o=btn.textContent; btn.textContent='복사됨'; btn.classList.add('done'); setTimeout(function(){ btn.textContent=o; btn.classList.remove('done'); }, 1400); }
  }
  $('#guideBtn').addEventListener('click', openGuide);
  $('#guideOpen').addEventListener('click', function(e){ e.preventDefault(); openGuide(); });
  $('#guideClose').addEventListener('click', closeGuide);
  $('#guideUser').addEventListener('input', renderGuide);
  $('#guide').addEventListener('click', function(e){
    if(e.target===$('#guide')){ closeGuide(); return; }
    const c=e.target.closest('.copy'); if(c){ const pre=c.parentElement.querySelector('pre'); if(pre) copyText(pre.textContent, c); return; }
    const t=e.target.closest('.os-tab'); if(t){ setOS(t.dataset.os); return; }
    const tp=e.target.closest('.tok-tab'); if(tp){ setPlat(tp.dataset.plat); return; }
  });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape' && $('#guide').classList.contains('show')) closeGuide(); });
  (function(){ let os='linux'; try{ os=localStorage.getItem(OSKEY)||detectOS(); }catch(e){ os=detectOS(); } setOS(os); let pl='slack'; try{ pl=localStorage.getItem(PLATKEY)||'slack'; }catch(e){} setPlat(pl); renderGuide(); })();

  function syncTC(){ try{ var m=document.getElementById('themeColor'); if(m){ var c=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(); if(c) m.setAttribute('content', c); } }catch(e){} }
  if('serviceWorker' in navigator){ window.addEventListener('load', function(){ navigator.serviceWorker.register('/sw.js').catch(function(){}); }); }
  setDesign(document.documentElement.getAttribute('data-design') || 'amber');
  setTheme(document.documentElement.getAttribute('data-theme') || 'dark');
  load();
</script>
</body>
</html>"""


if __name__ == "__main__":
    # 직접 실행 지원(python app.py): HOST/PORT 를 env 에서 — 하드코딩 회피.
    import uvicorn

    # TLS: web 역할 + SSL_CERTFILE/SSL_KEYFILE 설정·존재 시에만(래퍼와 동일 규칙). 경로는 env 로만.
    _cert = os.environ.get("SSL_CERTFILE", "")
    _key = os.environ.get("SSL_KEYFILE", "")
    _ssl = {}
    if (
        os.environ.get("HOOK_RELAY_ROLE") == "web"
        and _cert
        and _key
        and os.path.isfile(_cert)
        and os.path.isfile(_key)
    ):
        _ssl = {"ssl_certfile": _cert, "ssl_keyfile": _key}
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", os.environ.get("APP_PORT", "20000"))),
        **_ssl,
    )
