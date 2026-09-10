from pathlib import Path

from aiogram import F, Router

import asyncio

import config
from domain_setup import (
    cloudflare_hint,
    format_domain_line,
    line_host,
    parse_domain_input,
    russia_access_hint,
    sync_nginx_redirect,
)
from mail_content import PHOTO_EXTENSIONS, load_photo_paths
from panel_control import esc
from panel_kb import kb_back_texts
from panel_ui import admin_only, blocks_from, reset_state, state, waiting

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


def read_domain_lines():
    if not config.DOMAINS_FILE.exists():
        return []
    lines = []
    for raw in config.DOMAINS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            lines.append(line)
    return lines


def write_domain_lines(lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(config.DOMAINS_FILE, content.encode("utf-8"))


def toggle_domain_line(text):
    try:
        label, url = parse_domain_input(text)
    except ValueError:
        return None, "invalid", "", ""

    line = format_domain_line(label, url)
    existing = read_domain_lines()
    target = line_host(line)

    if domain_in_lines(existing, url):
        kept = [ln for ln in existing if line_host(ln) != target]
        write_domain_lines(kept)
        _, nginx_msg = sync_nginx_redirect()
        return "removed", len(config.load_domain_links()), url, nginx_msg

    existing.append(line)
    write_domain_lines(existing)
    _, nginx_msg = sync_nginx_redirect()
    return "added", len(config.load_domain_links()), url, nginx_msg


def normalize_share_link(link: str) -> str:
    link = (link or "").strip()
    if not link or link.startswith("#"):
        return ""
    if "|" in link:
        _, link = link.split("|", 1)
        link = link.strip()
    if link.startswith("share.google/"):
        link = "https://" + link
    elif link.startswith("share.google"):
        link = "https://" + link.lstrip("/")
    return link


def read_share_lines():
    if not config.SHARE_LINKS_FILE.exists():
        return []
    lines = []
    for raw in config.SHARE_LINKS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def write_share_lines(lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(config.SHARE_LINKS_FILE, content.encode("utf-8"))


def share_link_in_lines(lines, link):
    target = normalize_share_link(link)
    if not target:
        return False
    for ln in lines:
        if normalize_share_link(ln) == target:
            return True
    return False


def toggle_share_line(link):
    link = normalize_share_link(link)
    if not link:
        return None, "empty"

    existing = read_share_lines()
    stored = link
    if share_link_in_lines(existing, link):
        kept = [ln for ln in existing if normalize_share_link(ln) != link]
        write_share_lines(kept)
        return "removed", len(config.load_share_links())

    existing.append(stored)
    write_share_lines(existing)
    return "added", len(config.load_share_links())


def parse_bot_line(line):
    line = line.strip()
    if not line or "|" not in line:
        return "", ""
    token, link = line.split("|", 1)
    return token.strip(), link.strip()


def read_bot_lines():
    if not config.BOTS_FILE.exists():
        return []
    lines = []
    for raw in config.BOTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and "|" in line:
            lines.append(line)
    return lines


def write_bot_lines(lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(config.BOTS_FILE, content.encode("utf-8"))


def bot_link_in_lines(lines, link):
    link = link.strip()
    for ln in lines:
        _, lnk = parse_bot_line(ln)
        if lnk == link:
            return True
    return False


def toggle_bot_line(token, link):
    token = token.strip()
    link = link.strip()
    if not token or not link:
        return None, "empty"

    line = f"{token}|{link}"
    existing = read_bot_lines()

    if bot_link_in_lines(existing, link) or line in existing:
        kept = [ln for ln in existing if parse_bot_line(ln)[1] != link]
        write_bot_lines(kept)
        return "removed", len(config.load_bot_links())

    existing.append(line)
    write_bot_lines(existing)
    return "added", len(config.load_bot_links())


@router.callback_query(F.data == "at")
async def cb_at(cb):
    blocks = config.load_blocks()
    ph = config.DOMAIN_LINK_PLACEHOLDER
    rows = [
        "📝 <b>text.txt</b> — блоки через <code>---</code>",
        f"В файле: <b>{len(blocks)}</b> блоков",
        f"Шаблон ссылки: <code>{esc(ph)}</code> (рандом из share_links.txt)",
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


@router.callback_query(F.data == "href_share")
async def cb_href_share(cb):
    links = config.load_share_links()
    ph = config.DOMAIN_LINK_PLACEHOLDER

    rows = [
        "🔗 <b>Ссылки</b> — <code>share_links.txt</code>",
        f"В text.txt шаблон: <code>{esc(ph)}</code>",
        "t.me, share.google — что угодно. На каждую отправку — случайная из пула.",
        f"В пуле: <b>{len(links)}</b> ссылок",
    ]

    if links:
        for i, link in enumerate(links[:10], 1):
            rows.append(f"{i}. <code>{esc(link)}</code>")
        if len(links) > 10:
            rows.append(f"… и ещё {len(links) - 10}")
    else:
        rows.append("Пул пуст — добавь ссылки.")

    rows.extend([
        "",
        "Отправь ссылку (можно несколько строк):",
        "<code>https://t.me/HerAllCC0ntentbot?startapp=3888</code>",
        "",
        "• ссылки <b>не</b> в пуле → <b>добавлю</b>",
        "• та же ссылка ещё раз → <b>удалю</b>",
        "Рассылка подхватит без перезапуска (~30 сек).",
    ])

    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_share"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href_domain")
async def cb_href_domain(cb):
    links = config.load_domain_links()

    rows = [
        "🌐 <b>Домены для redirect</b> — <code>domains.txt</code>",
        "(не для рассылки — только веб-редирект на ботов)",
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
        "Отправь домен (https сам добавится):",
        "<code>look-now.pro</code>",
        "или <code>название|домен</code>",
    ])

    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_domain"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href")
async def cb_href(cb):
    await cb.answer("Каналы не используются", show_alert=True)


@router.callback_query(F.data == "href_bot")
async def cb_href_bot(cb):
    links = config.load_bot_links()

    rows = [
        "🤖 <b>Боты для redirect</b> — <code>bots.txt</code>",
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
        "• ссылка <b>не</b> в пуле → <b>добавлю</b>",
        "• ссылка уже в пуле → <b>удалю</b>",
    ])

    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "href_bot"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data.in_({"stories", "story_toggle"}))
async def cb_stories_disabled(cb):
    await cb.answer("Stories отключены — режим текст+фото", show_alert=True)


@router.message(waiting("href_domain", "href_bot", "href_share", "text_blocks"))
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

    if wait == "href_share" and text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            await message.answer("Пустая строка.")
            return
        added = removed = bad = 0
        total = len(config.load_share_links())
        for line in lines:
            action, total = toggle_share_line(line)
            if action is None:
                bad += 1
                continue
            if action == "added":
                added += 1
            else:
                removed += 1
        await message.answer(
            f"✅ +{added} | 🗑 −{removed} | ⚠️ пропуск {bad}\n"
            f"В пуле share: <b>{total}</b>\n\n"
            "Ещё ссылки — или ⬅️ Назад."
        )
        return

    if wait == "href_domain" and text:
        try:
            action, total, url, nginx_msg = await asyncio.to_thread(toggle_domain_line, text)
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

    if wait == "href_bot" and text:
        if "|" not in text:
            await message.answer("Формат: <code>токен|ссылка</code>")
            return

        token, link = [x.strip() for x in text.split("|", 1)]
        if not token or not link:
            await message.answer("Токен и ссылка не могут быть пустыми.")
            return

        action, total = toggle_bot_line(token, link)
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

    await message.answer("Меню: /start")


@router.message(waiting("photo_upload"), F.photo)
async def photo_upload(message, bot):
    from panel_control import clean_name

    photo = message.photo[-1]
    fname = clean_name(f"photo_{photo.file_unique_id}.jpg")
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
