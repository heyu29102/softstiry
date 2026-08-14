from aiogram import Router

from panel_ui import admin_only

router = Router()
router.message.filter(admin_only)


@router.message()
async def fallback(message):
    await message.answer("Меню: /start")
