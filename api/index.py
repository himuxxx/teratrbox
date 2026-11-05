# api/index.py — Terabox -> Telegram bot (multi-source, Vercel serverless)
# tier1: terabox-worker.robinkumarshakya103.workers.dev (free public worker)
# tier2: terabox.com/share/list with optional ndus cookie
# tier3: solver proxy (teraboxdl.site) if SOLVER_URL set

import html
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

# Optional — paste "ndus=..." from terabox.com login cookie
# browser -> devtools -> Application -> Cookies -> terabox.com -> ndus
TERABOX_COOKIE = ""

# Optional — remote solver (needs VPS, not Vercel)
SOLVER_URL = ""
TB_BASE    = "https://teraboxdl.site"
TS_SITEKEY = "0x4AAAAAAFZJOCzA6dP3BIYH"

HNN_API = "https://terabox-worker.robinkumarshakya103.workers.dev/api"

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36")

HTTP_TIMEOUT = 25
COOLDOWN     = 3.0
CHUNK        = 5

# ================================================================
TB_HOSTS = (
    "terabox.com", "terabox.app", "terabox.link", "teraboxapp.com",
    "1024terabox.com", "4funbox.com", "mirrobox.com", "nephobox.com",
    "freeterabox.com", "1024tera.com", "momerybox.com", "tibibox.com",
    "terabox.fun", "4funbox.co", "terasharelink.com", "gibibox.com",
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
                   "streamUrl", "streaming_url", default="N/A")
    return {"name": str(name), "size": str(size),
            "direct": str(direct), "stream": str(stream)}

# ================================================================
# tier 1 — public free worker
# ================================================================
def fetch_via_worker(share_url: str) -> list[dict]:
    r = requests.get(
        HNN_API,
        params={"url": share_url},
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    j = r.json()

    if not j.get("success"):
        raise RuntimeError(j.get("error") or "worker returned success=false")

    files_raw = j.get("files") or []
    out: list[dict] = []
    for f in files_raw:
        if not isinstance(f, dict):
            continue
        name = f.get("file_name") or f.get("filename") or "unknown"
        size = f.get("size") or human_size(f.get("file_size"))
        direct = (f.get("download_url") or f.get("original_download_url")
                  or f.get("download_link") or "N/A")
        stream = f.get("streaming_url") or f.get("play_url") or "N/A"
        out.append({"name": str(name), "size": str(size),
                    "direct": str(direct), "stream": str(stream)})

    if not out:
        raise RuntimeError("worker returned empty file list")
    return out

# ================================================================
# tier 2 — cookie direct (terabox.com/share/list, recursive)
# ================================================================
def _list_dir(sess, shorturl: str, dir_path: str, pwd: str, depth: int = 0) -> list[dict]:
    if depth > 3:
        return []
    r = sess.get(
        "https://www.terabox.com/share/list",
        params={"app_id": "250528", "web": "1", "channel": "dubox",
                "clienttype": "0", "root": "1" if dir_path == "/" else "0",
                "shorturl": shorturl, "dir": dir_path, "pwd": pwd},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("errno", 0) != 0:
        raise RuntimeError(f"share/list errno {j.get('errno')}: {j.get('errmsg') or ''}")

    out: list[dict] = []
    for item in j.get("list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("isdir") == 1:
            out.extend(_list_dir(sess, shorturl, item.get("path") or "/", pwd, depth + 1))
        else:
            parsed = _file_from_any(item)
            if parsed:
                out.append(parsed)
    return out


def fetch_via_cookie(share_url: str) -> list[dict]:
    if not TERABOX_COOKIE:
        raise RuntimeError("TERABOX_COOKIE not set")
    parts = parse_share_url(share_url)
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Cookie": TERABOX_COOKIE,
        "Referer": f"https://www.terabox.com/sharing/link?surl={parts['shorturl']}",
        "Accept": "application/json, text/plain, */*",
    })
    files = _list_dir(sess, parts["shorturl"], "/", parts["pwd"], 0)
    if not files:
        raise RuntimeError("cookie path returned empty list (cookie may be expired)")
    return files

# ================================================================
# tier 3 — solver proxy
# ================================================================
def _solve_turnstile(retries: int = 2) -> Optional[str]:
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
    files = [x for x in (_file_from_any(it) for it in (j.get("list") or [])) if x]
    if not files:
        raise RuntimeError("proxy returned empty list")
    return files

# ================================================================
# orchestrator
# ================================================================
def fetch_share_meta(share_url: str) -> list[dict]:
    errors: list[str] = []

    try:
        return fetch_via_worker(share_url)
    except Exception as e:
        LOG.warning("worker failed: %s", e)
        errors.append(f"worker: {e}")

    try:
        return fetch_via_cookie(share_url)
    except Exception as e:
        LOG.warning("cookie path failed: %s", e)
        errors.append(f"cookie: {e}")

    if SOLVER_URL:
        try:
            return fetch_via_proxy(share_url)
        except Exception as e:
            LOG.warning("proxy failed: %s", e)
            errors.append(f"proxy: {e}")

    raise RuntimeError(" | ".join(errors))

# ================================================================
# render
# ================================================================
def render_chunk(chunk, offset, total, source) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        "╭───────────────────────╮",
        "   <b>T E R A B O X  ·  D L</b>",
        "╰───────────────────────╯",
        "",
        f"📦 <b>{total}</b> file(s)  ·  showing <b>{offset + 1}–{offset + len(chunk)}</b>",
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
    await msg.answer(
        "╭───────────────────────╮\n"
        "   <b>T E R A B O X  ·  B O T</b>\n"
        "╰───────────────────────╯\n\n"
        "send a terabox share link.\n"
        "i'll return <b>direct download</b> and <b>stream</b> buttons per file.\n\n"
        "<b>/help</b>  ·  <b>/id</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await cmd_start(msg)


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"<code>{msg.from_user.id}</code>", parse_mode=ParseMode.HTML)


@router.message(F.text)
async def on_text(msg: Message):
    url = find_terabox_url(msg.text or "")
    if not url:
        return

    uid = msg.from_user.id
    now = time.time()
    last = COOLDOWN_BY_UID.get(uid, 0)
    if now - last < COOLDOWN:
        await msg.reply(f"⏳ slow down — {int(COOLDOWN - (now - last))}s")
        return
    COOLDOWN_BY_UID[uid] = now

    status = await msg.reply("<b>⚡ extracting…</b>", parse_mode=ParseMode.HTML)

    try:
        files = fetch_share_meta(url)
    except Exception as e:
        LOG.exception("fetch failed")
        try:
            await status.edit_text(
                "╭───────────────────────╮\n"
                "   <b>E X T R A C T   F A I L E D</b>\n"
                "╰───────────────────────╯\n\n"
                f"<code>{html.escape(str(e))[:600]}</code>",
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
        text, kb = render_chunk(files[start:start + CHUNK], start, total, url)
        try:
            await msg.answer(text, parse_mode=ParseMode.HTML,
                             reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            LOG.exception("send failed: %s", e)
            break

# ================================================================
# fastapi entrypoint
# ================================================================
app = FastAPI()


@app.get("/")
async def root():
    return {"service": "terabox-bot", "status": "ok",
            "webhook": "/api/webhook", "test": "/api/test?url=..."}


@app.get("/api/test")
async def test_extract(url: str):
    """Browser-test: /api/test?url=<terabox share url>. Returns raw JSON."""
    try:
        files = fetch_share_meta(url)
        return {"ok": True, "count": len(files), "files": files}
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
