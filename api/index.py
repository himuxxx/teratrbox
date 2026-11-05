# api/index.py — Terabox -> Telegram bot (solver-proxy, single path)
# Flow from TeraboxCLI.py: solver /solve -> teraboxdl.site /api/verify -> /api/proxy
# Requires SOLVER_URL reachable from Vercel (external host, not Vercel)

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

# config
BOT_TOKEN      = "8426186448:AAEJCf9mGHyZydX1TaJRWLRzvn50SlUyQw8"
WEBHOOK_SECRET = "tb_71x_smith_secret"

SOLVER_URL = "http://85.115.209.225:8191"
TB_BASE    = "https://teraboxdl.site"
TS_SITEKEY = "0x4AAAAAAFZJOCzA6dP3BIYH"

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36")

SOLVER_TIMEOUT = 90
HTTP_TIMEOUT   = 45
COOLDOWN       = 5.0
CHUNK          = 5
