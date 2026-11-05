# api/index.py — Terabox -> Telegram bot (original external solver)
# flow: solver /solve -> teraboxdl.site /api/verify -> /api/proxy

import html
import logging
import re
import secrets
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
BOT_TOKEN      = "8426186448:AAE2TXh2TfgEWK2CDN96DfVuDmYfNLe-IkA"
WEBHOOK_SECRET = "tb_71x_smith_secret"

SOLVER_URL = "http://85.115.209.225:8191"
TB_BASE    = "https://teraboxdl.site"
TS_SITEKEY = "0x4AAAAAAFZJOCzA6dP3BIYH"

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36")

SOLVER_TIMEOUT = 90
COOLDOWN       = 5.0
CHUNK          = 5

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
COPY_CACHE: dict[str, str] = {}

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


def _clip(s: str, n: int = 42) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 3] + "…"

# ================================================================
# solver flow
# ================================================================
def _solve_turnstile(retries: int = 3) -> Optional[str]:
    last_err = "unknown error"
    for i in range(retries):
        try:
            r = requests.post(
                SOLVER_URL.rstrip("/") + "/solve",
                json={"sitekey": TS_SITEKEY, "siteurl": TB_BASE, "timeout": 60},
                timeout=SOLVER_TIMEOUT,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                LOG.warning(last_err)
                continue
            tok = r.json().get("token")
            if tok:
                return tok
            else:
                last_err = f"json no token: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
            LOG.warning(f"solver attempt {i + 1}: {e}")
        time.sleep(1)
    
    raise RuntimeError(f"Solver failed after {retries} tries: {last_err}")


def fetch_share_meta(share_url: str) -> list[dict]:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})

    r = s.get(TB_BASE + "/", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"base HTTP {r.status_code}")

    token = _solve_turnstile()

    s.post(
        TB_BASE + "/api/verify",
        json={"token": token},
        headers={
            "Origin": TB_BASE,
            "Referer": TB_BASE + "/",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    if "tdl_pass" not in s.cookies:
        raise RuntimeError("verify rejected (no tdl_pass cookie)")

    r = s.post(
        TB_BASE + "/api/proxy",
        json={"url": share_url},
        headers={
            "Origin": TB_BASE,
            "Referer": TB_BASE + "/",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"proxy HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    if j.get("errno", 0) != 0:
        raise RuntimeError(j.get("errmsg") or f"proxy errno {j.get('errno')}")

    files: list[dict] = []
    for item in j.get("list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("isdir") == 1:
            continue
        files.append({
            "name":   item.get("server_filename") or "unknown",
            "size":   item.get("formatted_size") or "?",
            "direct": item.get("direct_link") or "N/A",
            "stream": item.get("stream_url") or "N/A",
        })
    if not files:
        raise RuntimeError("proxy returned empty list")
    return files

# ================================================================
# render
# ================================================================
def render_chunk(chunk, offset, total) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (total + CHUNK - 1) // CHUNK)
    page = offset // CHUNK + 1

    lines = [
        "          🔥 <b>T E R A B O X</b> 🔥          ",
        "",
    ]

    kb = InlineKeyboardBuilder()

    for i, f in enumerate(chunk):
        name = f.get("name") or "unknown"
        size = f.get("size") or "?"
        dl = f.get("direct") or ""
        st = f.get("stream") or ""

        chips = []
        if dl and dl != "N/A":
            chips.append("🔵 <b>DL</b>")
        if st and st != "N/A":
            chips.append("🔴 <b>STR</b>")
        chip_line = "  ·  ".join(chips) if chips else "<i>no links</i>"

        lines.append(f"{file_icon(name)} <code>{html.escape(_clip(name, 42))}</code>")
        lines.append(f"💾 <b>{html.escape(size)}</b>  ·  {chip_line}")
        lines.append("")

        row1 = []
        if dl and dl != "N/A":
            row1.append(InlineKeyboardButton(text="📥 Download Now", url=dl))
        if st and st != "N/A":
            row1.append(InlineKeyboardButton(text="🎬 Stream Now", url=st))
        if row1:
            kb.row(*row1)

        row2 = []
        if dl and dl != "N/A":
            tok = secrets.token_hex(6)
            COPY_CACHE[tok] = dl
            row2.append(InlineKeyboardButton(text="📋 Copy DL Link", callback_data=f"copy:{tok}"))
        if st and st != "N/A":
            tok = secrets.token_hex(6)
            COPY_CACHE[tok] = st
            row2.append(InlineKeyboardButton(text="📋 Copy Stream Link", callback_data=f"copy:{tok}"))
        if row2:
            kb.row(*row2)

        if len(COPY_CACHE) > 1000:
            for k in list(COPY_CACHE.keys())[:200]:
                COPY_CACHE.pop(k, None)

    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖➖➖")
    lines.append(
        f"📦 <b>{total}</b> file{'s' if total != 1 else ''}"
        f"   ·   📄 page <b>{page}</b>/<b>{pages}</b>"
    )
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖➖➖")

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
        "          🔥 <b>T E R A B O X</b> 🔥          \n\n"
        "  <b>drop a share link. get links.</b>\n\n"
        "  🔵  <b>Download Now</b>  —  save file\n"
        "  🔴  <b>Stream Now</b>    —  play instantly\n\n"
        "  <i>supported: terabox.com · 1024terabox.com\n"
        "  terabox.app · freeterabox.com · 4funbox.com</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await cmd_start(msg)


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"<code>{msg.from_user.id}</code>", parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("copy:"))
async def cb_copy(cb: CallbackQuery):
    tok = cb.data.split(":", 1)[1]
    link = COPY_CACHE.get(tok)
    if not link:
        await cb.answer("Link expired, please re-extract the URL.", show_alert=True)
        return
    
    await cb.message.answer(
        f"<code>{html.escape(link)}</code>\n\n"
        "<i>👆 tap the text above to copy</i>",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer("Sent!")


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

    status = await msg.reply(
        "⏳ <b>Processing your link...</b>\n"
        "<i>please wait ~20s</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        files = fetch_share_meta(url)
    except Exception as e:
        LOG.exception("fetch failed")
        try:
            await status.edit_text(
                "❌ <b>Extraction Failed</b>\n\n"
                f"<code>{html.escape(str(e))[:600]}</code>\n\n"
                "<i>link expired / private / solver down</i>",
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
        text, kb = render_chunk(files[start:start + CHUNK], start, total)
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
            "test": "/api/test?url=..."}


@app.get("/api/test")
async def test_extract(url: str):
    try:
        t0 = time.time()
        files = fetch_share_meta(url)
        return {"ok": True, "elapsed_s": round(time.time() - t0, 1),
                "count": len(files), "files": files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/solver-health")
async def solver_health():
    try:
        r = requests.get(SOLVER_URL.rstrip("/") + "/health", timeout=10)
        return {"ok": True, "status_code": r.status_code, "body": r.json()}
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
