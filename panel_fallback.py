from aiogram import Router

from panel_control import esc
from panel_texts import toggle_story_line
from panel_ui import admin_only, state
import config
from story_refs import load_story_refs, parse_story_line

router = Router()
router.message.filter(admin_only)


def _story_quick_reply(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if not all(parse_story_line(ln) for ln in lines):
        return None
    added = removed = 0
    labels_add, labels_rm = [], []
    total = len(load_story_refs(config.STORIES_FILE))
    for line in lines:
        action, total, label = toggle_story_line(line)
        if action == "added":
            added += 1
            labels_add.append(label)
        elif action == "removed":
            removed += 1
            labels_rm.append(label)
    rows = [
        f"📖 Story из сообщения: +{added} / −{removed}",
        f"В пуле: <b>{total}</b>",
    ]
    for lb in labels_add[:5]:
        rows.append(f"✅ <code>{esc(lb)}</code>")
    for lb in labels_rm[:5]:
        rows.append(f"🗑 <code>{esc(lb)}</code>")
    return "\n".join(rows)


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    text = (message.text or "").strip()
    quick = _story_quick_reply(text)
    if quick:
        await message.answer(quick)
        return
    await message.answer("Меню: /start\nИли кинь ссылку story: <code>https://t.me/channel/s/1</code>")
