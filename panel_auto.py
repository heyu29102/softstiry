import asyncio
import os

from aiogram import F, Router

import config
from panel_api import api_monitor
from panel_control import esc
from panel_kb import kb_auto
from panel_ui import admin_only, edit

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)


async def svc(verb):
    if os.name == "nt":
        return "Доступно только на Linux."
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", verb, config.AUTO_SERVICE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        raw = (out or err).decode("utf-8", "ignore").strip()
        return f"systemctl {verb} {config.AUTO_SERVICE} — код {proc.returncode}\n<pre>{esc(raw)}</pre>" if raw \
            else f"systemctl {verb} {config.AUTO_SERVICE} — код {proc.returncode}"
    except Exception as e:
        return f"Ошибка: {esc(e)}"


@router.callback_query(F.data == "api_toggle")
async def cb_api(cb):
    if not api_monitor["on"]:
        if not config.API_TOKEN:
            await cb.answer("Не задан API_TOKEN в .env.", show_alert=True)
            return
        if config.TARGET_USER_ID <= 0:
            await cb.answer("Не задан TARGET_USER_ID в .env.", show_alert=True)
            return
    api_monitor["on"] = not api_monitor["on"]
    await edit(cb.message, "🤖 Авто:", kb_auto())
    await cb.answer()


@router.callback_query(F.data.startswith("svc:"))
async def cb_svc(cb):
    await cb.message.answer(await svc(cb.data.split(":", 1)[1]))
    await cb.answer()
