import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
import panel_auto
import panel_fallback
import panel_menu
import panel_run
import panel_sessions
import panel_texts
from panel_api import api_loop
from panel_control import single_instance

if not config.BOT_TOKEN:
    raise SystemExit("Заполни BOT_TOKEN в .env")
if not config.ADMIN_IDS:
    raise SystemExit("Заполни ADMIN_IDS в .env")

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_routers(
    panel_menu.router,
    panel_sessions.router,
    panel_texts.router,
    panel_run.router,
    panel_auto.router,
    panel_fallback.router,
)


async def main():
    single_instance()
    task_api = asyncio.create_task(api_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task_api.cancel()
        await asyncio.gather(task_api, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
