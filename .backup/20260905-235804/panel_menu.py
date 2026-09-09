from aiogram import F, Router
from aiogram.filters import Command

import config
from panel_control import status_text
from panel_kb import kb_auto, kb_main, kb_run, kb_sessions, kb_status, kb_texts
from panel_ui import admin_only, edit, reset_state
from story_refs import load_story_refs

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)


@router.message(Command("start"))
async def cmd_start(message):
    await message.answer("Главное меню:", reply_markup=kb_main())


@router.callback_query(F.data.startswith("nav:"))
async def nav(cb):
    key = cb.data.split(":", 1)[1]
    reset_state(cb.from_user.id)
    if key == "main":
        await edit(cb.message, "Главное меню:", kb_main())
    elif key == "sessions":
        await edit(cb.message, "📦 Сессии:", kb_sessions())
    elif key == "texts":
        n = len(load_story_refs(config.STORIES_FILE))
        await edit(cb.message, f"📖 Stories (рассылка): в пуле <b>{n}</b>", kb_texts())
    elif key == "run":
        await edit(cb.message, "⚙️ Рассылка:", kb_run())
    elif key == "status":
        await edit(cb.message, status_text(), kb_status())
    elif key == "auto":
        await edit(cb.message, "🤖 Авто:", kb_auto())
    await cb.answer()
