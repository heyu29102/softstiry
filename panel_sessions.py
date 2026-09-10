import re
import shutil
from pathlib import Path

from aiogram import Bot, F, Router

import config
from panel_control import clean_name, count_sessions, esc, rm, temp_path
from panel_import import RAR_OK, archive_has_session, import_package, texts_from_upload
from panel_ui import admin_only, blocks_from, del_msg, state

PHONE_RE = re.compile(r"Телефон:\s*([^\n]+)")

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)


@router.callback_query(F.data == "count")
async def cb_count(cb):
    await cb.message.answer(f"📊 Сессий: <b>{count_sessions()}</b>")
    await cb.answer()


@router.callback_query(F.data == "up_sessions")
async def cb_up(cb):
    s = state(cb.from_user.id)
    s.clear()
    s["wait"] = "session"
    msg = await cb.message.answer("📁 Пришли .session + .json / .zip / .rar (или .txt с текстами)")
    s["prompt_id"] = msg.message_id
    await cb.answer()


@router.callback_query(F.data == "clean_bad")
async def cb_clean(cb):
    n = 0
    if config.BAD_DIR.exists():
        for p in config.BAD_DIR.iterdir():
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                n += 1
            except Exception:
                pass
    await cb.message.answer(f"🧹 Удалено: <b>{n}</b>")
    await cb.answer()


@router.message(F.document)
async def on_document(message, bot: Bot):
    await handle_doc(message, state(message.from_user.id), bot)


async def import_sessions_doc(message, fname, tmp, is_rar):
    phone_hint = ""
    phone = PHONE_RE.search((message.caption or "") + "\n" + (message.text or ""))
    if phone:
        phone_hint = phone.group(1).strip()
    res = await import_package(fname, tmp, is_rar=is_rar, phone_hint=phone_hint)
    total = count_sessions()
    routed_line = ""
    role = config.SESSION_ROUTE_ROLE
    peer_label = "🌐 intl →" if role in ("ru", "cis", "rushki") else "🇷🇺 RU/RB/KZ →"
    if res.get("routed_added") or res.get("routed_updated"):
        routed_line = (
            f"\n{peer_label} <code>{config.SESSIONS_PEER_DIR}</code>: "
            f"+<b>{res.get('routed_added', 0)}</b> / обн. <b>{res.get('routed_updated', 0)}</b>"
        )
    elif res.get("routed"):
        kind = "intl" if role in ("ru", "cis", "rushki") else "CIS"
        routed_line = f"\n{peer_label} в пакете {kind}: <b>{res['routed']}</b> (без изменений)"
    if res["files"] == 0:
        await message.answer("В пакете нет .session / .json.")
    elif res["added"] + res["updated"] == 0:
        await message.answer(f"Изменений нет. Сессий: <b>{total}</b>{routed_line}")
    elif res["added"] == 1 and res["updated"] == 0 and res["files"] == 1:
        who = f" {esc(phone_hint)}" if phone_hint else ""
        await message.answer(f"Новая сессия{who} загружена! Сессий: <b>{total}</b>{routed_line}")
    else:
        await message.answer(
            f"Добавлено: {res['added']}, обновлено: {res['updated']}. "
            f"Сессий: <b>{total}</b>{routed_line}"
        )


async def save_texts_doc(message, fname, tmp, is_rar):
    content = texts_from_upload(fname, tmp, is_rar)
    rm(tmp)
    if not content:
        await message.answer("В файле нет текста.")
        return
    config.save_blocks(blocks_from(content))
    await message.answer(f"✅ text.txt обновлён, блоков: <b>{len(config.load_blocks())}</b>")


async def handle_doc(message, s, bot):
    prompt_id = s.get("prompt_id")
    s.clear()
    fname = clean_name(message.document.file_name)
    tmp = temp_path(Path(fname).suffix.lower())
    try:
        f = await bot.get_file(message.document.file_id)
        await bot.download_file(f.file_path, destination=str(tmp))
        low = fname.lower()
        is_rar = low.endswith(".rar")
        if low.endswith(".session") or low.endswith(".json"):
            await import_sessions_doc(message, fname, tmp, False)
        elif low.endswith(".txt"):
            await save_texts_doc(message, fname, tmp, False)
        elif low.endswith(".zip") or low.endswith(".rar"):
            if is_rar and not RAR_OK:
                rm(tmp)
                await message.answer("rarfile не установлен.")
            elif archive_has_session(tmp, is_rar):
                await import_sessions_doc(message, fname, tmp, is_rar)
            else:
                await save_texts_doc(message, fname, tmp, is_rar)
        else:
            rm(tmp)
            await message.answer("Только .session / .json / .zip / .rar / .txt")
    except Exception as e:
        rm(tmp)
        await message.answer(f"Ошибка: {esc(e)}")
    finally:
        await del_msg(message)
        if prompt_id:
            try:
                await bot.delete_message(message.chat.id, prompt_id)
            except Exception:
                pass
