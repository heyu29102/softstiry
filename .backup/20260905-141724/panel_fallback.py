from aiogram import Router

from panel_control import esc
from panel_ui import admin_only, state
import config
from story_refs import add_story_ref, load_story_refs, normalize_story_input, parse_story_line

router = Router()
router.message.filter(admin_only)


def _extract_message_text(message) -> str:
    text = (message.text or message.caption or "").strip()
    if text:
        return text
    for ent in message.entities or message.caption_entities or []:
        if ent.type in ("url", "text_link"):
            part = (message.text or message.caption or "")[ent.offset : ent.offset + ent.length]
            if part:
                return part.strip()
    return ""


def _story_quick_reply(text):
    lines = [normalize_story_input(ln) for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if not all(parse_story_line(ln) for ln in lines):
        return None
    added = exists = 0
    labels_add = []
    total = len(load_story_refs(config.STORIES_FILE))
    for line in lines:
        try:
            status, total, label = add_story_ref(line, config.STORIES_FILE)
        except Exception as e:
            return f"❌ Ошибка записи stories.txt: <code>{esc(e)}</code>"
        if status == "added":
            added += 1
            labels_add.append(label)
        elif status == "exists":
            exists += 1
    rows = [
        f"📖 Story из сообщения: +{added} | уже в пуле: {exists}",
        f"В пуле: <b>{total}</b>",
    ]
    for lb in labels_add[:5]:
        rows.append(f"✅ <code>{esc(lb)}</code>")
    return "\n".join(rows)


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    text = _extract_message_text(message)
    if not text:
        await message.answer("Меню: /start\nИли кинь ссылку story: <code>https://t.me/channel/s/1</code>")
        return
    quick = _story_quick_reply(text)
    if quick:
        await message.answer(quick)
        return
    await message.answer("Меню: /start\nИли кинь ссылку story: <code>https://t.me/channel/s/1</code>")
