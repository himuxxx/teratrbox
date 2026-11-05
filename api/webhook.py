# api/webhook.py — Vercel Python entry. routes Telegram updates into aiogram.
from fastapi import FastAPI, Request, Response
from aiogram.types import Update

from bot import build_dispatcher, WEBHOOK_SECRET

app = FastAPI()
bot, dp = build_dispatcher()


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
