#!/usr/bin/env python3
"""
씨미(ci.me) 채팅 봇
- 명령어 응답 (웹 관리 페이지에서 추가/수정/삭제)
- 출석체크 (하루 1회, 새벽 5시 초기화)
- 변수 치환: <보낸사람>, <출석횟수>, <업타임>, <방제>, <카테고리>, <팔로우>
- 후원 감사 메세지 (<받은금액> 추가 변수)
- Selenium 자동 로그인 → 쿠키 추출
- localhost:8000 웹 관리 페이지
"""

from __future__ import annotations

import asyncio
import json
import uuid
import os
import sys
import webbrowser
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import random

import aiohttp
from aiohttp import web
import websockets
import aiosqlite

# ============================= 설정 =============================
# PyInstaller 빌드 환경 대응
if getattr(sys, 'frozen', False):
    # exe로 실행 중: 리소스는 _MEIPASS, DB는 exe 옆
    _BUNDLE_DIR = sys._MEIPASS
    _EXE_DIR = os.path.dirname(sys.executable)
else:
    _BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _EXE_DIR = _BUNDLE_DIR

BASE_DIR = _EXE_DIR
DB_PATH = os.path.join(_EXE_DIR, "bot.db")
WEB_DIR = os.path.join(_BUNDLE_DIR, "web")
OVERLAY_DIR = os.path.join(_EXE_DIR, "web")   # 오버레이는 항상 exe 옆 (file:// 접근용)
WEB_PORT = 8000

WS_URI = "wss://edge.ivschat.ap-northeast-2.amazonaws.com/"
API_BASE = "https://ci.me/api/app"
KST = timezone(timedelta(hours=9))
CHANNEL_INFO_INTERVAL = 60  # 채널 정보 갱신 주기(초)

LOGIN_URL = "https://ci.me/auth/login"
COOKIE_LIFETIME_HOURS = 24
# ================================================================

# ========================== 전역 상태 ===========================
state = {
    "running": False,
    "connected": False,
    "mauth_cookie": "",
    "cookie_time": "",
    "streamer_slug": "",
    "channel_info": {},   # title, category, openedAt, state
    "ws": None,
    "sid": None,
    "bot_task": None,
    "info_task": None,
}
overlay_clients: set = set()
# ================================================================


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ========================= 데이터베이스 =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS commands (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger     TEXT    UNIQUE NOT NULL,
                response    TEXT    NOT NULL,
                is_attendance INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL,
                attendance_date TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(username, attendance_date)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roulette_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                roulette_name   TEXT NOT NULL,
                user_name       TEXT NOT NULL,
                result          TEXT NOT NULL,
                spin_count      INTEGER DEFAULT 1,
                created_at      TEXT DEFAULT (datetime('now', '+9 hours'))
            );
            CREATE TABLE IF NOT EXISTS roulettes (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                roulette_name               TEXT    NOT NULL,
                data                        TEXT    NOT NULL DEFAULT '[]',
                trigger_donation_min_amount REAL    DEFAULT NULL,
                trigger_donation_max_amount REAL    DEFAULT NULL,
                trigger_donation_text       TEXT    DEFAULT NULL,
                trigger_optional_feature    TEXT    DEFAULT NULL,
                activate                    INTEGER DEFAULT 1,
                sort_order                  INTEGER DEFAULT 0,
                created_at                  TEXT    DEFAULT (datetime('now'))
            );
        """)
        defaults = {
            "donation_enabled": "0",
            "donation_message": "<보낸사람>님 <받은금액>원 후원 감사합니다!",
            "streamer_slug": "",
            "mauth_cookie": "",
            "cookie_time": "",
            "login_email": "",
            "login_password": "",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v)
            )

        # 첫 실행(DB 새로 생성)이면 기본 명령어 등록
        cur = await db.execute("SELECT COUNT(*) FROM commands")
        if (await cur.fetchone())[0] == 0:
            default_cmds = [
                ("!출첵", "<보낸사람>님 <출석횟수>번째 출석입니다.", 1),
                ("!방제", "<방제>", 0),
                ("!카테고리", "<카테고리>", 0),
                ("!팔로우", "<팔로우>동안 팔로우중", 0),
                ("!업타임", "<업타임>동안 방송중", 0),
            ]
            await db.executemany(
                "INSERT OR IGNORE INTO commands(trigger,response,is_attendance) VALUES(?,?,?)",
                default_cmds,
            )

        await db.commit()


async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value)
        )
        await db.commit()


def get_attendance_date() -> str:
    """새벽 5시 기준 출석 날짜 반환"""
    now = datetime.now(KST)
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


async def do_attendance(username: str):
    """출석 체크. (성공 여부, 총 출석 횟수) 반환"""
    date = get_attendance_date()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM attendance WHERE username=? AND attendance_date=?",
            (username, date),
        )
        already = await cur.fetchone()

        if already:
            cur2 = await db.execute(
                "SELECT COUNT(*) FROM attendance WHERE username=?", (username,)
            )
            return False, (await cur2.fetchone())[0]

        await db.execute(
            "INSERT INTO attendance(username, attendance_date) VALUES(?,?)",
            (username, date),
        )
        await db.commit()

        cur2 = await db.execute(
            "SELECT COUNT(*) FROM attendance WHERE username=?", (username,)
        )
        return True, (await cur2.fetchone())[0]


# ========================== 룰렛 헬퍼 ============================
def pick_roulette_result(items: list[dict]) -> int:
    """가중치 기반 룰렛 항목 선택. 항목 인덱스 반환."""
    weights = [float(item.get("weight", 0)) for item in items]
    total = sum(weights)
    if total <= 0:
        return random.randint(0, len(items) - 1)
    r = random.uniform(0, total)
    cumulative = 0
    for i, w in enumerate(weights):
        cumulative += w
        if r <= cumulative:
            return i
    return len(items) - 1


async def broadcast_overlay(data: dict):
    """연결된 오버레이 클라이언트에 데이터 전송"""
    global overlay_clients
    dead = set()
    for ws in overlay_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    overlay_clients -= dead


async def trigger_roulette(sender: str, roulette_row: dict, spin_count: int = 1):
    """룰렛 실행, 오버레이 전송 및 기록 저장"""
    items = json.loads(roulette_row["data"] or "[]")
    if not items:
        return None
    results = [pick_roulette_result(items) for _ in range(spin_count)]
    item_names = [it["data"] for it in items]
    result_names = [item_names[i] for i in results]
    await broadcast_overlay({
        "type": "run_roulette",
        "user_name": sender,
        "roulette_name": roulette_row["roulette_name"],
        "data": item_names,
        "resultdata": results,
    })
    # 기록 저장
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO roulette_logs(roulette_name, user_name, result, spin_count) VALUES(?,?,?,?)",
            (roulette_row["roulette_name"], sender, json.dumps(result_names, ensure_ascii=False), spin_count),
        )
        await db.commit()
    return result_names


# ========================== API 헬퍼 ============================
async def fetch_channel_info(session: aiohttp.ClientSession, slug: str) -> dict:
    url = f"{API_BASE}/channels/{slug}/live"
    try:
        async with session.get(url) as resp:
            body = await resp.json()
            d = body.get("data") or {}
            return {
                "title": d.get("title", ""),
                "category": (d.get("category") or {}).get("name", ""),
                "openedAt": d.get("openedAt", ""),
                "state": d.get("state", ""),
            }
    except Exception as e:
        log(f"[ERROR] 채널 정보 조회 실패: {e}")
        return {}


async def fetch_follow_info(
    session: aiohttp.ClientSession, slug: str, user_channel_id: str
) -> dict:
    cookie = state["mauth_cookie"]
    url = f"{API_BASE}/channels/{slug}/profile-card?channelId={user_channel_id}"
    cookies = {"mauth-authorization-code": cookie} if cookie else {}
    try:
        async with session.get(url, cookies=cookies) as resp:
            body = await resp.json()
            return (body.get("data") or {}).get("follow") or {}
    except Exception as e:
        log(f"[ERROR] 팔로우 정보 조회 실패: {e}")
        return {}


def calc_uptime(opened_at: str) -> str:
    try:
        start = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        total = int((datetime.now(timezone.utc) - start).total_seconds())
        if total < 0:
            return "0초"
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        if h > 0:
            return f"{h}시간 {m}분 {s}초"
        if m > 0:
            return f"{m}분 {s}초"
        return f"{s}초"
    except Exception:
        return "알 수 없음"


def calc_follow_duration(followed_at: str) -> str:
    try:
        start = datetime.fromisoformat(followed_at.replace("Z", "+00:00"))
        total = int((datetime.now(timezone.utc) - start).total_seconds())
        if total < 0:
            return "0분"
        d = total // 86400
        h = (total % 86400) // 3600
        m = (total % 3600) // 60
        parts = []
        if d:
            parts.append(f"{d}일")
        if h:
            parts.append(f"{h}시간")
        if m:
            parts.append(f"{m}분")
        return " ".join(parts) if parts else "0분"
    except Exception:
        return "알 수 없음"


async def replace_variables(
    template: str,
    sender: str,
    slug: str,
    session: aiohttp.ClientSession,
    user_channel_id: str | None = None,
) -> str:
    result = template.replace("<보낸사람>", sender)

    if "<업타임>" in result:
        opened = state.get("channel_info", {}).get("openedAt", "")
        result = result.replace("<업타임>", calc_uptime(opened) if opened else "알 수 없음")

    if "<방제>" in result:
        result = result.replace(
            "<방제>", state.get("channel_info", {}).get("title", "알 수 없음")
        )

    if "<카테고리>" in result:
        result = result.replace(
            "<카테고리>", state.get("channel_info", {}).get("category", "알 수 없음")
        )

    if "<팔로우>" in result:
        if user_channel_id:
            fi = await fetch_follow_info(session, slug, user_channel_id)
            if fi.get("isFollowing") and fi.get("followedAt"):
                result = result.replace("<팔로우>", calc_follow_duration(fi["followedAt"]))
            else:
                result = result.replace("<팔로우>", "팔로우 안 함")
        else:
            result = result.replace("<팔로우>", "알 수 없음")

    return result


# ============================ 봇 ================================
async def get_chat_token(session: aiohttp.ClientSession, slug: str):
    url = f"{API_BASE}/channels/{slug}/chat-token"
    cookie = state["mauth_cookie"]
    cookies = {"mauth-authorization-code": cookie} if cookie else {}
    try:
        async with session.post(url, cookies=cookies) as resp:
            body = await resp.json()
            d = body.get("data") or {}
            return d.get("token"), d.get("sid")
    except Exception as e:
        log(f"[ERROR] 토큰 발급 실패: {e}")
        return None, None


async def send_message(ws, sid: str, text: str):
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
    payload = {
        "Action": "SEND_MESSAGE",
        "RequestId": str(uuid.uuid4()),
        "Content": text,
        "Attributes": {"type": "MESSAGE", "sid": sid or "", "sendTimeAt": ts},
    }
    await ws.send(json.dumps(payload))


def parse_ws_message(raw: str):
    """(msg_type, sender, content, user_channel_id, attrs) 반환"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, None, None, {}

    msg_type = data.get("Type")
    content = data.get("Content", "")
    attrs = data.get("Attributes") or {}

    sender = None
    user_channel_id = None
    sender_attrs = (data.get("Sender") or {}).get("Attributes") or {}
    try:
        ui = json.loads(sender_attrs.get("user", "{}"))
        sender = ui.get("ch", {}).get("na")
        cid = ui.get("ch", {}).get("id")
        if cid:
            user_channel_id = str(cid)
    except (json.JSONDecodeError, AttributeError):
        pass

    return msg_type, sender, content, user_channel_id, attrs


async def handle_message(
    session: aiohttp.ClientSession, ws, sid: str, slug: str, raw: str
):
    msg_type, sender, content, ucid, attrs = parse_ws_message(raw)
    if msg_type is None:
        return

    attr_type = (attrs.get("type") or "").upper()

    # ── 후원 감지 ──
    if "DONATION" in attr_type or (
        msg_type == "EVENT"
        and "donation" in json.dumps(attrs, ensure_ascii=False).lower()
    ):
        await handle_donation(session, ws, sid, slug, sender, ucid, attrs, content)
        return

    if msg_type != "MESSAGE" or not sender:
        return

    log(f"[{sender}] {content}")
    cmd = content.strip()

    # DB에서 명령어 검색
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT response, is_attendance FROM commands WHERE trigger=?", (cmd,)
        )
        row = await cur.fetchone()

    if row:
        response_tpl, is_attendance = row

        if is_attendance:
            ok, count = await do_attendance(sender)
            if not ok:
                reply = f"{sender}님은 이미 오늘 출석체크를 하셨습니다. (총 {count}회)"
                await send_message(ws, sid, reply)
                log(f"  -> [봇] {reply}")
                return
            response_tpl = response_tpl.replace("<출석횟수>", str(count))

        reply = await replace_variables(response_tpl, sender, slug, session, ucid)
        await send_message(ws, sid, reply)
        log(f"  -> [봇] {reply}")



async def handle_donation(
    session, ws, sid, slug, sender, ucid, attrs, content
):
    # extra JSON 파싱 (DONATION_CHAT 형식)
    extra = {}
    try:
        extra = json.loads(attrs.get("extra", "{}"))
    except (json.JSONDecodeError, TypeError):
        pass

    amount = (
        extra.get("amt")
        or attrs.get("amount")
        or attrs.get("payAmount")
        or "0"
    )
    dn_sender = (
        sender
        or (extra.get("prof", {}).get("ch", {}).get("na"))
        or attrs.get("nickname")
        or attrs.get("name")
        or "익명"
    )
    # extra.msg로 content 보완
    if not content and extra.get("msg"):
        content = extra["msg"]

    # ── 후원 감사 메세지 ──
    enabled = await get_setting("donation_enabled")
    if enabled == "1":
        template = (
            await get_setting("donation_message")
            or "<보낸사람>님 <받은금액>원 후원 감사합니다!"
        )
        reply = template.replace("<받은금액>", str(amount))
        reply = await replace_variables(reply, dn_sender, slug, session, ucid)
        await send_message(ws, sid, reply)
        log(f"  -> [후원] {reply}")

    # ── 룰렛 후원 트리거 ──
    try:
        amt = float(amount)
    except (ValueError, TypeError):
        amt = 0
    if amt > 0:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM roulettes WHERE activate=1 AND trigger_donation_min_amount IS NOT NULL"
            )
            roulette_rows = [dict(r) for r in await cur.fetchall()]
        for r in roulette_rows:
            min_amt = float(r.get("trigger_donation_min_amount") or 0)
            if min_amt <= 0 or amt < min_amt:
                continue
            # 포함 텍스트 체크
            req_text = (r.get("trigger_donation_text") or "").strip()
            if req_text and req_text not in (content or ""):
                continue
            # 스핀 횟수 계산
            spin_count = int(amt // min_amt)
            max_amt = r.get("trigger_donation_max_amount")
            if max_amt and float(max_amt) > 0:
                max_spins = int(float(max_amt) // min_amt)
                spin_count = min(spin_count, max_spins)
            spin_count = max(1, spin_count)
            results = await trigger_roulette(dn_sender, r, spin_count)
            if results:
                log(f"  -> [룰렛] {r['roulette_name']}: {', '.join(results)}")
            break  # 첫 번째 매칭 룰렛만 실행


async def ping_loop(ws):
    try:
        while True:
            await asyncio.sleep(30)
            await ws.ping()
    except Exception:
        pass


async def listen_channel(session: aiohttp.ClientSession, slug: str):
    while state.get("running"):
        token, sid = await get_chat_token(session, slug)
        if not token:
            log("토큰 발급 실패, 10초 후 재시도...")
            await asyncio.sleep(10)
            continue

        state["sid"] = sid
        try:
            async with websockets.connect(WS_URI, subprotocols=[token]) as ws:
                state["ws"] = ws
                state["connected"] = True
                log(f"[{slug}] 채팅 연결됨")

                ping = asyncio.create_task(ping_loop(ws))
                try:
                    while True:
                        raw = await ws.recv()
                        await handle_message(session, ws, sid, slug, raw)
                except websockets.ConnectionClosed:
                    log(f"[{slug}] 연결 끊김, 재연결 대기...")
                finally:
                    ping.cancel()
                    state["connected"] = False
                    state["ws"] = None
        except Exception as e:
            log(f"[ERROR] [{slug}] {e}")
            state["connected"] = False

        if state.get("running"):
            await asyncio.sleep(5)


async def channel_info_loop(session: aiohttp.ClientSession, slug: str):
    while state.get("running"):
        info = await fetch_channel_info(session, slug)
        if info:
            state["channel_info"] = info
        await asyncio.sleep(CHANNEL_INFO_INTERVAL)


# ======================== Selenium 로그인 =======================
def do_selenium_login(email: str, password: str) -> str | None:
    """로그인 후 mauth-authorization-code 쿠키 반환"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        # 일반적인 권장 임포트 방식
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        log("[ERROR] selenium이 설치되지 않았습니다: pip install selenium")
        return None

    import time

    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, 20)

        # ci.me/auth/login → marpple 로그인 페이지로 리다이렉트됨
        # 이메일·비밀번호 필드가 모두 나타날 때까지 대기
        email_input = wait.until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "input[placeholder*='이메일'], input[type='email'], input[name='email']"
            ))
        )
        pw_input = driver.find_element(
            By.CSS_SELECTOR,
            "input[placeholder*='비밀번호'], input[type='password']"
        )

        # 딜레이 없이 바로 입력
        email_input.send_keys(email)
        pw_input.send_keys(password)

        # "로그인하기" 버튼 클릭
        btn = driver.find_element(
            By.XPATH,
            "//button[contains(text(),'로그인하기')] | //button[contains(text(),'로그인')]"
            " | //button[@type='submit']"
        )
        btn.click()

        # ci.me 도메인으로 이동 감지 (로그인 성공 시 리다이렉트)
        WebDriverWait(driver, 60).until(
            lambda d: "ci.me" in d.current_url
            and "/auth/login" not in d.current_url
        )
        time.sleep(1)

        # 쿠키 추출
        for c in driver.get_cookies():
            if c["name"] == "mauth-authorization-code":
                log("[LOGIN] 쿠키 추출 성공")
                return c["value"]

        log("[WARN] mauth-authorization-code 쿠키를 찾을 수 없습니다.")
        return None

    except Exception as e:
        log(f"[ERROR] Selenium 로그인 실패: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ========================== 웹 서버 =============================
routes = web.RouteTableDef()


@routes.get("/")
async def page_index(request):
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


@routes.get("/favicon.ico")
async def page_favicon(request):
    ico = os.path.join(_BUNDLE_DIR, "favicon.ico")
    if os.path.exists(ico):
        return web.FileResponse(ico)
    return web.Response(status=404)


@routes.get("/roulette")
async def page_roulette(request):
    return web.FileResponse(os.path.join(WEB_DIR, "roulette.html"))


@routes.get("/roulette_overlay")
async def page_roulette_overlay(request):
    return web.FileResponse(os.path.join(WEB_DIR, "roulette_overlay.html"))


@routes.get("/api/overlay-path")
async def api_overlay_path(request):
    """오버레이 파일의 file:// URL 반환 (exe 옆 web/ 폴더 기준)"""
    fpath = os.path.join(OVERLAY_DIR, "roulette_overlay.html")
    abs_path = os.path.abspath(fpath).replace("\\", "/")
    return web.json_response({"path": f"file:///{abs_path}"})


# ── 명령어 CRUD ──
@routes.get("/api/commands")
async def api_commands_list(request):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM commands ORDER BY id")
        rows = [dict(r) for r in await cur.fetchall()]
    return web.json_response(rows)


@routes.post("/api/commands")
async def api_commands_add(request):
    d = await request.json()
    trigger = (d.get("trigger") or "").strip()
    response = (d.get("response") or "").strip()
    is_att = 1 if d.get("is_attendance") else 0
    if not trigger or not response:
        return web.json_response({"error": "명령어와 응답을 입력하세요."}, status=400)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO commands(trigger,response,is_attendance) VALUES(?,?,?)",
                (trigger, response, is_att),
            )
            await db.commit()
        return web.json_response({"ok": True})
    except aiosqlite.IntegrityError:
        return web.json_response({"error": "이미 존재하는 명령어입니다."}, status=409)


@routes.put("/api/commands/{cmd_id}")
async def api_commands_update(request):
    cid = request.match_info["cmd_id"]
    d = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE commands SET trigger=?, response=?, is_attendance=? WHERE id=?",
            (d.get("trigger", ""), d.get("response", ""), 1 if d.get("is_attendance") else 0, cid),
        )
        await db.commit()
    return web.json_response({"ok": True})


@routes.delete("/api/commands/{cmd_id}")
async def api_commands_delete(request):
    cid = request.match_info["cmd_id"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM commands WHERE id=?", (cid,))
        await db.commit()
    return web.json_response({"ok": True})


def _mask(value: str, show: int = 3) -> str:
    """값을 블러용으로 마스킹: 앞 show자만 보이고 나머지 *"""
    if not value:
        return ""
    if len(value) <= show:
        return "*" * len(value)
    return value[:show] + "*" * (len(value) - show)


# ── 설정 ──
@routes.get("/api/settings")
async def api_settings_get(request):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
    settings = {r[0]: r[1] for r in rows}

    # 민감 정보는 마스킹 + 존재 여부만 전달
    resp = dict(settings)
    if settings.get("login_email"):
        resp["login_email_masked"] = _mask(settings["login_email"], 4)
        resp["login_email_saved"] = True
    else:
        resp["login_email_saved"] = False
    if settings.get("login_password"):
        resp["login_password_masked"] = _mask(settings["login_password"], 0)
        resp["login_password_saved"] = True
    else:
        resp["login_password_saved"] = False
    # 민감 정보는 클라이언트에 노출하지 않음
    resp.pop("login_email", None)
    resp.pop("login_password", None)
    resp.pop("mauth_cookie", None)
    return web.json_response(resp)


@routes.put("/api/settings")
async def api_settings_update(request):
    d = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        for k, v in d.items():
            await db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v))
            )
        await db.commit()
    if "streamer_slug" in d:
        state["streamer_slug"] = d["streamer_slug"]
    if "mauth_cookie" in d:
        state["mauth_cookie"] = d["mauth_cookie"]
    return web.json_response({"ok": True})


# ── 로그인 ──
@routes.post("/api/login")
async def api_login(request):
    d = await request.json()
    email = d.get("email", "")
    password = d.get("password", "")
    if not email or not password:
        return web.json_response({"error": "이메일과 비밀번호를 입력하세요."}, status=400)

    # 계정 정보 저장
    await set_setting("login_email", email)
    await set_setting("login_password", password)

    loop = asyncio.get_event_loop()
    cookie = await loop.run_in_executor(None, do_selenium_login, email, password)

    if cookie:
        state["mauth_cookie"] = cookie
        state["cookie_time"] = datetime.now(KST).isoformat()
        await set_setting("mauth_cookie", cookie)
        await set_setting("cookie_time", state["cookie_time"])
        # 로그인 성공 후 봇 자동 시작
        await _auto_start_bot(request.app, force_reconnect=True)
        bot_started = state.get("running", False)
        return web.json_response({
            "ok": True,
            "message": "로그인 성공!",
            "bot_started": bot_started,
        })
    return web.json_response({"error": "로그인 실패. 다시 시도해주세요."}, status=400)


@routes.post("/api/relogin")
async def api_relogin(request):
    """저장된 계정 정보로 재로그인"""
    email = await get_setting("login_email")
    password = await get_setting("login_password")
    if not email or not password:
        return web.json_response({"error": "저장된 계정 정보가 없습니다."}, status=400)

    loop = asyncio.get_event_loop()
    cookie = await loop.run_in_executor(None, do_selenium_login, email, password)

    if cookie:
        state["mauth_cookie"] = cookie
        state["cookie_time"] = datetime.now(KST).isoformat()
        await set_setting("mauth_cookie", cookie)
        await set_setting("cookie_time", state["cookie_time"])
        # 재로그인 성공 후 봇 자동 시작
        await _auto_start_bot(request.app, force_reconnect=True)
        bot_started = state.get("running", False)
        return web.json_response({
            "ok": True,
            "message": "재로그인 성공!",
            "bot_started": bot_started,
        })
    return web.json_response({"error": "재로그인 실패. 다시 시도해주세요."}, status=400)


# ── 상태 ──
@routes.get("/api/status")
async def api_status(request):
    ct = state.get("cookie_time", "")
    cookie_valid = False
    if ct and state.get("mauth_cookie"):
        try:
            diff = (datetime.now(KST) - datetime.fromisoformat(ct)).total_seconds()
            cookie_valid = diff < COOKIE_LIFETIME_HOURS * 3600
        except Exception:
            pass

    return web.json_response({
        "running": state.get("running", False),
        "connected": state.get("connected", False),
        "streamer_slug": state.get("streamer_slug", ""),
        "channel_info": state.get("channel_info", {}),
        "cookie_valid": cookie_valid,
        "cookie_time": ct,
    })


# ── 봇 시작 / 중지 ──
@routes.post("/api/bot/start")
async def api_bot_start(request):
    slug = state.get("streamer_slug") or await get_setting("streamer_slug")
    if not slug:
        return web.json_response({"error": "스트리머 슬러그를 먼저 설정하세요."}, status=400)
    if state.get("running"):
        return web.json_response({"error": "봇이 이미 실행 중입니다."}, status=400)

    state["running"] = True
    state["streamer_slug"] = slug

    sess = request.app.get("http_session")
    if not sess:
        sess = aiohttp.ClientSession()
        request.app["http_session"] = sess

    state["bot_task"] = asyncio.create_task(listen_channel(sess, slug))
    state["info_task"] = asyncio.create_task(channel_info_loop(sess, slug))

    log(f"봇 시작: {slug}")
    return web.json_response({"ok": True})


@routes.post("/api/bot/stop")
async def api_bot_stop(request):
    await _stop_bot()
    log("봇 중지됨")
    return web.json_response({"ok": True})


# ── 출석 통계 ──
@routes.get("/api/attendance")
async def api_attendance(request):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT username, COUNT(*) AS cnt, MAX(attendance_date) AS last
            FROM attendance GROUP BY username ORDER BY cnt DESC LIMIT 100
        """)
        rows = await cur.fetchall()
    return web.json_response(
        [{"username": r[0], "total": r[1], "last_date": r[2]} for r in rows]
    )


# ── 룰렛 CRUD ──
@routes.get("/api/roulettes")
async def api_roulettes_list(request):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM roulettes ORDER BY sort_order, id")
        rows = [dict(r) for r in await cur.fetchall()]
    return web.json_response(rows)


@routes.post("/api/roulettes/save-all")
async def api_roulettes_save_all(request):
    """전체 룰렛 일괄 저장"""
    data_list = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM roulettes")
        for i, r in enumerate(data_list):
            await db.execute(
                """INSERT INTO roulettes(roulette_name, data,
                   trigger_donation_min_amount, trigger_donation_max_amount,
                   trigger_donation_text, trigger_optional_feature,
                   activate, sort_order) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    r.get("roulette_name", f"룰렛 {i + 1}"),
                    r.get("data", "[]"),
                    r.get("trigger_donation_min_amount"),
                    r.get("trigger_donation_max_amount"),
                    r.get("trigger_donation_text"),
                    r.get("trigger_optional_feature"),
                    1 if r.get("activate") else 0,
                    i,
                ),
            )
        await db.commit()
    return web.json_response({"ok": True})


@routes.post("/api/roulettes/test")
async def api_roulettes_test(request):
    d = await request.json()
    items = d.get("data", [])
    if not items:
        return web.json_response({"error": "항목이 없습니다."}, status=400)
    roulette_name = d.get("roulette_name", "테스트 룰렛")
    idx = pick_roulette_result(items)
    result_item = items[idx]["data"]
    # 오버레이에 테스트 전송
    await broadcast_overlay({
        "type": "run_roulette",
        "user_name": "테스트",
        "roulette_name": roulette_name,
        "data": [item["data"] for item in items],
        "resultdata": [idx],
    })
    # 기록 저장
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO roulette_logs(roulette_name, user_name, result, spin_count) VALUES(?,?,?,?)",
            (roulette_name, "테스트", json.dumps([result_item], ensure_ascii=False), 1),
        )
        await db.commit()
    return web.json_response({"ok": True, "result": result_item})


# ── 룰렛 기록 ──
@routes.get("/api/roulette-logs")
async def api_roulette_logs(request):
    page = int(request.query.get("page", 1))
    per_page = int(request.query.get("per_page", 50))
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as cnt FROM roulette_logs")
        total = (await cur.fetchone())["cnt"]
        cur = await db.execute(
            "SELECT * FROM roulette_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return web.json_response({"total": total, "page": page, "per_page": per_page, "data": rows})


@routes.delete("/api/roulette-logs")
async def api_roulette_logs_clear(request):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM roulette_logs")
        await db.commit()
    return web.json_response({"ok": True})


@routes.get("/api/roulette-logs/csv")
async def api_roulette_logs_csv(request):
    import io, csv
    output = io.StringIO()
    output.write('\ufeff')  # BOM for Excel
    writer = csv.writer(output)
    writer.writerow(["번호", "룰렛", "사용자", "결과", "횟수", "일시"])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM roulette_logs ORDER BY id DESC")
        for r in await cur.fetchall():
            writer.writerow([r["id"], r["roulette_name"], r["user_name"], r["result"], r["spin_count"], r["created_at"]])
    return web.Response(
        body=output.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": "attachment; filename=roulette_logs.csv"},
    )


@routes.get("/api/roulette-logs/retention")
async def api_roulette_logs_retention(request):
    val = await get_setting("roulette_log_retention_months")
    return web.json_response({"months": int(val) if val else 6})


@routes.put("/api/roulette-logs/retention")
async def api_roulette_logs_retention_set(request):
    d = await request.json()
    months = max(1, int(d.get("months", 6)))
    await save_setting("roulette_log_retention_months", str(months))
    return web.json_response({"ok": True, "months": months})


async def cleanup_old_roulette_logs():
    """보관 기간이 지난 룰렛 기록 삭제"""
    val = await get_setting("roulette_log_retention_months")
    months = int(val) if val else 6
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM roulette_logs WHERE created_at < datetime('now', '+9 hours', ?)",
            (f"-{months} months",),
        )
        await db.commit()


# ── 오버레이 WebSocket ──
@routes.get("/ws/overlay")
async def ws_overlay(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    overlay_clients.add(ws)
    log("[오버레이] 클라이언트 연결됨")
    try:
        async for msg in ws:
            pass
    finally:
        overlay_clients.discard(ws)
        log("[오버레이] 클라이언트 연결 해제")
    return ws


async def _stop_bot():
    """실행 중인 봇 정리"""
    state["running"] = False
    for key in ("bot_task", "info_task"):
        t = state.get(key)
        if t and not t.done():
            t.cancel()
        state[key] = None
    ws = state.get("ws")
    if ws:
        try:
            await ws.close()
        except Exception:
            pass
        state["ws"] = None
    state["connected"] = False


async def _auto_start_bot(app, force_reconnect=False):
    """저장된 슬러그가 있으면 봇 자동 시작. force_reconnect=True면 기존 연결 끊고 재연결"""
    slug = state.get("streamer_slug")
    if not slug or not state.get("mauth_cookie"):
        return

    if force_reconnect and state.get("running"):
        log("봇 재연결 중...")
        await _stop_bot()

    if state.get("running"):
        return

    state["running"] = True
    sess = app.get("http_session")
    if not sess:
        sess = aiohttp.ClientSession()
        app["http_session"] = sess

    state["bot_task"] = asyncio.create_task(listen_channel(sess, slug))
    state["info_task"] = asyncio.create_task(channel_info_loop(sess, slug))
    log(f"봇 자동 시작: {slug}")


async def _auto_login(app):
    """저장된 계정으로 자동 로그인 (백그라운드)"""
    email = await get_setting("login_email")
    password = await get_setting("login_password")
    if not email or not password:
        log("[AUTO] 저장된 계정 없음 — 자동 로그인 건너뜀")
        return

    log("[AUTO] 저장된 계정으로 자동 로그인 시도...")
    loop = asyncio.get_event_loop()
    cookie = await loop.run_in_executor(None, do_selenium_login, email, password)

    if cookie:
        state["mauth_cookie"] = cookie
        state["cookie_time"] = datetime.now(KST).isoformat()
        await set_setting("mauth_cookie", cookie)
        await set_setting("cookie_time", state["cookie_time"])
        log("[AUTO] 자동 로그인 성공!")
        await _auto_start_bot(app)
    else:
        log("[AUTO] 자동 로그인 실패 — 웹페이지에서 수동 로그인하세요.")


# ── 앱 생명주기 ──
def _extract_overlay():
    """exe 실행 시 오버레이 파일을 exe 옆 web/ 폴더에 복사"""
    if not getattr(sys, 'frozen', False):
        return
    import shutil
    os.makedirs(OVERLAY_DIR, exist_ok=True)
    for fname in ("roulette_overlay.html", "roulette_sound.mp3"):
        src = os.path.join(WEB_DIR, fname)
        dst = os.path.join(OVERLAY_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
    log(f"[오버레이] {OVERLAY_DIR} 복사 완료")


async def on_startup(app):
    _extract_overlay()
    await init_db()
    await cleanup_old_roulette_logs()
    state["mauth_cookie"] = await get_setting("mauth_cookie") or ""
    state["cookie_time"] = await get_setting("cookie_time") or ""
    state["streamer_slug"] = await get_setting("streamer_slug") or ""
    log(f"웹 관리 페이지: http://localhost:{WEB_PORT}")

    # 대시보드 자동 열기 (별도 스레드로 약간 지연 후 열기)
    def _open_browser():
        import time
        time.sleep(0.5)
        webbrowser.open(f"http://localhost:{WEB_PORT}")
    threading.Thread(target=_open_browser, daemon=True).start()

    # 자동 로그인을 백그라운드 태스크로 실행 (웹서버 시작을 블로킹하지 않음)
    app["auto_login_task"] = asyncio.create_task(_auto_login(app))


async def on_cleanup(app):
    state["running"] = False
    for key in ("bot_task", "info_task"):
        t = state.get(key)
        if t and not t.done():
            t.cancel()
    alt = app.get("auto_login_task")
    if alt and not alt.done():
        alt.cancel()
    sess = app.get("http_session")
    if sess:
        await sess.close()
    # 오버레이 WebSocket 정리
    for ws in list(overlay_clients):
        await ws.close()
    overlay_clients.clear()


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=WEB_PORT, print=None)
