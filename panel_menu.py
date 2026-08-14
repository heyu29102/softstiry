from aiogram import F, Router
from aiogram.filters import Command

from panel_control import status_text
from panel_kb import kb_auto, kb_main, kb_run, kb_sessions, kb_status, kb_texts
from panel_ui import admin_only, edit

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)


@router.message(Command("start"))
async def cmd_start(message):
    await message.answer("Главное меню:", reply_markup=kb_main())


@router.callback_query(F.data.startswith("nav:"))
async def nav(cb):
    key = cb.data.split(":", 1)[1]
    if key == "main":
        await edit(cb.message, "Главное меню:", kb_main())
    elif key == "sessions":
        await edit(cb.message, "📦 Сессии:", kb_sessions())
    elif key == "texts":
        await edit(cb.message, "📖 Истории (stories.txt):", kb_texts())
    elif key == "run":
        await edit(cb.message, "⚙️ Рассылка:", kb_run())
    elif key == "status":
        await edit(cb.message, status_text(), kb_status())
    elif key == "auto":
        await edit(cb.message, "🤖 Авто:", kb_auto())
    await cb.answer()
