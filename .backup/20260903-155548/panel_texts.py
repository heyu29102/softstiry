from aiogram import F, Router

import asyncio

from pathlib import Path

import config
from domain_setup import (
    cloudflare_hint,
    format_domain_line,
    line_host,
    parse_domain_input,
    russia_access_hint,
    sync_nginx_redirect,
)
from panel_control import esc
from panel_kb import kb_back_texts
from panel_ui import admin_only, blocks_from, reset_state, state, waiting
from mail_content import load_photo_paths

router = Router()
router.message.filter(admin_only)
router.callback_query.filter(admin_only)


def parse_domain_line(line):
    from domain_setup import parse_stored_line
    _, link = parse_stored_line(line)
    return link


def domain_in_lines(lines, url):
    try:
        target = line_host(format_domain_line("", url))
    except ValueError:
        return False
    for ln in lines:
        if line_host(ln) == target:
            return True
    return False


def read_domain_lines(path=None):
    path = path or config.DOMAINS_FILE
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            lines.append(line)
    return lines


def write_domain_lines(lines, path=None):
    path = path or config.DOMAINS_FILE
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(path, content.encode("utf-8"))


def toggle_domain_line(text, path=None):
    path = path or config.DOMAINS_FILE
    try:
        label, url = parse_domain_input(text)
    except ValueError:
        return None, "invalid", "", ""

    line = format_domain_line(label, url)
    existing = read_domain_lines(path)
    target = line_host(line)

    if domain_in_lines(existing, url):
        kept = [ln for ln in existing if line_host(ln) != target]
        write_domain_lines(kept, path)
        _, nginx_msg = sync_nginx_redirect()
        return "removed", len(config.load_domain_links(path)), url, nginx_msg

    existing.append(line)
    write_domain_lines(existing, path)
    _, nginx_msg = sync_nginx_redirect()
    return "added", len(config.load_domain_links(path)), url, nginx_msg


def parse_channel_line(line):
    line = line.strip()
    if not line:
        return ""
    if "|" in line:
        _, link = line.split("|", 1)
        return link.strip()
    return line


def read_channel_lines():
    if not config.CHANNELS_FILE.exists():
        return []
    lines = []
    for raw in config.CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            lines.append(line)
    return lines


def write_channel_lines(lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(config.CHANNELS_FILE, content.encode("utf-8"))


def link_in_lines(lines, link):
    link = link.strip()
    for ln in lines:
        if parse_channel_line(ln) == link:
            return True
    return False


def toggle_channel_line(link):
    link = link.strip()
    if not link:
        return None, "empty"

    existing = read_channel_lines()

    if link_in_lines(existing, link):
        kept = [ln for ln in existing if parse_channel_line(ln) != link]
        write_channel_lines(kept)
        return "removed", len(config.load_channel_links())

    existing.append(link)
    write_channel_lines(existing)
    return "added", len(config.load_channel_links())


def parse_bot_line(line):
    line = line.strip()
    if not line or "|" not in line:
        return "", ""
    token, link = line.split("|", 1)
    return token.strip(), link.strip()


def read_bot_lines(path=None):
    path = path or config.BOTS_FILE
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and "|" in line:
            lines.append(line)
    return lines


def write_bot_lines(lines, path=None):
    path = path or config.BOTS_FILE
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(path, content.encode("utf-8"))


def toggle_bot_line(token, link, path=None):
    path = path or config.BOTS_FILE
    token = token.strip()
    link = link.strip()
    if not token or not link:
        return None, "empty"

    line = f"{token}|{link}"
    existing = read_bot_lines(path)

    if bot_link_in_lines(existing, link) or line in existing:
        kept = [ln for ln in existing if parse_bot_line(ln)[1] != link]
        write_bot_lines(kept, path)
        return "removed", len(config.load_bot_links(path))

    existing.append(line)
    write_bot_lines(existing, path)
    return "added", len(config.load_bot_links(path))


def bot_link_in_lines(lines, link):
    link = link.strip()
    for ln in lines:
        _, lnk = parse_bot_line(ln)
        if lnk == link:
            return True
    return False


def toggle_story_line(text):
    return None, "disabled", ""


@router.callback_query(F.data == "at")
async def cb_at(cb):
    blocks = config.load_blocks()
    ph = config.DOMAIN_LINK_PLACEHOLDER
    rows = [
        "📝 <b>text.txt</b> — блоки через <code>---</code>",
        f"В файле: <b>{len(blocks)}</b> блоков",
        f"Шаблон ссылки: <code>{esc(ph)}</code> (подставится из domains.txt)",
        "",
    ]
    if blocks:
        for i, block in enumerate(blocks[:5], 1):
            preview = block.replace("\n", " ")[:80]
            rows.append(f"{i}. <code>{esc(preview)}</code>")
        if len(blocks) > 5:
            rows.append(f"… и ещё {len(blocks) - 5}")
    else:
        rows.append("Пусто — добавь текст.")
    rows.extend([
        "",
        "Пришли новый текст (можно несколько блоков через <code>---</code>):",
        "или загрузи .txt в разделе Сессии.",
        "Рассылка подхватит без перезапуска (~30 сек).",
    ])
    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "text_blocks"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "photos")
async def cb_photos(cb):
    photos = load_photo_paths()
    rows = [
        "🖼 <b>Папка Photo/</b>",
        f"Фото в пуле: <b>{len(photos)}</b>",
        f"Доля фото в рассылке: <b>{int(config.PHOTO_SEND_RATIO * 100)}%</b> (остальное — только текст)",
        "",
        "Кинь сюда картинку (.jpg/.png/.webp) — сохраню в Photo/.",
        "Или положи файлы на сервер вручную.",
    ]
    if photos:
        rows.append("")
        for i, p in enumerate(photos[:10], 1):
            rows.append(f"{i}. <code>{esc(p.name)}</code>")
        if len(photos) > 10:
            rows.append(f"… и ещё {len(photos) - 10}")
    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "photo_upload"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href_domain")
async def cb_href_domain(cb):
    links = config.load_domain_links(config.DOMAINS_FILE)
    ph = config.DOMAIN_LINK_PLACEHOLDER

    rows = [
        "🌐 <b>Домены для групп</b> — <code>domains.txt</code>",
        f"🔗 В text.txt шаблон: <code>{esc(ph)}</code>",
        f"В пуле: <b>{len(links)}</b> доменов",
    ]

    if links:
        for i, link in enumerate(links[:10], 1):
            rows.append(f"{i}. <code>{esc(link)}</code>")
        if len(links) > 10:
            rows.append(f"… и ещё {len(links) - 10}")
    else:
        rows.append("Пул пуст — добавь домен.")

    rows.append("")
    rows.append("Отправь домен (https сам добавится):")
    rows.append("<code>look-now.pro</code>")
    rows.append("или <code>название|домен</code>")
    rows.append("nginx и https — автоматически.")
    rows.append("")
    rows.append("• ссылка <b>не</b> в пуле → <b>добавлю</b>")
    rows.append("• ссылка уже в пуле → <b>удалю</b>")
    rows.append("Можно слать несколько строк — режим не сбрасывается.")

    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_domain"

    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href")
async def cb_href(cb):
    await cb.answer("Каналы не используются — рассылка фото+текст в группы", show_alert=True)


@router.callback_query(F.data == "href_bot")
async def cb_href_bot(cb):
    links = config.load_bot_links(config.BOTS_FILE)

    rows = [
        "🤖 <b>Боты для групп</b> — <code>bots.txt</code>",
        "Формат: <code>токен|ссылка</code>",
        f"В пуле: <b>{len(links)}</b> ботов",
    ]

    if links:
        for i, link in enumerate(links[:10], 1):
            rows.append(f"{i}. <code>{esc(link)}</code>")
        if len(links) > 10:
            rows.append(f"… и ещё {len(links) - 10}")
    else:
        rows.append("Пул пуст — добавь бота.")

    rows.append("")
    rows.append("Отправь <code>токен|ссылка</code>:")
    rows.append("• ссылка <b>не</b> в пуле → <b>добавлю</b>")
    rows.append("• ссылка уже в пуле → <b>удалю</b>")
    rows.append("Можно слать несколько строк — режим не сбрасывается.")

    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_bot"

    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href_domain_contacts")
async def cb_href_domain_contacts(cb):
    links = config.load_domain_links(config.DOMAINS_CONTACTS_FILE)
    ph = config.DOMAIN_LINK_PLACEHOLDER
    rows = [
        "🌐 <b>Домены для контактов (ЛС)</b> — <code>domains_contacts.txt</code>",
        f"🔗 Шаблон в text.txt: <code>{esc(ph)}</code>",
        f"В пуле: <b>{len(links)}</b> доменов",
    ]
    if links:
        for i, link in enumerate(links[:10], 1):
            rows.append(f"{i}. <code>{esc(link)}</code>")
        if len(links) > 10:
            rows.append(f"… и ещё {len(links) - 10}")
    else:
        rows.append("Пул пуст — добавь домен.")
    rows.extend([
        "",
        "Отправь домен: <code>mydomain.pro</code>",
        "• нет в пуле → добавлю | есть → удалю",
    ])
    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_domain_contacts"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href_bot_contacts")
async def cb_href_bot_contacts(cb):
    links = config.load_bot_links(config.BOTS_CONTACTS_FILE)
    rows = [
        "🤖 <b>Боты для контактов (ЛС)</b> — <code>bots_contacts.txt</code>",
        "Формат: <code>токен|ссылка</code>",
        f"В пуле: <b>{len(links)}</b> ботов",
    ]
    if links:
        for i, link in enumerate(links[:10], 1):
            rows.append(f"{i}. <code>{esc(link)}</code>")
        if len(links) > 10:
            rows.append(f"… и ещё {len(links) - 10}")
    else:
        rows.append("Пул пуст — добавь бота.")
    rows.extend([
        "",
        "Отправь <code>токен|ссылка</code>:",
        "• нет в пуле → добавлю | есть → удалю",
    ])
    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_bot_contacts"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.message(waiting(
    "href_domain", "href_bot", "href_domain_contacts", "href_bot_contacts", "text_blocks"
))
async def texts_input(message):
    s = state(message.from_user.id)
    wait = s.get("wait")
    text = (message.text or "").strip()

    if wait == "text_blocks" and text:
        blocks = blocks_from(text)
        if not blocks:
            await message.answer("Пустой текст.")
            return
        config.save_blocks(blocks)
        await message.answer(
            f"✅ text.txt обновлён, блоков: <b>{len(blocks)}</b>\n"
            "Рассылка подхватит без перезапуска."
        )
        return

    if wait == "story_toggle" and text:
        await message.answer("Stories больше не используются — режим фото+текст в группы.")
        return

    if wait == "href_domain" and text:
        try:
            action, total, url, nginx_msg = await asyncio.to_thread(
                toggle_domain_line, text, config.DOMAINS_FILE
            )
        except Exception as error:
            await message.answer(f"❌ Ошибка: <code>{esc(error)}</code>")
            return

        if action is None:
            await message.answer(
                "Формат: <code>look-now.pro</code> или <code>название|домен</code>"
            )
            return

        cf = cloudflare_hint()
        ru_hint = russia_access_hint(line_host(url))
        if action == "added":
            extra = f"\n{ru_hint}" if ru_hint else ""
            await message.answer(
                f"✅ Добавлен в пул:\n<code>{esc(url)}</code>\n"
                f"Всего доменов: <b>{total}</b>\n"
                f"⚙️ {esc(nginx_msg)}\n"
                f"🌐 {cf}{extra}\n\n"
                "Ещё строка — или ⬅️ Назад."
            )
        else:
            await message.answer(
                f"🗑 Удалён из пула:\n<code>{esc(url)}</code>\n"
                f"Всего доменов: <b>{total}</b>\n"
                f"⚙️ {esc(nginx_msg)}\n\n"
                "Ещё строка — или ⬅️ Назад."
            )
        return

    if wait == "href_domain_contacts" and text:
        try:
            action, total, url, nginx_msg = await asyncio.to_thread(
                toggle_domain_line, text, config.DOMAINS_CONTACTS_FILE
            )
        except Exception as error:
            await message.answer(f"❌ Ошибка: <code>{esc(error)}</code>")
            return
        if action is None:
            await message.answer("Формат: <code>look-now.pro</code> или <code>название|домен</code>")
            return
        cf = cloudflare_hint()
        ru_hint = russia_access_hint(line_host(url))
        if action == "added":
            extra = f"\n{ru_hint}" if ru_hint else ""
            await message.answer(
                f"✅ Добавлен (контакты):\n<code>{esc(url)}</code>\n"
                f"Всего: <b>{total}</b>\n⚙️ {esc(nginx_msg)}\n🌐 {cf}{extra}"
            )
        else:
            await message.answer(
                f"🗑 Удалён (контакты):\n<code>{esc(url)}</code>\n"
                f"Всего: <b>{total}</b>\n⚙️ {esc(nginx_msg)}"
            )
        return

    if wait == "href_bot" and text:
        if "|" not in text:
            await message.answer("Формат: <code>токен|ссылка</code>")
            return

        token, link = [x.strip() for x in text.split("|", 1)]
        if not token or not link:
            await message.answer("Токен и ссылка не могут быть пустыми.")
            return

        action, total = toggle_bot_line(token, link, config.BOTS_FILE)
        if action is None:
            await message.answer("❌ Пустой токен или ссылка.")
            return

        if action == "added":
            await message.answer(
                f"✅ Добавлен в пул:\n<code>{esc(link)}</code>\n"
                f"Всего ботов: <b>{total}</b>\n\n"
                "Ещё строка — или ⬅️ Назад."
            )
        else:
            await message.answer(
                f"🗑 Удалён из пула:\n<code>{esc(link)}</code>\n"
                f"Всего ботов: <b>{total}</b>\n\n"
                "Ещё строка — или ⬅️ Назад."
            )
        return

    if wait == "href_bot_contacts" and text:
        if "|" not in text:
            await message.answer("Формат: <code>токен|ссылка</code>")
            return
        token, link = [x.strip() for x in text.split("|", 1)]
        if not token or not link:
            await message.answer("Токен и ссылка не могут быть пустыми.")
            return
        action, total = toggle_bot_line(token, link, config.BOTS_CONTACTS_FILE)
        if action is None:
            await message.answer("❌ Пустой токен или ссылка.")
            return
        if action == "added":
            await message.answer(
                f"✅ Добавлен (контакты):\n<code>{esc(link)}</code>\nВсего: <b>{total}</b>"
            )
        else:
            await message.answer(
                f"🗑 Удалён (контакты):\n<code>{esc(link)}</code>\nВсего: <b>{total}</b>"
            )
        return

    await message.answer("Меню: /start")


@router.message(waiting("photo_upload"), F.photo)
async def photo_upload(message, bot):
    from panel_control import clean_name

    s = state(message.from_user.id)
    photo = message.photo[-1]
    ext = ".jpg"
    fname = clean_name(f"photo_{photo.file_unique_id}{ext}")
    dest = config.PHOTO_DIR / fname
    try:
        f = await bot.get_file(photo.file_id)
        await bot.download_file(f.file_path, destination=str(dest))
        total = len(load_photo_paths())
        await message.answer(f"✅ Фото сохранено: <code>{esc(fname)}</code>\nВ пуле: <b>{total}</b>")
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{esc(e)}</code>")


@router.message(waiting("photo_upload"), F.document)
async def photo_upload_doc(message, bot):
    from panel_control import clean_name
    from mail_content import PHOTO_EXTENSIONS

    doc = message.document
    if not doc or not doc.file_name:
        await message.answer("Нужен файл-картинка (.jpg/.png/.webp).")
        return
    ext = Path(doc.file_name).suffix.lower()
    if ext not in PHOTO_EXTENSIONS:
        await message.answer("Только .jpg / .png / .webp / .gif")
        return
    fname = clean_name(doc.file_name)
    dest = config.PHOTO_DIR / fname
    try:
        f = await bot.get_file(doc.file_id)
        await bot.download_file(f.file_path, destination=str(dest))
        total = len(load_photo_paths())
        await message.answer(f"✅ Фото сохранено: <code>{esc(fname)}</code>\nВ пуле: <b>{total}</b>")
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{esc(e)}</code>")
