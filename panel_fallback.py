from aiogram import Router

from panel_control import esc
from panel_texts import story_pool_path, story_pool_title, toggle_story_line
from panel_ui import admin_only, state
from story_refs import load_story_refs, parse_story_line

router = Router()
router.message.filter(admin_only)


def _story_quick_reply_for_user(user_id, text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if not all(parse_story_line(ln) for ln in lines):
        return None

    wait = state(user_id).get("wait")
    if wait == "story_toggle:group":
        kinds = ["group"]
    elif wait == "story_toggle:contact":
        kinds = ["contact"]
    else:
        kinds = ["group", "contact"]

    rows = []
    for pool_kind in kinds:
        added = removed = 0
        labels_add, labels_rm = [], []
        total = len(load_story_refs(story_pool_path(pool_kind)))
        for line in lines:
            action, total, label = toggle_story_line(line, pool_kind)
            if action == "added":
                added += 1
                labels_add.append(label)
            elif action == "removed":
                removed += 1
                labels_rm.append(label)
        rows.append(f"📖 {story_pool_title(pool_kind)}: +{added} / −{removed} (в пуле <b>{total}</b>)")
        for lb in labels_add[:3]:
            rows.append(f"  ✅ <code>{esc(lb)}</code>")
        for lb in labels_rm[:3]:
            rows.append(f"  🗑 <code>{esc(lb)}</code>")
    return "\n".join(rows)


@router.message()
async def fallback(message):
    if state(message.from_user.id).get("wait"):
        return
    text = (message.text or "").strip()
    quick = _story_quick_reply_for_user(message.from_user.id, text)
    if quick:
        await message.answer(quick)
        return
    await message.answer(
        "Меню: /start\n"
        "Или кинь ссылку story — добавится в <b>оба</b> пула:\n"
        "<code>https://t.me/channel/s/1</code>\n"
        "Для одного пула: меню → Истории → Добавить"
    )
