from aiogram import Router

from panel_texts import toggle_domain_line
from panel_ui import admin_only, state
import config

router = Router()
router.message.filter(admin_only)


def _domain_quick_reply(text):
    line = text.strip().splitlines()[0].strip() if text else ""
    if not line or " " in line:
        return None
    if "." not in line and "|" not in line:
        return None
    try:
        action, total, url, _ = toggle_domain_line(line)
    except Exception:
        return None
    if action is None:
        return None
    verb = "Добавлен" if action == "added" else "Удалён"
    return f"🌐 {verb}: <code>{url}</code>\nВ пуле доменов: <b>{total}</b>"


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    text = (message.text or "").strip()
    quick = _domain_quick_reply(text)
    if quick:
        await message.answer(quick)
        return
    await message.answer(
        "Меню: /start\n"
        "Или кинь домен — добавлю в domains.txt"
    )
