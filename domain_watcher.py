"""
Проверяет домены в domains.txt:
- сайт отвечает (HTTP/SSL)
- redirect на t.me работает (воронка)
- Telegram не считает ссылку скамом (маркеры в HTML, TelegramBot UA)
- опционально: GetWebPagePreview через Telethon-сессию (DOMAINS_WATCHER_SESSION)

Мёртвые / скам / не открываются — удаляет из файла.
Сетевые ошибки и неизвестный статус — строку оставляет.
"""

import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config

REQUEST_TIMEOUT = 12
REQUEST_RETRIES = 3
MAX_REDIRECTS = 10
BROWSER_UA = "Mozilla/5.0 (compatible; DomainWatcher/1.0)"
TELEGRAMBOT_UA = "TelegramBot (like TwitterBot)"

SCAM_MARKERS = (
    "suspicious link",
    "this link is suspicious",
    "this link may be harmful",
    "this link might be harmful",
    "reported for phishing",
    "reported as phishing",
    "malicious link",
    "phishing",
    "harmful website",
    "dangerous website",
    "open an external link",
    "could be unsafe",
    "may be unsafe",
    "подозрительная ссылка",
    "подозрительную ссылку",
    "подозрительной ссылке",
    "вредонос",
    "мошенн",
    "фишинг",
    "опасн",
    "небезопасн",
)

DEAD_MARKERS = (
    "can't be displayed",
    "can not be displayed",
    "this page doesn't exist",
    "this page does not exist",
    "has been removed",
    "inactive and cannot be accessed",
    "channel is unavailable",
    "сожалению, вы не можете",
    "нарушает условия",
    "нарушает правила",
)

TELEGRAM_RPC_DEAD = (
    "url_invalid",
    "webpage_media_empty",
    "blocked",
    "disallowed",
    "malicious",
    "harmful",
    "scam",
    "phishing",
    "dangerous",
)


def watcher_session_path():
    raw = (config.DOMAINS_WATCHER_SESSION or "").strip()
    if raw:
        return Path(raw)
    default = config.BASE_DIR / "watcher.session"
    if default.exists():
        return default
    return None


def read_domains():
    if not config.DOMAINS_FILE.exists():
        print(f"Файл не найден: {config.DOMAINS_FILE}")
        return []

    lines = []
    for raw in config.DOMAINS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            lines.append(line)
    return lines


def write_domains(lines):
    content = "\n".join(lines)
    if content:
        content += "\n"
    config.atomic_write(config.DOMAINS_FILE, content.encode("utf-8"))
    try:
        from domain_setup import sync_nginx_redirect

        _, msg = sync_nginx_redirect()
        print(f"[nginx sync] {msg}")
    except Exception as error:
        print(f"[nginx sync] error: {error}")


def parse_domain_line(line):
    line = line.strip()
    if not line:
        return "", ""
    if "|" in line:
        name, link = line.split("|", 1)
        return name.strip(), link.strip()
    return "", line


def normalize_url(link):
    link = link.strip()
    if not link:
        return ""
    if not re.match(r"^https?://", link, re.I):
        link = "https://" + link
    return link


def host_from_url(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def text_has_markers(text, markers):
    low = (text or "").lower()
    return any(m in low for m in markers)


async def follow_url(client, url, user_agent):
    """Следует редиректам вручную, собирает HTML для проверки маркеров."""
    visited = []
    current = url
    last_response = None
    bodies = []

    for _ in range(MAX_REDIRECTS):
        if current in visited:
            print(f"Петля редиректов: {current}")
            return last_response, bodies, current, "loop"
        visited.append(current)

        response = await client.get(
            current,
            headers={"User-Agent": user_agent},
            follow_redirects=False,
        )
        last_response = response

        body = ""
        try:
            body = response.text[:50000]
        except Exception:
            pass
        if body:
            bodies.append(body)

        if text_has_markers(body, SCAM_MARKERS):
            return last_response, bodies, current, "scam_html"
        if text_has_markers(body, DEAD_MARKERS):
            return last_response, bodies, current, "dead_html"

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") or response.headers.get("location")
            if not location:
                break
            current = httpx.URL(current).join(location)
            continue

        break

    return last_response, bodies, current, None


async def http_check(link):
    """
    True  — домен жив, редирект на t.me ок (или 503 = nginx жив, пул ботов пуст).
    False — мёртвый / скам / битый SSL / петля / не t.me.
    None  — сеть, неизвестный ответ.
    """
    url = normalize_url(link)
    if not url:
        return None

    host = host_from_url(url)
    if not host:
        return None

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        verify=True,
        follow_redirects=False,
    ) as client:
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                response, bodies, final_url, marker = await follow_url(client, url, BROWSER_UA)
                code = response.status_code if response else 0
                final_host = host_from_url(str(final_url))

                print(
                    f"HTTP #{attempt} {url}: {code} → {final_url} "
                    f"(final host: {final_host or '-'})"
                )

                if marker == "scam_html":
                    print("HTML: маркеры скама/phishing")
                    return False
                if marker == "dead_html":
                    print("HTML: маркеры мёртвой страницы")
                    return False
                if marker == "loop":
                    return False

                if code == 503:
                    print("503 redirect — домен жив, bots.txt пуст или только self-links")
                    return True

                if code >= 500:
                    if attempt < REQUEST_RETRIES:
                        await asyncio.sleep(2)
                        continue
                    return None

                if code >= 400:
                    return False

                if final_host in ("t.me", "telegram.me", "telegram.dog"):
                    return True

                if final_host == host:
                    print("Редирект не ушёл на t.me — остался на своём домене")
                    return False

                if final_host:
                    print(f"Финальный хост не t.me: {final_host}")
                    return False

                return None

            except httpx.HTTPStatusError as error:
                print(f"HTTP ошибка: {error}")
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError) as error:
                print(f"Сеть, попытка {attempt}/{REQUEST_RETRIES}: {error}")
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)
            except Exception as error:
                print(f"Ошибка HTTP проверки: {error}")
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)

    return None


async def telegrambot_reachable(link):
    """
    True  — TelegramBot UA получает ответ (не блок на уровне nginx/WAF).
    False — сайт явно блокирует TelegramBot (403/404 при живом браузерном UA).
    None  — не сравнить.
    """
    url = normalize_url(link)
    if not url:
        return None

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        verify=True,
        follow_redirects=False,
    ) as client:
        try:
            browser_resp, _, _, browser_marker = await follow_url(client, url, BROWSER_UA)
            tg_resp, tg_bodies, _, tg_marker = await follow_url(client, url, TELEGRAMBOT_UA)

            browser_code = browser_resp.status_code if browser_resp else 0
            tg_code = tg_resp.status_code if tg_resp else 0

            print(f"TelegramBot UA: HTTP {tg_code} (browser: {browser_code})")

            if tg_marker == "scam_html" or any(text_has_markers(b, SCAM_MARKERS) for b in tg_bodies):
                print("TelegramBot: маркеры скама в HTML")
                return False

            if browser_code < 400 and tg_code >= 400:
                print("Сайт блокирует TelegramBot — превью в Telegram может не работать")
                return None

            return True
        except Exception as error:
            print(f"TelegramBot проверка: {error}")
            return None


async def telethon_preview_check(link):
    """
    True  — Telegram API отдал превью (ссылка не в чёрном списке).
    False — RPC явно говорит blocked/invalid/scam.
    None  — нет сессии / временная ошибка / редирект без превью (норма).
    """
    session_path = watcher_session_path()
    if not session_path or not session_path.exists():
        return None

    url = normalize_url(link)
    if not url:
        return None

    try:
        from opentele.api import API
        from opentele.tl import TelegramClient
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import GetWebPagePreviewRequest
    except ImportError as error:
        print(f"Telethon не установлен: {error}")
        return None

    api = API.TelegramDesktop.Generate(unique_id="domain_watcher")
    client = TelegramClient(str(session_path), api=api)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"Сессия не авторизована: {session_path}")
            return None

        try:
            result = await client(GetWebPagePreviewRequest(message=url))
            media = getattr(result, "media", None)
            if media is None:
                print("Telethon: превью пустое (редирект-домен — норма)")
                return None

            webpage = getattr(media, "webpage", None)
            if webpage is not None:
                not_supported = getattr(webpage, "not_supported", False)
                if not_supported:
                    print("Telethon: webpage not_supported")
                    return False
                print(f"Telethon: превью ok, type={getattr(webpage, 'type', '?')}")
                return True

            print("Telethon: media без webpage")
            return None

        except RPCError as error:
            msg = str(error).lower()
            print(f"Telethon RPC: {error}")
            if any(token in msg for token in TELEGRAM_RPC_DEAD):
                return False
            return None
    except Exception as error:
        print(f"Telethon ошибка: {error}")
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def domain_alive(link):
    http_status = await http_check(link)
    if http_status is False:
        return False
    if http_status is None:
        return None

    tg_bot_status = await telegrambot_reachable(link)
    if tg_bot_status is False:
        return False

    preview_status = await telethon_preview_check(link)
    if preview_status is False:
        return False

    return True


async def check_all_domains():
    lines = read_domains()

    if not lines:
        print("domains.txt пустой")
        return

    alive_lines = []
    removed = 0

    for line in lines:
        name, link = parse_domain_line(line)
        if not link:
            print(f"Пустая ссылка, удаляю: {line}")
            removed += 1
            continue

        print(f"Проверяю: {link}")
        status = await domain_alive(link)

        if status is True:
            print(f"Живой: {link}")
            alive_lines.append(line)
            continue

        if status is None:
            print(f"Не удалось проверить, оставляю: {link}")
            alive_lines.append(line)
            continue

        print(f"Мёртвый/скам, удаляю: {link}")
        removed += 1

    if removed:
        write_domains(alive_lines)
        print(f"Обновлён domains.txt: осталось {len(alive_lines)}, удалено {removed}")
    else:
        print(f"Все домены живы ({len(alive_lines)})")


async def main():
    session = watcher_session_path()
    print("Domain watcher запущен", flush=True)
    print(f"Файл доменов: {config.DOMAINS_FILE}", flush=True)
    print(f"Интервал: {config.DOMAINS_WATCHER_INTERVAL} сек", flush=True)
    if session:
        print(f"Telethon сессия: {session}", flush=True)
    else:
        print("Telethon сессия: нет (только HTTP + TelegramBot UA)", flush=True)

    while True:
        try:
            await check_all_domains()
        except Exception as error:
            print(f"Ошибка watcher: {error}")

        await asyncio.sleep(config.DOMAINS_WATCHER_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
