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
from panel_control import esc
from panel_kb import kb_back_texts
from panel_ui import admin_only, reset_state, state, waiting
from story_refs import load_story_refs, parse_story_line, save_story_refs

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


def toggle_story_line(text):
    ref = parse_story_line(text)
    if not ref:
        return None, "invalid", ""
    refs = load_story_refs(config.STORIES_FILE)
    key = (ref.peer.lower(), ref.story_id)
    for i, r in enumerate(refs):
        if (r.peer.lower(), r.story_id) == key:
            refs.pop(i)
            save_story_refs(refs, config.STORIES_FILE)
            return "removed", len(refs), ref.label
    refs.append(ref)
    save_story_refs(refs, config.STORIES_FILE)
    return "added", len(refs), ref.label


@router.callback_query(F.data == "stories")
async def cb_stories(cb):
    refs = load_story_refs(config.STORIES_FILE)
    if not refs:
        await cb.message.answer("Историй нет. Жми «Добавить / убрать».")
    else:
        rows = [f"{i}. <code>{esc(r.label)}</code>" for i, r in enumerate(refs[:30], 1)]
        if len(refs) > 30:
            rows.append(f"… и ещё {len(refs) - 30}")
        await cb.message.answer("📖 stories.txt:\n" + "\n".join(rows))
    await cb.answer()


@router.callback_query(F.data == "story_toggle")
async def cb_story_toggle(cb):
    refs = load_story_refs(config.STORIES_FILE)
    rows = [
        "📖 <b>Добавить story в рассылку</b>",
        f"В пуле сейчас: <b>{len(refs)}</b>",
        "",
        "Пришли ссылку или строку (можно несколько, с новой строки):",
        "<code>https://t.me/testchanelkk/s/1</code>",
        "<code>channel|42</code>",
        "",
        "• ссылки <b>не</b> в пуле → <b>добавлю</b>",
        "• та же ссылка ещё раз → <b>удалю</b> из пула",
        "",
        "Рассылка подхватит без перезапуска (~30 сек).",
        "Или просто кинь ссылку t.me/.../s/... в бот без кнопок.",
    ]
    reset_state(cb.from_user.id)
    state(cb.from_user.id)["wait"] = "story_toggle"
    await cb.message.answer("\n".join(rows), reply_markup=kb_back_texts())
    await cb.answer()


@router.callback_query(F.data == "href_domain")
async def cb_href_domain(cb):
    links = config.load_domain_links()
    ph = config.DOMAIN_LINK_PLACEHOLDER

    rows = [
        f"🔗 В text.txt шаблон: <code>{esc(ph)}</code>",
        "text.txt не меняем — домен подставляется при отправке.",
        f"🌐 В пуле: <b>{len(links)}</b> доменов",
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
    await cb.answer("Каналы не используются — рассылка через stories.txt", show_alert=True)


@router.callback_query(F.data == "at")
async def cb_at(cb):
    await cb.answer("Текстовые блоки отключены — только stories", show_alert=True)


@router.callback_query(F.data == "href_bot")
async def cb_href_bot(cb):
    links = config.load_bot_links()

    rows = [
        "🔗 Пул для редиректа (домены → боты)",
        "Файл: <code>bots.txt</code> (токен|ссылка)",
        f"🤖 В пуле: <b>{len(links)}</b> ботов",
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


@router.message(waiting("href_domain", "href_bot", "story_toggle"))
async def texts_input(message):
    s = state(message.from_user.id)
    wait = s.get("wait")
    text = (message.text or "").strip()

    if wait == "story_toggle" and text:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            await message.answer("Пустая строка.")
            return
        added = removed = bad = 0
        added_labels = []
        removed_labels = []
        total = len(load_story_refs(config.STORIES_FILE))
        for line in lines:
            action, total, label = toggle_story_line(line)
            if action is None:
                bad += 1
                continue
            if action == "added":
                added += 1
                added_labels.append(label)
            else:
                removed += 1
                removed_labels.append(label)
        rows = [f"✅ +{added} | 🗑 −{removed} | ⚠️ пропуск {bad}", f"В пуле: <b>{total}</b>"]
        if added_labels:
            rows.append("Добавлено:")
            for lb in added_labels[:10]:
                rows.append(f"• <code>{esc(lb)}</code>")
        if removed_labels:
            rows.append("Удалено:")
            for lb in removed_labels[:10]:
                rows.append(f"• <code>{esc(lb)}</code>")
        rows.append("\nЕщё ссылки — или ⬅️ Назад.")
        await message.answer("\n".join(rows))
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
