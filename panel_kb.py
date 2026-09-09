import os

from aiogram.utils.keyboard import InlineKeyboardBuilder

from panel_api import api_monitor
from panel_control import app_running


def kb_main():
    k = InlineKeyboardBuilder()
    k.button(text="📦 Сессии", callback_data="nav:sessions")
    k.button(text="📖 Истории", callback_data="nav:texts")
    k.button(text="⚙️ Рассылка", callback_data="nav:run")
    k.button(text="🤖 Авто", callback_data="nav:auto")
    k.button(text="📊 Статус", callback_data="nav:status")
    k.adjust(2, 2, 1)
    return k.as_markup()


def kb_sessions():
    k = InlineKeyboardBuilder()
    k.button(text="📂 Загрузить (.session/.zip/.rar)", callback_data="up_sessions")
    k.button(text="✅ Подсчёт", callback_data="count")
    k.button(text="🧹 Очистить bad", callback_data="clean_bad")
    k.button(text="⬅️ Назад", callback_data="nav:main")
    k.adjust(1)
    return k.as_markup()


def kb_texts():
    k = InlineKeyboardBuilder()
    k.button(text="👥 Истории для групп", callback_data="stories_groups")
    k.button(text="📇 Истории для контактов", callback_data="stories_contacts")
    k.button(text="➕ Группы: добавить/убрать", callback_data="story_toggle:group")
    k.button(text="➕ Контакты: добавить/убрать", callback_data="story_toggle:contact")
    k.button(text="🌐 Домен в пул / убрать", callback_data="href_domain")
    k.button(text="🤖 Бот в пул / убрать", callback_data="href_bot")
    k.button(text="⬅️ Назад", callback_data="nav:main")
    k.adjust(1)
    return k.as_markup()


def kb_back_texts():
    k = InlineKeyboardBuilder()
    k.button(text="⬅️ Назад", callback_data="nav:texts")
    return k.as_markup()


def kb_run():
    k = InlineKeyboardBuilder()
    k.button(text=("🔴 Остановить" if app_running() else "🟢 Запустить"), callback_data="toggle")
    k.button(text="♻️ Перезапуск", callback_data="restart")
    k.button(text="⬅️ Назад", callback_data="nav:main")
    k.adjust(1)
    return k.as_markup()


def kb_status():
    k = InlineKeyboardBuilder()
    k.button(text="🔄 Обновить", callback_data="nav:status")
    k.button(text="📜 Хвост логов", callback_data="tail")
    k.button(text="📦 Скачать лог", callback_data="dl")
    k.button(text="⬅️ Назад", callback_data="nav:main")
    k.adjust(1)
    return k.as_markup()


def kb_auto():
    k = InlineKeyboardBuilder()
    k.button(text=f"API мониторинг: {'🟢 ON' if api_monitor['on'] else '🔴 OFF'}", callback_data="api_toggle")
    if os.name != "nt":
        k.button(text="▶️ start", callback_data="svc:start")
        k.button(text="⏸ stop", callback_data="svc:stop")
        k.button(text="♻️ restart", callback_data="svc:restart")
        k.button(text="⬅️ Назад", callback_data="nav:main")
        k.adjust(1, 3, 1)
    else:
        k.button(text="⬅️ Назад", callback_data="nav:main")
        k.adjust(1)
    return k.as_markup()
