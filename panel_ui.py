from aiogram.exceptions import TelegramBadRequest

import config

states = {}


def state(uid):
    s = states.get(uid)
    if s is None:
        s = states[uid] = {}
    return s


def reset_state(uid):
    states[uid] = {}


def admin_only(event):
    return bool(event.from_user and event.from_user.id in config.ADMIN_IDS)


def waiting(*kinds):
    def check(message):
        return state(message.from_user.id).get("wait") in kinds
    return check


def blocks_from(content):
    blocks, buf = [], []
    for line in content.splitlines():
        if line.strip() == "---":
            b = "\n".join(buf).strip()
            if b:
                blocks.append(b)
            buf = []
        else:
            buf.append(line)
    tail = "\n".join(buf).strip()
    if tail:
        blocks.append(tail)
    return blocks


async def edit(msg, text, kb=None):
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def del_msg(msg):
    try:
        await msg.delete()
    except Exception:
        pass
