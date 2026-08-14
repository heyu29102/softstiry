import asyncio

import httpx

import config

CHECK_INTERVAL = 30
REQUEST_TIMEOUT = 10
REQUEST_RETRIES = 3


def read_bots():
    if not config.BOTS_FILE.exists():
        print(f"Файл не найден: {config.BOTS_FILE}")
        return []

    bots = []
    for raw_line in config.BOTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" not in line:
            print(f"Пропускаю неправильную строку: {line}")
            continue
        bots.append(line)
    return bots


def write_bots(bots):
    content = "\n".join(bots)
    if content:
        content += "\n"
    config.atomic_write(config.BOTS_FILE, content.encode("utf-8"))


def parse_bot(line):
    token, link = line.split("|", 1)
    return token.strip(), link.strip()


async def bot_alive(token):
    """
    True  — токен живой.
    False — бот удалён / токен недействителен.
    None  — временная ошибка, статус неизвестен.
    """
    url = f"https://api.telegram.org/bot{token}/getMe"

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                response = await client.get(url)

                try:
                    data = response.json()
                except Exception:
                    data = {}

                print(
                    f"Проверка #{attempt}: HTTP {response.status_code}, "
                    f"ответ: {data}"
                )

                if response.status_code == 200 and data.get("ok") is True:
                    return True

                error_code = data.get("error_code")
                description = str(data.get("description", "")).lower()

                if response.status_code in (401, 404):
                    return False

                if error_code in (401, 404):
                    return False

                dead_messages = (
                    "unauthorized",
                    "not found",
                    "invalid token",
                    "bot token is invalid",
                )
                if any(msg in description for msg in dead_messages):
                    return False

                if response.status_code >= 500:
                    await asyncio.sleep(2)
                    continue

                # Rate limit — токен живой
                if response.status_code == 429 or error_code == 429:
                    return True

                return None

            except (httpx.TimeoutException, httpx.NetworkError) as error:
                print(f"Ошибка сети, попытка {attempt}/{REQUEST_RETRIES}: {error}")
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)

            except Exception as error:
                print(f"Ошибка проверки бота: {error}")
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)

    return None


async def check_all_bots():
    """
    Проверяет все строки в bots.txt.
    Мёртвые и битые строки удаляет из файла.
    При сетевой ошибке бот остаётся в списке.
    """
    bots = read_bots()

    if not bots:
        print("bots.txt пустой")
        return

    alive = []
    removed = 0

    for line in bots:
        try:
            token, link = parse_bot(line)
        except Exception as error:
            print(f"Неправильная строка, удаляю: {line} ({error})")
            removed += 1
            continue

        print(f"Проверяю: {link}")
        status = await bot_alive(token)

        if status is True:
            print(f"Живой: {link}")
            alive.append(line)
            continue

        if status is None:
            print(f"Не удалось проверить, оставляю: {link}")
            alive.append(line)
            continue

        print(f"Мёртвый, удаляю: {link}")
        removed += 1

    if removed:
        write_bots(alive)
        print(f"Обновлён bots.txt: осталось {len(alive)}, удалено {removed}")
    else:
        print(f"Все боты живы ({len(alive)})")


async def main():
    print("Watcher запущен")
    print(f"Файл ботов: {config.BOTS_FILE}")
    print(f"Интервал проверки: {CHECK_INTERVAL} секунд")

    while True:
        try:
            await check_all_bots()
        except Exception as error:
            print(f"Ошибка watcher: {error}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())