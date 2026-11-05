# api/index.py — Terabox -> Telegram bot (multi-source, bn/en i18n)
# tier1 shakir proxy · tier2 robin · tier3 jsToken+sign · tier4 ndus cookie · tier5 solver

import html
import json
import logging
import re
import time
from typing import Optional

import requests
from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================================================================
# CONFIG
# ================================================================
BOT_TOKEN      = "8426186448:AAEJCf9mGHyZydX1TaJRWLRzvn50SlUyQw8"
WEBHOOK_SECRET = "tb_71x_smith_secret"

# *** SET THIS to fix dlink N/A ***
# browser -> terabox.com login -> devtools -> Application -> Cookies
#   -> terabox.com -> ndus value
# paste below as: TERABOX_COOKIE = "ndus=PASTE_HERE"
TERABOX_COOKIE = ""

SOLVER_URL = ""
TB_BASE    = "https://teraboxdl.site"
TS_SITEKEY = "0x4AAAAAAFZJOCzA6dP3BIYH"

SHAKIR_API = "https://tbx-proxy.shakir-ansarii075.workers.dev/"
ROBIN_API  = "https://terabox-worker.robinkumarshakya103.workers.dev/api"

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36")

HTTP_TIMEOUT = 20
COOLDOWN     = 3.0
CHUNK        = 5

# ================================================================
# i18n
# ================================================================
LANG_STRINGS = {
    "en": {
        "welcome_body": (
            "send a terabox share link.\n"
            "i'll return <b>direct download</b> and <b>stream</b> buttons per file.\n\n"
            "<b>/help</b>  ·  <b>/id</b>  ·  <b>/lang</b>"
        ),
        "extracting": "⚡ extracting…",
        "failed_hint": "link may be expired, private, or terabox changed shape",
        "slow_down": "⏳ slow down — {n}s",
        "lang_set": "language set to <b>English</b>.",
        "lang_pick": "choose language:",
        "files_label": "file(s)",
        "showing": "showing",
        "no_dlink_hint": "no direct link — set TERABOX_COOKIE in bot config (see /help)",
    },
    "bn": {
        "welcome_body": (
            "terabox শেয়ার লিংক পাঠাও।\n"
            "প্রতি ফাইলের জন্য <b>direct download</b> আর <b>stream</b> বাটন দেব।\n\n"
            "<b>/help</b>  ·  <b>/id</b>  ·  <b>/lang</b>"
        ),
        "extracting": "⚡ বের করছি…",
        "failed_hint": "লিংক expired হতে পারে, private, অথবা terabox structure বদলেছে",
        "slow_down": "⏳ একটু দাঁড়াও — {n}s",
        "lang_set": "ভাষা <b>বাংলা</b> সেট হয়েছে।",
        "lang_pick": "ভাষা বেছে নাও:",
        "files_label": "ফাইল",
        "showing": "দেখাচ্ছি",
        "no_dlink_hint": "direct link পাওয়া যায়নি — bot config এ TERABOX_COOKIE সেট করো (দেখো /help)",
    },
}

USER_LANG: dict[int, str] = {}


def lang_for(obj) -> str:
    try:
        uid = obj.from_user.id
    except Exception:
        return "en"
    if uid in USER_LANG:
        return USER_LANG[uid]
    code = (getattr(obj.from_user, "language_code", None) or "en").lower()
    return "bn" if code.startswith("bn") else "en"


def t(lang: str, key: str, **kw) -> str:
    s = LANG_STRINGS.get(lang, LANG_STRINGS["en"]).get(key, "")
    return s.format(**kw) if kw else s

# ================================================================
TB_HOSTS = (
    "terabox.com", "terabox.app", "terabox.link", "teraboxapp.com",
    "1024terabox.com", "4funbox.com", "mirrobox.com", "nephobox.com",
    "freeterabox.com", "1024tera.com", "momerybox.com", "tibibox.com",
    "terabox.fun", "4funbox.co", "terasharelink.com", "gibibox.com",
    "teraboxshare.com", "teraboxlink.com", "terasharefile.com",
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

LOG = logging.getLogger("terabot")
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(_h)

COOLDOWN_BY_UID: dict[int, float] = {}

# ================================================================
# helpers
# ================================================================
def find_terabox_url(text: str) -> Optional[str]:
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(").,;!?")
        if any(h in u.lower() for h in TB_HOSTS):
            return u
    return None


def file_icon(name: str) -> str:
    n = (name or "").lower()
    if n.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v")):
        return "🎬"
    if n.endswith((".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac")):
        return "🎵"
    if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")):
        return "🖼"
    if n.endswith((".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2")):
        return "🗜"
    if n.endswith((".pdf", ".doc", ".docx", ".txt", ".epub", ".mobi", ".csv")):
        return "📄"
    if n.endswith((".apk", ".exe", ".msi", ".dmg", ".deb", ".rpm")):
        return "💾"
    if n.endswith((".iso", ".img")):
        return "💿"
    return "📦"


def human_size(raw) -> str:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return str(raw) if raw else "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def parse_share_url(share_url: str) -> dict:
    pwd = ""
    m = re.search(r"[?&]pwd=([A-Za-z0-9]+)", share_url)
    if m:
        pwd = m.group(1)
    shorturl = ""
    m = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
    if m:
        shorturl = m.group(1)
    else:
        m = re.search(r"[?&]surl=([A-Za-z0-9_-]+)", share_url)
        if m:
            shorturl = m.group(1)
    if not shorturl:
        raise RuntimeError("cannot parse share url (no /s/<id> or surl=)")
    return {"shorturl": shorturl, "pwd": pwd}


def _pick(f: dict, *keys, default=""):
    for k in keys:
        v = f.get(k)
        if v not in (None, "", 0):
            return v
    return default


def _file_from_any(f: dict) -> Optional[dict]:
    if not isinstance(f, dict):
        return None
    if f.get("isdir") == 1:
        return None
    name = _pick(f, "filename", "server_filename", "name", "file_name", default="unknown")
    size_raw = _pick(f, "size", "file_size", default=None)
    size = _pick(f, "size_formatted", "formatted_size", "size_str",
                 "human_size", default=None) or human_size(size_raw)
    direct = _pick(f, "dlink", "downloadLink", "download_link",
                   "download_url", "download", default="N/A")
    stream = _pick(f, "stream_url", "streaming_link", "play_url",
                   "streamUrl", "streaming_url", "vip_stream_url", default="N/A")
    if (not stream or stream == "N/A") and direct and direct != "N/A":
        stream = direct
    return {"name": str(name), "size": str(size),
            "direct": str(direct), "stream": str(stream)}


def _files_from_list(items) -> list[dict]:
    out = []
    if isinstance(items, dict):
        items = items.get("list") or []
    for it in items or []:
        parsed = _file_from_any(it)
        if parsed:
            out.append(parsed)
    return out

# ================================================================
# tier 1 — shakir proxy (GET + POST)
# ================================================================
def fetch_via_shakir(share_url: str) -> list[dict]:
    parts = parse_share_url(share_url)
    surls = [parts["shorturl"]]
    if parts["shorturl"].startswith("1"):
        surls.append(parts["shorturl"][1:])

    attempts = []
    for surl in surls:
        base = {"mode": "resolve", "surl": surl, "raw": "1"}
        if parts["pwd"]:
            base["pwd"] = parts["pwd"]
        attempts.append(("GET", base, None))
        attempts.append(("POST", {}, {"url": share_url}))
        attempts.append(("POST", {}, {"surl": surl, "pwd": parts["pwd"]}))

    last_err = ""
    for method, params, body in attempts:
        try:
            if method == "GET":
                r = requests.get(SHAKIR_API, params=params,
                                 headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
            else:
                r = requests.post(SHAKIR_API, json=body,
                                  headers={"User-Agent": UA,
                                           "Content-Type": "application/json"},
                                  timeout=HTTP_TIMEOUT)
        except Exception as e:
            last_err = f"{method} {e}"
            continue
        if r.status_code != 200:
            last_err = f"{method} HTTP {r.status_code}: {r.text[:120]}"
            continue
        try:
            j = r.json()
        except Exception:
            last_err = f"{method} non-json"
            continue
        if j.get("errno", 0) not in (0, None) or j.get("success") is False:
            last_err = f"{method} errno {j.get('errno')}: {j.get('errmsg') or j.get('error') or ''}"
            continue
        files = _files_from_list(j.get("list") or j.get("files") or [])
        if files:
            return files
        last_err = f"{method} empty list"
    raise RuntimeError(f"shakir: {last_err or 'no files'}")

# ================================================================
# tier 2 — robin
# ================================================================
def fetch_via_robin(share_url: str) -> list[dict]:
    r = requests.get(ROBIN_API, params={"url": share_url},
                     headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"robin HTTP {r.status_code}: {r.text[:120]}")
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(j.get("error") or "robin success=false")
    files = []
    for f in j.get("files") or []:
        if not isinstance(f, dict):
            continue
        files.append({
            "name": str(f.get("file_name") or f.get("filename") or "unknown"),
            "size": str(f.get("size") or human_size(f.get("file_size"))),
            "direct": str(f.get("download_url") or f.get("original_download_url")
                          or f.get("download_link") or "N/A"),
            "stream": str(f.get("streaming_url") or f.get("play_url") or "N/A"),
        })
    if not files:
        raise RuntimeError("robin returned empty list")
    return files

# ================================================================
# tier 3 — jsToken + yunData sign/timestamp
# ================================================================
_JS_TOKEN_RE  = re.compile(r"fn%28%22([^%]+?)%22%29")
_JS_TOKEN_RE2 = re.compile(r"jsToken['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_\-]+)")
_YUNDATA_RE   = re.compile(r"yunData\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_SIGN_RE      = re.compile(r'"(?:sign|SIGN)"\s*:\s*"([^"]+)"')
_TS_RE        = re.compile(r'"(?:timestamp|TIMESTAMP)"\s*:\s*"?(\d+)"?')


def _scrape_page(sess, surl: str):
    candidates = [
        f"https://www.terabox.com/s/{surl}",
        f"https://www.terabox.com/sharing/link?surl={surl}",
        f"https://www.1024tera.com/s/{surl}",
    ]
    for page_url in candidates:
        try:
            r = sess.get(page_url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": UA,
                                  "Accept": "text/html,application/xhtml+xml"})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        text = r.text
        token = None
        m = _JS_TOKEN_RE.search(text) or _JS_TOKEN_RE2.search(text)
        if m:
            token = m.group(1)
        yun = {}
        ym = _YUNDATA_RE.search(text)
        if ym:
            try:
                data = json.loads(ym.group(1))
                if isinstance(data, dict):
                    if data.get("sign"):
                        yun["sign"] = data["sign"]
                    if data.get("timestamp"):
                        yun["timestamp"] = str(data["timestamp"])
            except Exception:
                pass
        if "sign" not in yun:
            sm = _SIGN_RE.search(text)
            if sm:
                yun["sign"] = sm.group(1)
        if "timestamp" not in yun:
            tm = _TS_RE.search(text)
            if tm:
                yun["timestamp"] = tm.group(1)
        if token or yun:
            return token, yun
    return None, {}


def _share_list_call(sess, surl, token, pwd, yun, dir_path="/"):
    params = {
        "app_id": "250528", "web": "1", "channel": "chunlei",
        "clienttype": "0", "root": "1" if dir_path == "/" else "0",
        "shorturl": surl, "dlink": "1",
    }
    if dir_path != "/":
        params["dir"] = dir_path
    if token:
        params["jsToken"] = token
    if pwd:
        params["pwd"] = pwd
    if yun.get("sign"):
        params["sign"] = yun["sign"]
    if yun.get("timestamp"):
        params["timestamp"] = yun["timestamp"]
    r = sess.get("https://www.terabox.com/share/list",
                 params=params, timeout=HTTP_TIMEOUT,
                 headers={"Referer": f"https://www.terabox.com/s/{surl}",
                          "Accept": "application/json, text/plain, */*"})
    r.raise_for_status()
    return r.json()


def fetch_via_jstoken(share_url: str) -> list[dict]:
    parts = parse_share_url(share_url)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "*/*"})
    token, yun = _scrape_page(sess, parts["shorturl"])
    if not token and not yun:
        raise RuntimeError("share page scrape empty")
    surls = [parts["shorturl"]]
    if parts["shorturl"].startswith("1"):
        surls.append(parts["shorturl"][1:])
    last_err = ""
    for surl in surls:
        try:
            j = _share_list_call(sess, surl, token, parts["pwd"], yun)
        except Exception as e:
            last_err = f"request: {e}"
            continue
        if j.get("errno", 0) != 0:
            last_err = f"errno {j.get('errno')}: {j.get('errmsg') or ''}"
            continue
        files = _files_from_list(j.get("list") or [])
        if files:
            return files
        last_err = "empty list"
    raise RuntimeError(f"jsToken: {last_err or 'no files'}")

# ================================================================
# tier 4 — ndus cookie (dlink this is the one that works)
# ================================================================
def _cookie_list_dir(sess, shorturl, dir_path, pwd, yun, depth=0):
    if depth > 3:
        return []
    try:
        j = _share_list_call(sess, shorturl, None, pwd, yun, dir_path)
    except Exception as e:
        raise RuntimeError(f"cookie list dir={dir_path}: {e}")
    if j.get("errno", 0) != 0:
        raise RuntimeError(f"cookie share/list errno {j.get('errno')}: {j.get('errmsg') or ''}")
    out = []
    for item in j.get("list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("isdir") == 1:
            out.extend(_cookie_list_dir(sess, shorturl,
                                        item.get("path") or "/", pwd, yun, depth + 1))
        else:
            parsed = _file_from_any(item)
            if parsed:
                out.append(parsed)
    return out


def fetch_via_cookie(share_url: str) -> list[dict]:
    if not TERABOX_COOKIE:
        raise RuntimeError("TERABOX_COOKIE not set — dlink needs login session")
    parts = parse_share_url(share_url)
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Cookie": TERABOX_COOKIE,
        "Referer": f"https://www.terabox.com/sharing/link?surl={parts['shorturl']}",
        "Accept": "application/json, text/plain, */*",
    })
    _, yun = _scrape_page(sess, parts["shorturl"])
    files = _cookie_list_dir(sess, parts["shorturl"], "/", parts["pwd"], yun, 0)
    if not files:
        raise RuntimeError("cookie path empty (cookie may be expired)")
    return files

# ================================================================
# tier 5 — solver proxy
# ================================================================
def _solve_turnstile(retries=2):
    for i in range(retries):
        try:
            r = requests.post(
                SOLVER_URL.rstrip("/") + "/solve",
                json={"sitekey": TS_SITEKEY, "siteurl": TB_BASE, "timeout": 60},
                timeout=80,
            )
            if r.status_code == 200:
                tok = r.json().get("token")
                if tok:
                    return tok
        except Exception as e:
            LOG.warning("solver attempt %d: %s", i + 1, e)
        time.sleep(1)
    return None


def fetch_via_proxy(share_url: str) -> list[dict]:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    r = s.get(TB_BASE + "/", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    token = _solve_turnstile()
    if not token:
        raise RuntimeError("solver returned no token")
    s.post(TB_BASE + "/api/verify", json={"token": token},
           headers={"Origin": TB_BASE, "Referer": TB_BASE + "/",
                    "Content-Type": "application/json"},
           timeout=HTTP_TIMEOUT)
    if "tdl_pass" not in s.cookies:
        raise RuntimeError("verify rejected (no tdl_pass)")
    r = s.post(TB_BASE + "/api/proxy", json={"url": share_url},
               headers={"Origin": TB_BASE, "Referer": TB_BASE + "/",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/plain, */*"},
               timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("errno", 0) != 0:
        raise RuntimeError(j.get("errmsg") or f"proxy errno {j.get('errno')}")
    files = _files_from_list(j.get("list") or [])
    if not files:
        raise RuntimeError("proxy returned empty list")
    return files

# ================================================================
# orchestrator — cookie tier FIRST when set (it's the only one that yields dlink)
# ================================================================
def fetch_share_meta(share_url: str):
    errors = []
    tiers = []
    if TERABOX_COOKIE:
        tiers.append(("cookie", fetch_via_cookie))
    tiers.extend([
        ("shakir",  fetch_via_shakir),
        ("robin",   fetch_via_robin),
        ("jsToken", fetch_via_jstoken),
    ])
    if not TERABOX_COOKIE:
        tiers.append(("cookie", fetch_via_cookie))

    for name, fn in tiers:
        try:
            files = fn(share_url)
            LOG.info("tier %s ok (%d files)", name, len(files))
            return files, name
        except Exception as e:
            LOG.warning("tier %s failed: %s", name, e)
            errors.append(f"{name}: {e}")
    if SOLVER_URL:
        try:
            return fetch_via_proxy(share_url), "proxy"
        except Exception as e:
            LOG.warning("tier proxy failed: %s", e)
            errors.append(f"proxy: {e}")
    raise RuntimeError(" | ".join(errors))

# ================================================================
# render
# ================================================================
def render_chunk(chunk, offset, total, source, lang) -> tuple[str, InlineKeyboardMarkup]:
    label = t(lang, "files_label")
    showing = t(lang, "showing")
    lines = [
        "╭───────────────────────╮",
        "   <b>T E R A B O X  ·  D L</b>",
        "╰───────────────────────╯",
        "",
        f"📦 <b>{total}</b> {label}  ·  {showing} <b>{offset + 1}–{offset + len(chunk)}</b>",
        "",
    ]
    kb = InlineKeyboardBuilder()
    for i, f in enumerate(chunk):
        idx = offset + i + 1
        lines.append(f"<b>{idx}.</b>  {file_icon(f['name'])} <code>{html.escape(f['name'])}</code>")
        lines.append(f"       └ <i>{html.escape(f['size'])}</i>")
        lines.append("")
        dl, st = f.get("direct") or "", f.get("stream") or ""
        row = []
        if dl and dl != "N/A":
            row.append(InlineKeyboardButton(text=f"📥  {idx}", url=dl))
        if st and st != "N/A":
            row.append(InlineKeyboardButton(text=f"▶️  {idx}", url=st))
        if row:
            kb.row(*row)
    if source:
        kb.row(InlineKeyboardButton(text="🔗  open source", url=source))
    return "\n".join(lines), kb.as_markup()

# ================================================================
# aiogram
# ================================================================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


@router.message(CommandStart())
async def cmd_start(msg: Message):
    lang = lang_for(msg)
    await msg.answer(
        "╭───────────────────────╮\n"
        "   <b>T E R A B O X  ·  B O T</b>\n"
        "╰───────────────────────╯\n\n"
        + t(lang, "welcome_body"),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await cmd_start(msg)


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"<code>{msg.from_user.id}</code>", parse_mode=ParseMode.HTML)


@router.message(Command("lang"))
async def cmd_lang(msg: Message):
    lang = lang_for(msg)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
        InlineKeyboardButton(text="🇧🇩 বাংলা",   callback_data="lang:bn"),
    ]])
    await msg.answer(t(lang, "lang_pick"), reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(cb: CallbackQuery):
    new = cb.data.split(":", 1)[1]
    if new not in LANG_STRINGS:
        await cb.answer()
        return
    USER_LANG[cb.from_user.id] = new
    try:
        await cb.message.edit_text(t(new, "lang_set"), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await cb.answer()


@router.message(F.text)
async def on_text(msg: Message):
    url = find_terabox_url(msg.text or "")
    if not url:
        return
    lang = lang_for(msg)
    uid = msg.from_user.id
    now = time.time()
    last = COOLDOWN_BY_UID.get(uid, 0)
    if now - last < COOLDOWN:
        await msg.reply(t(lang, "slow_down", n=int(COOLDOWN - (now - last))))
        return
    COOLDOWN_BY_UID[uid] = now

    status = await msg.reply(t(lang, "extracting"), parse_mode=ParseMode.HTML)
    try:
        files, tier = fetch_share_meta(url)
    except Exception as e:
        LOG.exception("fetch failed")
        try:
            await status.edit_text(
                "╭───────────────────────╮\n"
                "   <b>E X T R A C T   F A I L E D</b>\n"
                "╰───────────────────────╯\n\n"
                f"<code>{html.escape(str(e))[:700]}</code>\n\n"
                f"<i>{t(lang, 'failed_hint')}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass

    total = len(files)
    for start in range(0, total, CHUNK):
        text, kb = render_chunk(files[start:start + CHUNK], start, total, url, lang)
        try:
            await msg.answer(text, parse_mode=ParseMode.HTML,
                             reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            LOG.exception("send failed: %s", e)
            break

# ================================================================
# fastapi
# ================================================================
app = FastAPI()


@app.get("/")
async def root():
    return {"service": "terabox-bot", "status": "ok",
            "webhook": "/api/webhook",
            "test": "/api/test?url=...",
            "debug": "/api/debug?url=..."}


@app.get("/api/test")
async def test_extract(url: str):
    try:
        files, tier = fetch_share_meta(url)
        return {"ok": True, "tier": tier, "count": len(files), "files": files,
                "cookie_set": bool(TERABOX_COOKIE)}
    except Exception as e:
        return {"ok": False, "error": str(e), "cookie_set": bool(TERABOX_COOKIE)}


@app.get("/api/debug")
async def debug_extract(url: str):
    try:
        parts = parse_share_url(url)
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept": "*/*"})
        token, yun = _scrape_page(sess, parts["shorturl"])
        raw = _share_list_call(sess, parts["shorturl"], token, parts["pwd"], yun)
        return {
            "ok": True,
            "jsToken_found": bool(token),
            "yun": yun,
            "cookies_set": list(sess.cookies.get_dict().keys()),
            "raw_errno": raw.get("errno"),
            "raw_list": raw.get("list"),
            "cookie_configured": bool(TERABOX_COOKIE),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/webhook")
async def webhook(request: Request) -> Response:
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return Response(status_code=403)
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)


@app.get("/api/webhook")
async def health():
    return {"status": "ok", "service": "terabox-bot"}
