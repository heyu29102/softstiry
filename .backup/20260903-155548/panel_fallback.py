from aiogram import Router

from panel_ui import admin_only, state

router = Router()
router.message.filter(admin_only)


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    await message.answer(
        "Меню: /start\n"
        "Контент: text.txt + Photo/\n"
        "Рассылка: фото+текст только в группы/супергруппы"
    )
