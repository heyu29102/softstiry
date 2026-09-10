import asyncio

from aiogram import F, Router
from aiogram.types import FSInputFile

import config
from panel_control import app_running, esc, read_tail, start_app, stop_app
from panel_kb import kb_run
from panel_ui import admin_only

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)

control_lock = asyncio.Lock()


@router.callback_query(F.data == "toggle")
async def cb_toggle(cb):
    async with control_lock:
        err = stop_app() if app_running() else start_app()
    await cb.message.answer(esc(err) if err else "Готово.")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb_run())
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "restart")
async def cb_restart(cb):
    async with control_lock:
        stop_app()
        await asyncio.sleep(1)
        err = start_app()
    await cb.message.answer(esc(err) if err else "♻️ Перезапущен.")
    await cb.answer()


@router.callback_query(F.data == "tail")
async def cb_tail(cb):
    text = read_tail(config.APP_LOG, 80)
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)][:5] or [text]:
        await cb.message.answer(f"<pre>{esc(chunk)}</pre>")
    await cb.answer()


@router.callback_query(F.data == "dl")
async def cb_dl(cb):
    if config.APP_LOG.exists():
        await cb.message.answer_document(FSInputFile(str(config.APP_LOG)))
    else:
        await cb.message.answer("Лог пуст.")
    await cb.answer()
