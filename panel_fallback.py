from aiogram import Router

from panel_control import esc
from panel_texts import normalize_share_link, toggle_share_line
from panel_ui import admin_only, state
import config

router = Router()
router.message.filter(admin_only)


def _link_quick_reply(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if not all(normalize_share_link(ln) for ln in lines):
        return None
    if not all(
        normalize_share_link(ln).startswith("http")
        for ln in lines
    ):
        return None
    added = removed = 0
    total = len(config.load_share_links())
    for line in lines:
        action, total = toggle_share_line(line)
        if action == "added":
            added += 1
        elif action == "removed":
            removed += 1
    return (
        f"🔗 Ссылки из сообщения: +{added} / −{removed}\n"
        f"В пуле: <b>{total}</b>"
    )


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    text = (message.text or "").strip()
    quick = _link_quick_reply(text)
    if quick:
        await message.answer(quick)
        return
    await message.answer(
        "Меню: /start\n"
        "Или кинь ссылку (t.me / share.google) — добавлю в пул."
    )
